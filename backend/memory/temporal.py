"""记忆时效性与冲突消解（v6 §6.4）。

> 长期记忆最大的坑不是「召回不到」，而是「召回到过期版本」——
> 召回到过期资质会直接导致排班违规。

四件事，本模块各占一节：

| § | 机制 | 落点 |
|---|---|---|
| 6.4 ① | `valid_from` / `valid_to` / `superseded_by`，检索默认加时间过滤 | :func:`active_at` |
| 6.4 ② | 同 key 多版本 → 最新有效版本 + **显式标注历史版本数量** | :func:`latest_version` |
| 6.4 ③ | 写入冲突检测，按来源可信度排序，无法自动裁决**升级人工** | :class:`MemoryConflict` / :func:`resolve_conflict` |
| 6.4 ④ | 遗忘：情景记忆超 3 个训练周期归档到冷表，可检索但不参与默认召回 | :func:`archive_horizon` |

## 「按可信度排序」不等于「高的赢」

v6 §6.4 给的序是 **PG 事实 > 排班确认记录 > 对话推断**。三档之间是**严格**的：
高档覆盖低档，低档写不进来（但要留痕，不是静默丢弃）。**同档相撞则升级人工**
——两条同样可信、内容互斥的记忆，系统没有任何依据去挑一个，挑了就是猜。
这条对应 FTS-2001（数据完整性/冲突）。

## 刘斌的 C 类资质是这套机制的活样本

2026-01-07 之前它是「有效资质」，之后是「到期待复训」。同一个问题
「刘斌能不能飞仪表课目」在 01-06 与 01-09 两个时点必须给出**不同理由**的答案
（S-11 之下两次都是「能」：前者正常执行，后者强制复训）。
落点在 `retrieval/structured.py::qualification_facts`，判据就是本模块的
:func:`active_at`。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Final, Generic, Protocol, TypeVar, runtime_checkable

from backend.core.errors import DataConflictError

# ─────────────────────────────────────────────────────────────────────
# ③ 来源可信度（v6 §6.4：PG 事实 > 排班确认记录 > 对话推断）
# ─────────────────────────────────────────────────────────────────────

#: PG 里的事实表（人员/飞机/课目/空域…），摄取经人工确认后落库
SOURCE_PG_FACT: Final[str] = "PG事实"
#: 排班确认记录：人工门禁 `APPROVE` 之后归档的方案与其附带结论
SOURCE_PLAN_CONFIRMED: Final[str] = "排班确认记录"
#: 对话推断：从用户说的话里蒸馏出来的偏好与表述映射
SOURCE_CONVERSATION: Final[str] = "对话推断"

#: 可信度序。**数字大的可信**，同值即同档。
SOURCE_TRUST: Final[dict[str, int]] = {
    SOURCE_PG_FACT: 3,
    SOURCE_PLAN_CONFIRMED: 2,
    SOURCE_CONVERSATION: 1,
}

#: 未登记的来源一律按最低档处理 —— **不是按最高档**。
#: 一个拼错的来源名不该获得覆盖 PG 事实的权力。
UNKNOWN_SOURCE_TRUST: Final[int] = 0


def trust_of(source: str) -> int:
    """来源 → 可信度。未登记的来源按最低档（0），不是按最高档。"""
    return SOURCE_TRUST.get(source, UNKNOWN_SOURCE_TRUST)


# ─────────────────────────────────────────────────────────────────────
# ① 时间过滤
# ─────────────────────────────────────────────────────────────────────
@runtime_checkable
class Versioned(Protocol):
    """带时效的记忆条目。`EpisodicMemory` / `ProceduralMemory` 都符合。"""

    memory_id: str
    valid_from: datetime
    valid_to: datetime | None
    superseded_by: str | None


#: `Versioned` 的类型变量。Python 3.11 没有 PEP 695 的 `def f[T]()` 语法，
#: 所以显式声明 —— 换 3.12 之后可以改写，但没有必要为此抬高解释器下限。
_V = TypeVar("_V", bound=Versioned)
_T = TypeVar("_T")


def _as_datetime(value: datetime | date) -> datetime:
    """把「提问时点」抬成 `datetime`，**光秃秃的 `date` 取当日末刻**。

    ## 为什么是末刻而不是 00:00（M9-B 实测改的）

    这里抬的是**提问时点**（`is_active_at` 的 `at`），不是条目的生效时刻 ——
    后者本来就带时分秒，走 `_naive()` 直接比较。

    「截至 D 日」在业务上**包含 D 日当天写下的东西**。取 00:00 等于把提问
    理解成「D 日刚开始那一瞬间」，于是当天写的一律判成「还没生效」。而记忆的
    `valid_from` 几乎都带钟点：`distill()` 落的是当日 18:00，情景记忆各有时刻。

    **M9-B 实测的代价**（`memory_320`，320 条探针）：

    | 症状 | 00:00 | 当日末刻 |
    |---|---|---|
    | 可见偏好条数 | **1 / 26** | 25 / 26 |
    | 第 20 周写入的情景记忆召回 | **0 / 5** | 5 / 5 |

    半开区间语义不受影响：`valid_to` 落在当天的条目仍然按
    `valid_to <= moment` 判失效 —— 「当天被取代」的版本在当天末刻确实已经不是
    最新版了，这正是要的行为。
    """
    if isinstance(value, datetime):
        return value
    return datetime.combine(value, time.max)


def _naive(value: datetime) -> datetime:
    """统一成 naive 再比较。

    PG 的 `TIMESTAMPTZ` 读回来带 tzinfo，而调用方常常拿一个 naive 的
    `datetime(2026, 1, 6)` 来问「这一天有效吗」。混着比会抛
    `can't compare offset-naive and offset-aware datetimes` —— 这不是
    防御性编程，是这套表结构与调用姿势的必然结果。
    """
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def is_active_at(item: Versioned, at: datetime | date) -> bool:
    """该条目在 `at` 时刻是否有效。

    区间语义与约束9 的窗口一致：**半开** `[valid_from, valid_to)`。
    统一半开是为了「上一版的 `valid_to` = 下一版的 `valid_from`」时不会两版
    同时有效 —— 那会让「最新有效版本」这句话失去意义。

    ## `superseded_by` 是链接，不是作废标记

    有效性**只由区间决定**。一条 2026-01-05 生效、01-12 被新版本取代的记忆，
    在 01-06 这天**依然是当时的正确答案** —— 「同一问题在两个时点给出不同
    答案」正是 §6.4 要的能力（刘斌的 C 类资质就是那个活样本）。
    把 `superseded_by` 当作作废标记，历史时点的查询会一律返回空。

    **唯一的例外**：`superseded_by` 有值而 `valid_to` 为空。那种行说不清自己
    什么时候不再成立，与其猜一个时点，不如一律判无效（写入方
    `procedural.put_preference` 是两个字段一起改的，出现这种行意味着有人只改了
    一半）。
    """
    if item.superseded_by is not None and item.valid_to is None:
        return False
    moment = _naive(_as_datetime(at))
    if _naive(item.valid_from) > moment:
        return False
    return not (item.valid_to is not None and _naive(item.valid_to) <= moment)


def active_at(items: Iterable[_V], at: datetime | date) -> list[_V]:
    """时间过滤（v6 §6.4「检索默认加时间过滤」）。"""
    return [item for item in items if is_active_at(item, at)]


# ─────────────────────────────────────────────────────────────────────
# ② 同 key 多版本
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class VersionView(Generic[_T]):
    """一个 key 的版本视图：最新有效版本 + 历史版本数量。

    `history_count` **必须呈现给用户**（v6 §6.4「显式标注历史版本数量」）：
    「刘斌的 C 类资质到期日是 2026-01-07（该条另有 1 个历史版本）」比
    「刘斌的 C 类资质到期日是 2026-01-07」诚实得多 —— 后者藏起了
    「这个值被改过」这件事，而 §5.5 的 X1 恰恰就是一次改动。
    """

    current: _T | None
    history_count: int

    @property
    def has_history(self) -> bool:
        return self.history_count > 0

    def note(self) -> str:
        """给回答用的一句话标注。没有历史版本时返回空串。"""
        return f"（该条另有 {self.history_count} 个历史版本）" if self.has_history else ""


def latest_version(items: Sequence[_V], at: datetime | date) -> VersionView[_V]:
    """同 key 的多个版本 → 最新有效版本 + 历史版本数。

    「最新」按 `valid_from` 取最大；并列时按 `memory_id` 取字典序最大，
    **保证同样输入永远同样输出**（铁律 9：任何未固定的顺序都是 bug）。
    """
    active = active_at(items, at)
    if not active:
        return VersionView(current=None, history_count=len(items))
    current = max(active, key=lambda i: (_naive(i.valid_from), i.memory_id))
    return VersionView(current=current, history_count=len(items) - 1)


# ─────────────────────────────────────────────────────────────────────
# ③ 写入冲突
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class MemoryConflict:
    """一次写入冲突（v6 §6.4）。

    `resolution` 三态：

    - `supersede` —— 新条目可信度**严格更高**，旧条目置 `superseded_by`；
    - `reject` —— 新条目可信度**严格更低**，不写入，但**留痕**；
    - `escalate` —— 同档相撞，系统没有依据裁决，**升级人工**（FTS-2001）。
    """

    key: str
    existing_id: str
    existing_source: str
    existing_value: str
    incoming_source: str
    incoming_value: str
    resolution: str

    @property
    def needs_human(self) -> bool:
        return self.resolution == "escalate"

    def describe(self) -> str:
        arrow = {"supersede": "覆盖", "reject": "拒绝", "escalate": "升级人工"}[self.resolution]
        return (
            f"记忆冲突（{self.key}）：现存「{self.existing_value}」"
            f"[{self.existing_source}] vs 新写入「{self.incoming_value}」"
            f"[{self.incoming_source}] → {arrow}"
        )

    def as_error(self) -> DataConflictError:
        """升级人工时抛的异常。**只在 `escalate` 下调用。**"""
        if not self.needs_human:
            raise ValueError(f"{self.resolution} 不需要升级人工，不该走 as_error()")
        return DataConflictError(
            self.describe(),
            details={
                "key": self.key,
                "existing_id": self.existing_id,
                "existing_source": self.existing_source,
                "existing_value": self.existing_value,
                "incoming_source": self.incoming_source,
                "incoming_value": self.incoming_value,
            },
            suggestions=[
                "两条来源同样可信、内容互斥，系统没有裁决依据",
                "请人工指定保留哪一条，或补一条更高可信度的来源（PG 事实）",
            ],
        )


def detect_conflict(
    *,
    key: str,
    existing_id: str,
    existing_source: str,
    existing_value: Any,
    incoming_source: str,
    incoming_value: Any,
) -> MemoryConflict | None:
    """内容矛盾时给出一条冲突记录；内容相同则返回 `None`。

    **判据是「值不同」而不是「值不兼容」**：本模块不理解记忆的语义，
    也不该理解 —— 「偏好的松弛顺序是 Tier1→Tier2」和「Tier2→Tier1」在
    字符串层面就是两个值，谁对由可信度或人来定。
    """
    if _canonical(existing_value) == _canonical(incoming_value):
        return None
    existing_trust = trust_of(existing_source)
    incoming_trust = trust_of(incoming_source)
    if incoming_trust > existing_trust:
        resolution = "supersede"
    elif incoming_trust < existing_trust:
        resolution = "reject"
    else:
        resolution = "escalate"
    return MemoryConflict(
        key=key,
        existing_id=existing_id,
        existing_source=existing_source,
        existing_value=_canonical(existing_value),
        incoming_source=incoming_source,
        incoming_value=_canonical(incoming_value),
        resolution=resolution,
    )


def _canonical(value: Any) -> str:
    """值的规范文本形态。字典按键排序 —— 键序不同不算冲突。"""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}={_canonical(value[k])}" for k in sorted(value)) + "}"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_canonical(v) for v in value) + "]"
    return str(value)


def rank_by_trust(sources: Iterable[str]) -> list[str]:
    """按可信度降序排列来源（同档按名称字典序，保证可复现）。"""
    return sorted(sources, key=lambda s: (-trust_of(s), s))


# ─────────────────────────────────────────────────────────────────────
# ④ 遗忘策略
# ─────────────────────────────────────────────────────────────────────
#: v6 §6.4「情景记忆超过 3 个训练周期后归档到冷表」
DEFAULT_RETENTION_CYCLES: Final[int] = 3


def archive_horizon(
    now: datetime | date,
    *,
    cycle_weeks: int,
    cycles: int = DEFAULT_RETENTION_CYCLES,
) -> datetime:
    """早于返回值的情景记忆应归档。

    ⚠️ **`cycle_weeks` 由调用方给，本模块不猜。** v6 §6.4 只说「3 个训练周期」，
    而训练周期长度按课目类别有 12 / 16 / 20 周三种（§6.3.3）。本窗口的口径是
    **取当前快照里最长的那个**（`memory/episodic.py::retention_cycle_weeks`）：
    宁可晚归档也不早归档 —— 归档的条目仍可检索，只是不参与默认召回，
    早归档的代价是「本该被想起来的事没被想起来」，比晚归档贵得多。
    """
    if cycle_weeks <= 0:
        raise ValueError(f"cycle_weeks 必须为正，收到 {cycle_weeks}")
    if cycles <= 0:
        raise ValueError(f"cycles 必须为正，收到 {cycles}")
    return _naive(_as_datetime(now)) - timedelta(weeks=cycle_weeks * cycles)


__all__ = [
    "DEFAULT_RETENTION_CYCLES",
    "SOURCE_CONVERSATION",
    "SOURCE_PG_FACT",
    "SOURCE_PLAN_CONFIRMED",
    "SOURCE_TRUST",
    "UNKNOWN_SOURCE_TRUST",
    "MemoryConflict",
    "VersionView",
    "Versioned",
    "active_at",
    "archive_horizon",
    "detect_conflict",
    "is_active_at",
    "latest_version",
    "rank_by_trust",
    "trust_of",
]
