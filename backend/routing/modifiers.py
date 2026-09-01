"""约束修饰的**确定性扫描**（`Z-45`）。

## 为什么不交给模型

`SolveIntent.incremental_constraints` 原先完全靠 Planner 自己产出。
M9-B 实测：**99 条带修饰的样本里只抽到 8 条**（F1 16.02%）。
补上字段 description 之后 20 条里从 3 条升到 4 条 —— **几乎没动**。

线索是**同一句话的不同措辞成败不一致**：

    「把2026-W04的班排出来，周三不要安排飞行」  ✅ 抽到 FORBID
    「下周给所有人排班，周三不要安排飞行」      ❌ 没抽到

同一个修饰、同一个类别，只因为前半句不同就成败不同 —— 说明模型**没有稳定
规则可依**，是在碰运气。而全集统计显示这些表述高度模式化：
**84 种表述、7 类**，「用 AC××」「每天最多排 N 个架次」「周X不要安排飞行」
这类句式可枚举。

**可枚举的东西不该交给概率模型。** 这与 `merge_slots` 对周次的处置同构：
确定性扫描器先抓一遍，模型只负责扫描器覆盖不到的自由表述。

⚠️ **本模块只做「原话 → 结构化修饰」的识别，不做任何排班判断。**
它产出的 `IncrementalConstraint` 仍然是**求解器输入**，照常走
`compile_spec → solve → validate`（v6 §7.3.4）。
"""

from __future__ import annotations

import re
from typing import Final

from backend.schemas.intent import IncrementalConstraint

#: 星期表述 → 规范写法。
_WEEKDAYS: Final[dict[str, str]] = {
    "周一": "周一",
    "星期一": "周一",
    "礼拜一": "周一",
    "周二": "周二",
    "星期二": "周二",
    "礼拜二": "周二",
    "周三": "周三",
    "星期三": "周三",
    "礼拜三": "周三",
    "周四": "周四",
    "星期四": "周四",
    "礼拜四": "周四",
    "周五": "周五",
    "星期五": "周五",
    "礼拜五": "周五",
    "周六": "周六",
    "星期六": "周六",
    "礼拜六": "周六",
    "周日": "周日",
    "星期日": "周日",
    "周天": "周日",
}

#: 「某天不要飞」。`别|不要|不安排|避开` 覆盖实测里出现过的说法。
_FORBID_DAY: Final[re.Pattern[str]] = re.compile(
    r"(周[一二三四五六日天]|星期[一二三四五六日]|礼拜[一二三四五六日])"
    r"[^，。；、]{0,6}?(别|不要|不安排|避开|不排)"
)
#: 反过来的语序：「不要在周三安排」。
_FORBID_DAY_REV: Final[re.Pattern[str]] = re.compile(
    r"(别|不要|不安排|避开|不排)[^，。；、]{0,6}?"
    r"(周[一二三四五六日天]|星期[一二三四五六日]|礼拜[一二三四五六日])"
)
#: 「每天最多 N 个架次」/「一天最多一个」。
_DENSITY: Final[re.Pattern[str]] = re.compile(
    r"(?:每天|一天|每日)[^，。；、]{0,4}?最多[^，。；、]{0,4}?(\d+|一|两|三|四|五)\s*(?:个)?架次"
)
#: 「只用 AC10 和 AC27」/「用AC84」—— 指定机队。
#:
#: 前面的单字负向词也必须排除：旧表达式会从「别用 AC73」里的「用」重新起配，
#: 把禁用 AC73 翻成只用 AC73。这里宁可漏掉一种自由表述交给 Planner，也不能
#: 产生方向相反的求解输入。
_PIN_AIRCRAFT: Final[re.Pattern[str]] = re.compile(
    r"(?<!别)(?<!不)(?<!要)(?<!止)(?<!免)(?:只)?用\s*((?:AC\d+[、和，,\s]*)+)"
)
#: 「JL-8 的架次都走 RWY-2」。只出现跑道编号不代表指定跑道：
#: 「RWY-2 关闭」是禁用，不能翻成 PIN_RUNWAY。
_PIN_RUNWAY: Final[re.Pattern[str]] = re.compile(
    r"(?:都|全部)?(?:改)?(?:走|排(?:到|在)?|使用)\s*(RWY-\d+)"
)
#: 「都安排在上午」/「都排在 08:00 之后」/「早上 8 点以后」。
_SHIFT_MORNING: Final[re.Pattern[str]] = re.compile(r"都?(?:安排|排)?在?上午")
_PIN_AFTER_TIME: Final[re.Pattern[str]] = re.compile(
    r"(\d{1,2})[:：](\d{2})\s*(?:之后|以后|后)"
    r"(?![^，。；、]{0,4}(?:不飞|别飞|停飞|不排))"
)

_CN_NUM: Final[dict[str, int]] = {"一": 1, "两": 2, "三": 3, "四": 4, "五": 5}


def _as_int(token: str) -> int:
    return _CN_NUM.get(token, 0) or int(token)


def scan_modifiers(text: str, *, round_no: int = 1) -> list[IncrementalConstraint]:
    """从原话里扫出约束修饰。**抓不到就返回空，绝不猜**。

    `targets` 一律填 `["ALL"]`：这些是**对整周所有人**的限制
    （「周三不要安排飞行」不是针对某个人）。真正针对个人的修饰由模型补，
    本扫描器不去推断对象 —— 推错对象比漏抓更糟。
    """
    found: list[IncrementalConstraint] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, params: dict[str, object], surface: str) -> None:
        key = (kind, str(sorted(params.items())))
        if key in seen:
            return
        seen.add(key)
        found.append(
            IncrementalConstraint(
                kind=kind,  # type: ignore[arg-type]
                targets=["ALL"],
                params=params,
                origin_utterance=surface,
                round_no=round_no,
            )
        )

    for pattern, day_group in ((_FORBID_DAY, 1), (_FORBID_DAY_REV, 2)):
        for m in pattern.finditer(text):
            day = _WEEKDAYS.get(m.group(day_group))
            if day:
                add("FORBID", {"weekday": day}, m.group(0))

    for m in _DENSITY.finditer(text):
        n = _as_int(m.group(1))
        if n:
            add("REDUCE_DENSITY", {"max_per_day": n}, m.group(0))

    for m in _PIN_AIRCRAFT.finditer(text):
        ids = re.findall(r"AC\d+", m.group(1))
        if ids:
            add("PIN_RESOURCE", {"aircraft": sorted(set(ids))}, m.group(0))

    for m in _PIN_RUNWAY.finditer(text):
        add("PIN_RUNWAY", {"runway_id": m.group(1)}, m.group(0))

    for m in _PIN_AFTER_TIME.finditer(text):
        add("PIN_TIME", {"not_before": f"{int(m.group(1)):02d}:{m.group(2)}"}, m.group(0))

    if _SHIFT_MORNING.search(text):
        add("SHIFT_WINDOW", {"prefer": "上午"}, "上午")

    return found


__all__ = ["scan_modifiers"]
