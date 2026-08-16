"""多轮计划修订：NL → 增量约束注入（v6 §7.3.4）。

```
用户："给所有人排班"              → SolveIntent(scope=ALL, freeze=BALANCED)  → 方案 v1
用户："周三上午太挤了，挪两个到下午" → translate_revision                       → 方案 v2
用户："何超那个换成 AC49"          → translate_revision                       → 方案 v3
用户："行，就这样"                → human_gate → 归档
```

## 四条硬性设计（v6 §7.3.4），逐条落在哪

| # | 设计 | 落点 |
|---|---|---|
| 1 | 增量约束是**求解器输入**，不是结果修改 | 产物是 `IncrementalConstraint`，由 `compile_spec` 带进模型，仍走完整 `solve → validate` |
| 2 | **可撤销** | :class:`RevisionStack`，`origin_utterance` 保留原话 |
| 3 | **不可行即回滚并解释**，不静默丢弃 | 图的修订循环在 INFEASIBLE 时 `undo()` + FTS-3005（`graph.py`） |
| 4 | 翻译结果**必须回显确认** | :attr:`RevisionTranslation.echo`，这一步不能省 |

## `PIN_RUNWAY` 是 v6 新增的一支，也是最容易出事的一支

S-05 把跑道变成求解决策变量，于是「这几个都走 2 号跑道」成了可翻译的诉求。
但 **RWY-2 只服务 JL-8**（v6 §1.3.5）：目标架次里只要有一架 JL-9，这条约束
就不可满足。本模块在翻译阶段就做一次**预检**，把这件事写进回显文案——用户在
点「确认」之前就该看到「AC84 是 JL-9，走不了 2 号跑道」，而不是等一轮求解回来
才被告知不可行。预检**不改变翻译结果**：约束照常产出、照常求解、照常按第 3 条
回滚，预检只是把解释提前。

## 两条翻译路径，不是二选一

- **LLM 路径**（主）：受约束解码到六种 `kind` + 槽位，带 few-shot。语义映射
  是 LLM 在本系统里不可替代的位置（v6 §7.1.6）。
- **规则路径**（降级）：五种规范表述的确定性匹配。**FTS-4001 时它接管**——
  LLM 挂了，修订能力退化但不消失。它同时是 `translate_revision` 工具的实现。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import time
from typing import Any, Final, Literal

from backend.core.errors import FTSError, LLMSchemaError
from backend.harness import AgentSpec, ContextBlock, Harness
from backend.routing.entities import EntityDirectory, resolve_aircraft, resolve_person
from backend.schemas.intent import ConstraintSpec, IncrementalConstraint, RevisionKind
from backend.schemas.plan import SchedulePlan, Sortie

#: 六种增量约束（v6 §7.3.4，`PIN_RUNWAY` 为 v6 新增）
REVISION_KINDS: Final[tuple[RevisionKind, ...]] = (
    "FORBID",
    "PIN_TIME",
    "PIN_RESOURCE",
    "SHIFT_WINDOW",
    "REDUCE_DENSITY",
    "PIN_RUNWAY",
)

#: 受约束解码的输出形状。`targets` 里放的是**原文表述或已知编号**，
#: 人名一律由 `resolve_person` 消解 —— 模型不得自己写 `person_id`。
REVISION_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(REVISION_KINDS)},
        "targets": {"type": "array", "items": {"type": "string"}},
        "params": {"type": "object"},
    },
    "required": ["kind", "targets"],
}

_WEEKDAYS: Final[tuple[str, ...]] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 英文缩写 ↔ 中文星期。few-shot 里用的是业务方确认过的 `WED`/`FRI` 写法，
#: 内部一律归一到 `Sortie.weekday` 的中文形态，两边都认。
_WEEKDAY_ALIASES: Final[dict[str, str]] = {
    "MON": "周一",
    "TUE": "周二",
    "WED": "周三",
    "THU": "周四",
    "FRI": "周五",
    "SAT": "周六",
    "SUN": "周日",
    "周天": "周日",
    "星期一": "周一",
    "星期二": "周二",
    "星期三": "周三",
    "星期四": "周四",
    "星期五": "周五",
    "星期六": "周六",
    "星期日": "周日",
    "星期天": "周日",
}

#: 半日窗口（v6 §1.3.2 的训练窗 06:00-18:00 一分为二）
_HALF_DAY: Final[dict[str, str]] = {
    "上午": "06:00-12:00",
    "早上": "06:00-12:00",
    "下午": "12:00-18:00",
    "晚上": "12:00-18:00",
}

_CN_NUMERALS: Final[dict[str, int]] = {
    "一": 1,
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


# ─────────────────────────────────────────────────────────────────────
# few-shot（业务方 2026-08-13 确认：五条正例逐字取自 v6 §7.3.4 映射表，
# 第六条为 PIN_RUNWAY × JL-9 的负例）
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RevisionExample:
    utterance: str
    kind: RevisionKind
    targets: list[str]
    params: dict[str, Any]
    note: str = ""


FEW_SHOT: Final[tuple[RevisionExample, ...]] = (
    RevisionExample(
        utterance="周三上午太挤了，挪两个到下午",
        kind="REDUCE_DENSITY",
        targets=["周三"],
        params={"day": "WED", "window": "06:00-12:00", "delta": -2},
    ),
    RevisionExample(
        utterance="何超那个换成 AC49",
        kind="PIN_RESOURCE",
        targets=["何超"],
        params={"aircraft": "AC49"},
        note="targets 写原文表述「何超」，编号由 resolve_person 消解，不要自己写 P08",
    ),
    RevisionExample(
        utterance="刘斌周五别排了",
        kind="FORBID",
        targets=["刘斌"],
        params={"day": "FRI"},
    ),
    RevisionExample(
        utterance="早点飞",
        kind="SHIFT_WINDOW",
        targets=["ALL"],
        params={"latest": "09:00"},
    ),
    RevisionExample(
        utterance="这几个都走 2 号跑道",
        kind="PIN_RUNWAY",
        targets=["ALL"],
        params={"runway": "RWY-2"},
    ),
    RevisionExample(
        utterance="AC84 那班也走 2 号跑道",
        kind="PIN_RUNWAY",
        targets=["AC84"],
        params={"runway": "RWY-2"},
        note=(
            "照常翻译。RWY-2 只服务 JL-8 而 AC84 是 JL-9，这条约束求解时会判不可行，"
            "届时按 §7.3.4 第 3 条回滚上一版并解释，不在翻译阶段私自改成别的跑道"
        ),
    ),
)


def few_shot_block() -> str:
    """把 few-shot 渲染成一个上下文块。

    渲染成 JSON 而不是散文，是因为输出本身就是受约束解码的 JSON——示例与产物
    同形，模型少做一次格式迁移。
    """
    lines = ["以下是六个已确认的翻译示例（第六个是不可满足的负例，照常翻译）："]
    for i, ex in enumerate(FEW_SHOT, start=1):
        payload = json.dumps(
            {"kind": ex.kind, "targets": ex.targets, "params": ex.params}, ensure_ascii=False
        )
        lines.append(f"{i}. 用户：「{ex.utterance}」\n   输出：{payload}")
        if ex.note:
            lines.append(f"   注意：{ex.note}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# 修订栈（第 2 条硬性设计：可撤销）
# ─────────────────────────────────────────────────────────────────────
@dataclass
class RevisionStack:
    """多轮修订栈。`undo` 弹出最后一条重解（v6 §7.3.4 第 2 条）。"""

    items: list[IncrementalConstraint] = field(default_factory=list)

    @classmethod
    def from_state(cls, items: Sequence[IncrementalConstraint]) -> RevisionStack:
        return cls(items=list(items))

    @property
    def round_no(self) -> int:
        """下一轮的轮次号。轮次从 1 起（`IncrementalConstraint.round_no` 有 `ge=1`）。"""
        return len(self.items) + 1

    def push(self, constraint: IncrementalConstraint) -> RevisionStack:
        self.items.append(constraint)
        return self

    def undo(self) -> IncrementalConstraint | None:
        """弹出最后一条。空栈返回 None —— 没得撤销不是错误。"""
        return self.items.pop() if self.items else None

    def undo_many(self, times: int) -> list[IncrementalConstraint]:
        """连撤 `times` 次，返回被弹出的约束（**按弹出顺序**，即从新到旧）。

        栈里不够撤时**撤到空为止**，不抛 —— 用户说「都撤了吧」而栈里只有两条，
        正确的行为是撤两条，不是报错。
        """
        popped: list[IncrementalConstraint] = []
        for _ in range(max(0, times)):
            item = self.undo()
            if item is None:
                break
            popped.append(item)
        return popped

    def utterances(self) -> list[str]:
        """按轮次列出原话，供 UI 展示「您先后说了什么」。"""
        return [c.origin_utterance for c in self.items]

    def version_no(self) -> int:
        """当前方案的版本号。首轮方案是 v1，每条修订 +1。

        栈里 2 条 → 当前是 v3。`undo` 两次后栈里 0 条 → 回到 v1。
        **这是 UI 上「方案 vN」那个 N 的唯一定义点**，不要在别处再算一次。
        """
        return len(self.items) + 1


# ─────────────────────────────────────────────────────────────────────
# 翻译产物
# ─────────────────────────────────────────────────────────────────────
TranslationSource = Literal["llm", "rule"]


@dataclass(frozen=True)
class RevisionTranslation:
    """一次修订翻译的完整产物。"""

    constraint: IncrementalConstraint
    echo: str
    source: TranslationSource
    warnings: tuple[str, ...] = ()
    llm_calls: int = 0

    @property
    def infeasible_hint(self) -> bool:
        """预检发现这条约束多半解不出来。**不阻断**，只是把话说在前面。"""
        return bool(self.warnings)


# ─────────────────────────────────────────────────────────────────────
# 归一化与目标消解
# ─────────────────────────────────────────────────────────────────────
def normalize_weekday(value: str) -> str:
    """`WED` / `星期三` / `周三` → `周三`。认不出就原样返回，由下游判非法。"""
    text = value.strip()
    if text in _WEEKDAYS:
        return text
    return _WEEKDAY_ALIASES.get(text.upper(), _WEEKDAY_ALIASES.get(text, text))


def _cn_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    if len(token) == 1:
        return _CN_NUMERALS.get(token)
    return None


def resolve_targets(
    surfaces: Sequence[str],
    *,
    plan: SchedulePlan | None,
    directory: EntityDirectory | None,
) -> tuple[list[str], list[str]]:
    """把 `targets` 里的表述消解成 `sortie_id` / `person_id` / `aircraft_id`。

    返回 (已消解的目标, 警告)。**消解不了的表述不静默丢弃**——它会进警告，
    随回显一起给用户看。`ALL` 原样保留：它是「本次范围内全部架次」的合法写法。
    """
    resolved: list[str] = []
    warnings: list[str] = []
    known_sorties = {s.sortie_id for s in plan.sorties} if plan else set()

    for surface in surfaces:
        text = surface.strip()
        if not text:
            continue
        if text == "ALL" or text in known_sorties or text in _WEEKDAYS:
            resolved.append(text)
            continue
        if directory is None:
            warnings.append(f"「{text}」无法消解：当前没有实体名录")
            continue
        person = resolve_person(text, directory)
        if person.resolved and person.entity_id is not None:
            resolved.append(person.entity_id)
            continue
        aircraft = resolve_aircraft(text, directory)
        if aircraft.resolved and aircraft.entity_id is not None:
            resolved.append(aircraft.entity_id)
            continue
        warnings.append(person.question() if person.ambiguous else f"「{text}」在当前快照里查不到")

    # 去重但保序：同一个人被提到两次不该产生两条同样的目标
    deduped: list[str] = []
    for item in resolved:
        if item not in deduped:
            deduped.append(item)
    return deduped, warnings


def sorties_for_targets(plan: SchedulePlan | None, targets: Sequence[str]) -> list[Sortie]:
    """把目标展开成具体架次，供预检与回显用。"""
    if plan is None:
        return []
    if "ALL" in targets:
        return list(plan.sorties)
    wanted = set(targets)
    out: list[Sortie] = []
    for sortie in plan.sorties:
        if (
            sortie.sortie_id in wanted
            or sortie.aircraft_id in wanted
            or any(c.person_id in wanted for c in sortie.crew)
            or sortie.weekday in wanted
        ):
            out.append(sortie)
    return out


# ─────────────────────────────────────────────────────────────────────
# 人话形状 → 求解器线格式
# ─────────────────────────────────────────────────────────────────────
#
# **两套 params 键名是刻意的，不是疏忽。**
#
# - **人话形状**（`{"day": "周三", "window": "06:00-12:00", "delta": -2}`）是
#   v6 §7.3.4 映射表的写法，也是 few-shot 里业务方确认过的写法。它进
#   `origin_utterance` 旁边、进回显文案、进审计——**人要看得懂**。
# - **线格式**（`{"day_index": 2, "max_takeoffs_per_day": 5}`）是 M2-A 的
#   `solver/model.py::post_incremental` 认的键名。它按分钟数与 0~6 的日索引
#   说话，因为模型里的时间就是「当日 window_start 起的分钟数」。
#
# 中间必须有一次显式转换。**没有它就是静默失效**：`{"runway": "RWY-2"}` 传给
# 只认 `runway_id` 的编码器，取到空串 → 目标候选全被判 `x=0` → 整轮不可行，
# 而日志上看不出任何异常。
def _weekday_index(day: str) -> int | None:
    normalized = normalize_weekday(day)
    return _WEEKDAYS.index(normalized) if normalized in _WEEKDAYS else None


def _clock_to_minutes(clock: str, *, window_start: time) -> int | None:
    """`"09:00"` → 距 `window_start` 的分钟数。窗口外或格式不对返回 None。"""
    try:
        hour, minute = (int(part) for part in clock.split(":", 1))
    except (ValueError, AttributeError):
        return None
    base = window_start.hour * 60 + window_start.minute
    return (hour * 60 + minute) - base


def to_solver_params(
    constraint: IncrementalConstraint,
    *,
    window_start: time,
    day_counts: Mapping[int, int] | None = None,
    horizon_minutes: int,
) -> dict[str, Any]:
    """把人话形状的 params 译成求解器线格式。

    `day_counts` 是**当前方案里每天的架次数**，`REDUCE_DENSITY` 要用它把
    「减少 2 个」换算成绝对上限（求解器认的是上限，不是增量）。拿不到就退回
    「不设上限」而不是猜一个——猜低了会凭空砍掉别人的架次。
    """
    params = dict(constraint.params)
    out: dict[str, Any] = {}

    if "day" in params:
        index = _weekday_index(str(params["day"]))
        if index is not None:
            out["day_index"] = index

    if constraint.kind == "PIN_RUNWAY":
        out["runway_id"] = str(params.get("runway", params.get("runway_id", "")))
    elif constraint.kind == "PIN_RESOURCE":
        out["aircraft_id"] = str(params.get("aircraft", params.get("aircraft_id", "")))
    elif constraint.kind == "PIN_TIME":
        minute = _clock_to_minutes(str(params.get("takeoff", "")), window_start=window_start)
        if minute is not None:
            out["takeoff_minute"] = minute
    elif constraint.kind == "SHIFT_WINDOW":
        if "earliest" in params:
            minute = _clock_to_minutes(str(params["earliest"]), window_start=window_start)
            if minute is not None:
                out["earliest_minute"] = max(0, minute)
        if "latest" in params:
            minute = _clock_to_minutes(str(params["latest"]), window_start=window_start)
            if minute is not None:
                out["latest_minute"] = min(horizon_minutes, minute)
    elif constraint.kind == "REDUCE_DENSITY":
        delta = int(params.get("delta", -1))
        current = (day_counts or {}).get(int(out.get("day_index", -1)), None)
        if current is not None:
            out["max_takeoffs_per_day"] = max(0, current + delta)
    return out


def for_solver(
    constraint: IncrementalConstraint,
    *,
    window_start: time,
    plan: SchedulePlan | None = None,
    horizon_minutes: int,
) -> IncrementalConstraint:
    """产出一条**求解器能直接吃**的增量约束（人话 params 换成线格式）。

    `origin_utterance` 与 `round_no` 原样保留 —— 撤销与审计靠它们，换了形状
    就对不上用户说过的话了。
    """
    counts: dict[int, int] = {}
    if plan is not None:
        for sortie in plan.sorties:
            counts[(sortie.date - plan.week_start).days] = (
                counts.get((sortie.date - plan.week_start).days, 0) + 1
            )
    return constraint.model_copy(
        update={
            "params": to_solver_params(
                constraint,
                window_start=window_start,
                day_counts=counts,
                horizon_minutes=horizon_minutes,
            )
        }
    )


# ─────────────────────────────────────────────────────────────────────
# 预检：PIN_RUNWAY × JL-9
# ─────────────────────────────────────────────────────────────────────
def check_runway_feasibility(
    constraint: IncrementalConstraint,
    *,
    plan: SchedulePlan | None,
    spec: ConstraintSpec | None,
    directory: EntityDirectory | None,
) -> list[str]:
    """`PIN_RUNWAY` 的机型预检（v6 §7.3.4 `PIN_RUNWAY` 行的脚注）。

    判据是 `ConstraintSpec.runways`（跑道 → 服务机型）与名录里的机型，**不是
    写死的「RWY-2 只服务 JL-8」**：跑道与机型都由上传数据决定（CLAUDE.md §11）。
    基准数据下它恰好等价于那句话，换一批数据就自动跟着变。
    """
    if constraint.kind != "PIN_RUNWAY":
        return []
    runway = str(constraint.params.get("runway", "")).strip()
    if not runway or spec is None or directory is None:
        return []
    served = set(spec.runways.get(runway, ()))
    if not served:
        return [f"{runway} 不在本次规格的跑道表里（可用：{sorted(spec.runways)}）"]

    blocked: list[str] = []
    for sortie in sorties_for_targets(plan, constraint.targets):
        aircraft_type = directory.aircraft.get(sortie.aircraft_id, "")
        if aircraft_type and aircraft_type not in served:
            blocked.append(
                f"{sortie.sortie_id}（{sortie.aircraft_id} 是 {aircraft_type}）"
                f"—— {runway} 只服务 {sorted(served)}"
            )
    if not blocked:
        return []
    return [
        f"这条要求多半解不出来：{'；'.join(blocked)}。"
        "若求解确认不可行，系统会回滚到上一版方案并给出冲突项（FTS-3005）。"
    ]


# ─────────────────────────────────────────────────────────────────────
# 规则路径（降级 + `translate_revision` 工具的实现）
# ─────────────────────────────────────────────────────────────────────
_RE_AIRCRAFT_SWAP = re.compile(r"(?:换成|改成|改用|换到)\s*(AC\d+|\d+\s*号机)", re.IGNORECASE)
_RE_RUNWAY = re.compile(r"(?:走|用|改到)\s*(?:(RWY-\d+)|(\d+)\s*号跑道)", re.IGNORECASE)
_RE_FORBID = re.compile(r"(别排|不排|不要排|别安排|不用排)")
_RE_DENSITY = re.compile(r"(挤|太多|太满|太密)")
_RE_MOVE_N = re.compile(r"挪\s*(\d+|[一两二三四五六七八九十])\s*(?:个|架|班)?")
_RE_TIME = re.compile(r"(\d{1,2})\s*[:：点]\s*(\d{0,2})")


def _find_weekday(text: str) -> str | None:
    for day in _WEEKDAYS:
        if day in text:
            return day
    if "周天" in text:
        return "周日"
    return None


def _find_half_day(text: str) -> str | None:
    for phrase, window in _HALF_DAY.items():
        if phrase in text:
            return window
    return None


def _find_person_surfaces(text: str, directory: EntityDirectory | None) -> list[str]:
    """逐字扫描话里出现的已知人名/编号。规则路径只做精确匹配，不猜。"""
    if directory is None:
        return []
    found = [pid for pid in sorted(directory.persons) if pid in text]
    found += [name for _, name in sorted(directory.persons.items()) if name and name in text]
    return found


def rule_translate(
    utterance: str,
    *,
    round_no: int,
    plan: SchedulePlan | None = None,
    directory: EntityDirectory | None = None,
) -> IncrementalConstraint | None:
    """五种规范表述的确定性翻译。认不出返回 None —— **不瞎猜一个 kind**。

    这条路径的定位是「LLM 不可用时仍能处理规范表述」，不是「替代 LLM」。
    认不出时返回 None，由调用方走 FTS-4001 的表单追问，比翻译成一条错约束
    然后排出一版没人要的方案好得多。
    """
    text = utterance.strip()
    if not text:
        return None
    persons = _find_person_surfaces(text, directory)
    day = _find_weekday(text)

    def build(
        kind: RevisionKind, targets: list[str], params: dict[str, Any]
    ) -> IncrementalConstraint:
        resolved, _ = resolve_targets(targets or ["ALL"], plan=plan, directory=directory)
        return IncrementalConstraint(
            kind=kind,
            targets=resolved or ["ALL"],
            params=params,
            origin_utterance=utterance,
            round_no=round_no,
        )

    runway = _RE_RUNWAY.search(text)
    if runway is not None:
        rid = runway.group(1) or f"RWY-{runway.group(2)}"
        targets = persons or list(re.findall(r"AC\d+", text.upper()))
        return build("PIN_RUNWAY", targets, {"runway": rid.upper()})

    swap = _RE_AIRCRAFT_SWAP.search(text)
    if swap is not None:
        token = swap.group(1).upper().replace(" ", "")
        aircraft_id = token if token.startswith("AC") else f"AC{re.sub(r'[^0-9]', '', token)}"
        return build("PIN_RESOURCE", persons, {"aircraft": aircraft_id})

    if _RE_FORBID.search(text) is not None:
        params: dict[str, Any] = {}
        if day is not None:
            params["day"] = day
        return build("FORBID", persons, params)

    if _RE_DENSITY.search(text) is not None or _RE_MOVE_N.search(text) is not None:
        moved = _RE_MOVE_N.search(text)
        delta = -(_cn_int(moved.group(1)) or 1) if moved is not None else -1
        window = _find_half_day(text) or "06:00-18:00"
        params = {"window": window, "delta": delta}
        if day is not None:
            params["day"] = day
        return build("REDUCE_DENSITY", [day] if day else [], params)

    if "早点" in text or "提前" in text:
        clock = _RE_TIME.search(text)
        latest = _fmt_clock(clock) if clock is not None else "09:00"
        return build("SHIFT_WINDOW", persons, {"latest": latest})
    if "晚点" in text or "推后" in text or "往后" in text:
        clock = _RE_TIME.search(text)
        earliest = _fmt_clock(clock) if clock is not None else "13:00"
        return build("SHIFT_WINDOW", persons, {"earliest": earliest})

    clock = _RE_TIME.search(text)
    if clock is not None and ("固定" in text or "定在" in text or "就排在" in text):
        return build("PIN_TIME", persons, {"takeoff": _fmt_clock(clock)})

    return None


def _fmt_clock(match: re.Match[str]) -> str:
    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) else 0
    return f"{hour:02d}:{minute:02d}"


# ─────────────────────────────────────────────────────────────────────
# LLM 路径
# ─────────────────────────────────────────────────────────────────────
REVISION_AGENT: Final[AgentSpec] = AgentSpec(
    name="planner",
    tools=(),
    requires_tool_call=False,
    output_schema=REVISION_OUTPUT_SCHEMA,
)


def _parse_revision_payload(text: str) -> tuple[RevisionKind, list[str], dict[str, Any]]:
    payload = json.loads(text)
    kind = payload.get("kind", "")
    if kind not in REVISION_KINDS:
        raise ValueError(f"kind 必须是 {REVISION_KINDS} 之一，实际 {kind!r}")
    targets = [str(t) for t in payload.get("targets", []) if str(t).strip()]
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError(f"params 必须是对象，实际 {type(params).__name__}")
    if "day" in params:
        params["day"] = normalize_weekday(str(params["day"]))
    return kind, targets, dict(params)


# ─────────────────────────────────────────────────────────────────────
# 用户发起的 undo（v6 §7.3.4 第 2 条硬性设计的入口）
# ─────────────────────────────────────────────────────────────────────
#
# 「`undo` 弹出最后一条重解」这句话在 v6 里定义了**机制**，没定义**入口**。
# 本窗口把入口做成「修订轮里的一种表述」而不是人工门禁上的第四种决策：
#
# - 人工门禁的三种决策（APPROVE / REVISE / REJECT）是 v6 §7.2.4 的规格表，
#   加第四种要改设计方案（CLAUDE.md §7 第 8 条：改 docs 要先问）；
# - 而「撤销刚才那条」本来就是用户在修订轮里说的一句话，与「换成 AC49」
#   在交互上是同一个位置。**能不改规格就不改。**
#
# 撤销**同样要走完整的 `solve → validate`**（第 1 条硬性设计）：弹掉一条约束
# 之后的方案是重解出来的，不是缓存回放的。这一点与「加一条约束」完全对称。

#: 撤销表述。**只认明确的撤销词** —— 「算了」「不要了」太含糊，
#: 含糊的话按翻译不出来处理（抛「这句没能翻译成增量约束」），不猜。
_UNDO_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"撤销|撤回|连撤|回退|退回|恢复到上一?版|还原"),
    re.compile(r"^\s*undo\b", re.IGNORECASE),
    re.compile(r"上一?版|前一?版"),
)

#: 「撤销两次」「连撤 3 条」里的次数
_UNDO_TIMES: Final[re.Pattern[str]] = re.compile(
    r"(\d+|[一两二三四五六七八九十])\s*(?:次|条|步|版)"
)


def undo_times(utterance: str) -> int:
    """这句话要求撤销几次？**不是撤销请求返回 0。**

    「撤销」→ 1；「撤销两次」→ 2；「回到 v2」这类**按版本号**的说法不认 ——
    版本号要减去当前版本才知道撤几次，而那是调用方（拿得到栈）才知道的事，
    在这里算等于把状态偷偷带进一个纯函数。
    """
    text = utterance.strip()
    if not text or not any(p.search(text) for p in _UNDO_PATTERNS):
        return 0
    match = _UNDO_TIMES.search(text)
    if match is None:
        return 1
    return _cn_int(match.group(1)) or 1


def undo_echo(popped: Sequence[IncrementalConstraint], stack: RevisionStack) -> str:
    """撤销的回显文案（v6 §7.3.4 第 4 条：翻译结果必须回显确认）。

    **把撤掉的原话逐条列出来**，用户才能确认撤对了 —— 「撤销了 2 条」
    这种回执看不出撤的是不是他想撤的那两条。
    """
    if not popped:
        return "当前没有可撤销的修订，方案保持不变。"
    lines = [f"我理解为：撤销最近 {len(popped)} 条修订，重新求解。"]
    for item in popped:
        lines.append(f"  - 撤销第 {item.round_no} 轮：「{item.origin_utterance}」（{item.kind}）")
    remaining = stack.utterances()
    if remaining:
        kept = "；".join(f"第 {i} 轮「{u}」" for i, u in enumerate(remaining, start=1))
        lines.append(f"保留的修订：{kept}")
    else:
        lines.append("撤销后回到首轮方案（v1），不带任何增量约束。")
    lines.append(f"撤销后的方案版本：v{stack.version_no()}")
    return "\n".join(lines)


def translate_revision(
    utterance: str,
    *,
    round_no: int,
    harness: Harness | None = None,
    plan: SchedulePlan | None = None,
    directory: EntityDirectory | None = None,
    spec: ConstraintSpec | None = None,
) -> RevisionTranslation:
    """把一句修订原话翻译成 `IncrementalConstraint`（v6 §7.3.4）。

    先 LLM 后规则；两条路径产出的约束形状完全一致，下游分不出也不需要分。
    """
    warnings: list[str] = []
    llm_calls = 0
    constraint: IncrementalConstraint | None = None
    source: TranslationSource = "rule"

    if harness is not None:
        try:
            out = harness.call(
                REVISION_AGENT,
                [
                    ContextBlock(kind="decision", content=few_shot_block(), label="few_shot"),
                    ContextBlock(kind="summary", content=_plan_summary(plan), label="plan"),
                    ContextBlock(kind="history", content=utterance, role="user"),
                ],
            )
            llm_calls = out.llm_calls
            if not out.degraded:
                kind, surfaces, params = _parse_revision_payload(out.text)
                targets, target_warnings = resolve_targets(surfaces, plan=plan, directory=directory)
                warnings.extend(target_warnings)
                constraint = IncrementalConstraint(
                    kind=kind,
                    targets=targets or ["ALL"],
                    params=params,
                    origin_utterance=utterance,
                    round_no=round_no,
                )
                source = "llm"
            else:
                warnings.append(f"LLM 修订翻译降级（{out.error_code}），已改用规则路径")
        except (FTSError, ValueError, json.JSONDecodeError) as exc:
            warnings.append(f"LLM 修订翻译失败（{exc}），已改用规则路径")

    if constraint is None:
        constraint = rule_translate(utterance, round_no=round_no, plan=plan, directory=directory)
        source = "rule"

    if constraint is None:
        raise LLMSchemaError(
            f"这句修订没能翻译成增量约束：「{utterance}」。"
            "请换一种说法，或直接指出要改的架次号与目标（如「S000117 换 AC49」）",
            severity="WARN",
            stage="intent",
            details={"utterance": utterance, "round_no": round_no},
            suggestions=[
                "支持的表述：禁排某人某天 / 换飞机 / 换跑道 / 减少某窗口起飞数 / 早点或晚点飞",
            ],
        )

    warnings.extend(check_runway_feasibility(constraint, plan=plan, spec=spec, directory=directory))
    return RevisionTranslation(
        constraint=constraint,
        echo=echo_text(constraint, plan=plan, directory=directory),
        source=source,
        warnings=tuple(warnings),
        llm_calls=llm_calls,
    )


def _plan_summary(plan: SchedulePlan | None) -> str:
    """给模型看的方案摘要。**只入摘要，明细由工具按需取**（v6 §7.7.1 第 5 行）。"""
    if plan is None:
        return "当前还没有已生成的方案（首轮排班）。"
    by_day: dict[str, int] = {}
    for sortie in plan.sorties:
        by_day[sortie.weekday] = by_day.get(sortie.weekday, 0) + 1
    spread = "、".join(f"{day} {count} 架次" for day, count in sorted(by_day.items()))
    return (
        f"当前方案 {plan.plan_id}（{plan.iso_week}）共 {len(plan.sorties)} 个架次：{spread}。"
        f"松弛档 Tier{plan.relaxation_tier}，欠账 {len(plan.debts)} 条。"
    )


def echo_text(
    constraint: IncrementalConstraint,
    *,
    plan: SchedulePlan | None = None,
    directory: EntityDirectory | None = None,
) -> str:
    """回显确认文案（v6 §7.3.4 第 4 条，**这一步不能省**）。

    句式固定为「我理解为：……」，与 v6 §7.3.4 的原文一致。带上人名而不只是
    编号——用户说的是「何超」，回显成 `P08` 他没法确认对不对。
    """
    labels = _target_labels(constraint.targets, directory=directory, plan=plan)
    who = "、".join(labels) if labels else "本次范围内全部架次"
    params = constraint.params

    if constraint.kind == "REDUCE_DENSITY":
        day = params.get("day", "")
        window = params.get("window", "全天")
        delta = int(params.get("delta", -1))
        return f"我理解为：{day} {window} 减少 {abs(delta)} 个起飞"
    if constraint.kind == "PIN_RESOURCE":
        return f"我理解为：{who} 的架次改用 {params.get('aircraft', '（未指定机号）')}"
    if constraint.kind == "PIN_RUNWAY":
        return f"我理解为：{who} 的架次都走 {params.get('runway', '（未指定跑道）')}"
    if constraint.kind == "FORBID":
        day = params.get("day", "")
        return f"我理解为：{who} {day or '本周'}不安排架次"
    if constraint.kind == "SHIFT_WINDOW":
        if "latest" in params:
            return f"我理解为：{who} 的架次不晚于 {params['latest']} 起飞"
        return f"我理解为：{who} 的架次不早于 {params.get('earliest', '（未指定）')} 起飞"
    return f"我理解为：{who} 的起飞时刻固定在 {params.get('takeoff', '（未指定）')}"


def _target_labels(
    targets: Sequence[str],
    *,
    directory: EntityDirectory | None,
    plan: SchedulePlan | None,
) -> list[str]:
    out: list[str] = []
    for target in targets:
        if target == "ALL":
            continue
        if directory is not None and target in directory.persons:
            out.append(f"{directory.persons[target]}({target})")
        elif directory is not None and target in directory.aircraft:
            out.append(target)
        elif plan is not None and any(s.sortie_id == target for s in plan.sorties):
            out.append(f"架次 {target}")
        else:
            out.append(target)
    return out


__all__ = [
    "FEW_SHOT",
    "REVISION_AGENT",
    "REVISION_KINDS",
    "REVISION_OUTPUT_SCHEMA",
    "RevisionExample",
    "RevisionStack",
    "RevisionTranslation",
    "TranslationSource",
    "check_runway_feasibility",
    "echo_text",
    "few_shot_block",
    "for_solver",
    "normalize_weekday",
    "resolve_targets",
    "rule_translate",
    "sorties_for_targets",
    "to_solver_params",
    "translate_revision",
    "undo_echo",
    "undo_times",
]
