"""实体消解：精确字典匹配 + 编辑距离候选（v6 §7.2.1 末段）。

> 槽位抽取仍走 LLM（实体、周次、约束修饰的组合太开放），**但实体消解不靠
> LLM 猜**——`resolve_person` 做精确字典匹配 + 编辑距离候选，命中多个或距离
> 过近（如同时命中「何超 / 高超」）时**不自行选择，触发反问**。

## 三档判定，逐档收紧

| 档 | 条件 | 结果 |
|---|---|---|
| 精确 | 表述就是编号，或与某个名称逐字相等 | 直接消解，`confidence=1.0` |
| 唯一近似 | 最优编辑距离 ≤ `max_distance`，且**严格优于**次优 | 消解，`confidence<1.0`，记 `fuzzy` |
| 歧义 | 最优距离出现并列，或没有任何候选进入阈值 | **不选**，写 `ambiguities` 触发反问 |

「严格优于次优」这一条是本模块的要害。基准数据里 `P02 高超` 与 `P08 何超`
只差一个字：用户打成「郝超」时两者距离都是 1，**并列**。此时选任何一个都是
在赌，而赌错的后果是把架次排到另一个人头上。所以并列一律反问。

## 编号不写死位数

`P\\d+` / `AC\\d+`，不是 `P\\d{2}`（v6 §5.1.1、`Z-4`）。用户上传 100 个人时
`P100` 必须能解析出来。同理，人员名单来自**当前快照**，不是代码里的常量表。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final

from backend.schemas.common import EntityKind

#: 编号形态（与 `backend.schemas.plan` 的模式同源，只固定前缀、不限位数）
_PERSON_ID = re.compile(r"^P\d+$")
_AIRCRAFT_ID = re.compile(r"^AC\d+$")
_MISSION_ID = re.compile(r"^mission[A-Z]-\d+$")

#: 「AC49」「49 号机」「49号」都指同一架；先扫编号，扫不到再退到裸数字
_BARE_NUMBER = re.compile(r"(\d+)\s*(?:号机|号|机)?$")

#: ISO 周的两种写法：`2026W02` / `2026-W02`
_ISO_WEEK = re.compile(r"^(\d{4})-?W(\d{1,2})$", re.IGNORECASE)
#: `2026-01-05` / `2026/1/5`
_ISO_DATE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")
#: 「1月5日」「1 月 5 号」
_CN_DATE = re.compile(r"^(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?$")

#: 相对周次表述 → 相对本周的偏移
_RELATIVE_WEEKS: Final[dict[str, int]] = {
    "本周": 0,
    "这周": 0,
    "当周": 0,
    "下周": 1,
    "下一周": 1,
    "次周": 1,
    "上周": -1,
    "上一周": -1,
    "前一周": -1,
    "下下周": 2,
    "上上周": -2,
}

#: 近似匹配的距离上限。中文姓名普遍 2~3 字，放到 2 会把「孙军 / 吴鹏」也拉进来。
DEFAULT_MAX_DISTANCE: Final[int] = 1


def levenshtein(a: str, b: str) -> int:
    """标准编辑距离。

    自己写而不引第三方，是因为它只有十行，而多一个依赖就多一处离线交付要装的
    东西（v6 §11.4）。
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,  # 删
                    current[j - 1] + 1,  # 增
                    previous[j - 1] + (ca != cb),  # 换
                )
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class Candidate:
    """一个近似候选。`distance` 进反问话术，让用户看得见「为什么问你」。"""

    entity_id: str
    label: str
    distance: int


@dataclass(frozen=True)
class Resolution:
    """一次消解的完整结果。**消解不了不是错误**，是要反问的信号。"""

    kind: EntityKind
    surface: str
    entity_id: str | None = None
    confidence: float = 0.0
    #: exact_id / exact_name / fuzzy / ambiguous / not_found
    reason: str = "not_found"
    candidates: tuple[Candidate, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.entity_id is not None

    @property
    def ambiguous(self) -> bool:
        return self.reason == "ambiguous"

    def as_ambiguity(self) -> dict[str, Any]:
        """写进 `state["ambiguities"]` 的形状。

        带上候选与距离，是为了让反问能说人话：「您说的『郝超』，是 高超(P02)
        还是 何超(P08)？」——只说「无法识别」的追问，用户答不上来。
        """
        return {
            "kind": self.kind,
            "surface": self.surface,
            "reason": self.reason,
            "candidates": [
                {"entity_id": c.entity_id, "label": c.label, "distance": c.distance}
                for c in self.candidates
            ],
            "question": self.question(),
        }

    def question(self) -> str:
        """反问话术。"""
        if self.reason == "ambiguous" and self.candidates:
            options = "、".join(f"{c.label}({c.entity_id})" for c in self.candidates)
            return f"「{self.surface}」有多个可能：{options}。请问是哪一个？"
        return f"「{self.surface}」在当前数据快照里查不到，请确认写法或补传数据。"


@dataclass(frozen=True)
class EntityDirectory:
    """当前快照的实体名录。

    **它是数据，不是常量**：`persons` 等映射由调用方从当前快照装配
    （:func:`directory_from_session`），代码里没有任何一份写死的名单
    （CLAUDE.md §11：8 人 / `P\\d{2}` / `JL-8` 一个都不许写成常量）。
    """

    persons: Mapping[str, str] = field(default_factory=dict)
    aircraft: Mapping[str, str] = field(default_factory=dict)
    missions: Mapping[str, str] = field(default_factory=dict)
    #: 别名 → 编号。业务方给某人起的小名、机队的旧编号都放这里，精确匹配一档生效
    aliases: Mapping[str, str] = field(default_factory=dict)

    def labels(self, kind: EntityKind) -> Mapping[str, str]:
        if kind == "person":
            return self.persons
        if kind == "aircraft":
            return self.aircraft
        if kind == "mission":
            return self.missions
        return {}

    def known(self, kind: EntityKind) -> frozenset[str]:
        return frozenset(self.labels(kind))


def _fuzzy(
    surface: str,
    labels: Mapping[str, str],
    *,
    max_distance: int,
) -> tuple[Candidate, ...]:
    """按编辑距离排出候选，只保留进入阈值的那些。"""
    scored = [
        Candidate(entity_id=eid, label=label, distance=levenshtein(surface, label))
        for eid, label in labels.items()
    ]
    within = [c for c in scored if c.distance <= max_distance]
    return tuple(sorted(within, key=lambda c: (c.distance, c.entity_id)))


def _decide(
    kind: EntityKind,
    surface: str,
    candidates: Sequence[Candidate],
) -> Resolution:
    """把候选列表判成「消解 / 歧义 / 查不到」。"""
    if not candidates:
        return Resolution(kind=kind, surface=surface, reason="not_found")
    best = candidates[0]
    tied = [c for c in candidates if c.distance == best.distance]
    if len(tied) > 1:
        # 并列 —— 「何超 / 高超」正是这一支。不选，反问。
        return Resolution(kind=kind, surface=surface, reason="ambiguous", candidates=tuple(tied))
    # 唯一最优，且严格优于次优（因为次优的 distance 必然更大）
    confidence = max(0.0, 1.0 - best.distance / max(len(best.label), 1))
    return Resolution(
        kind=kind,
        surface=surface,
        entity_id=best.entity_id,
        confidence=confidence,
        reason="fuzzy",
        candidates=tuple(candidates),
    )


def _resolve_by_label(
    kind: EntityKind,
    surface: str,
    directory: EntityDirectory,
    *,
    id_pattern: re.Pattern[str],
    max_distance: int,
) -> Resolution:
    text = surface.strip()
    labels = directory.labels(kind)

    if id_pattern.match(text):
        if text in labels:
            return Resolution(
                kind=kind, surface=surface, entity_id=text, confidence=1.0, reason="exact_id"
            )
        # 形态对但库里没有 —— 这正是 `entity_hallucination` 的样子，绝不放行
        return Resolution(kind=kind, surface=surface, reason="not_found")

    alias = directory.aliases.get(text)
    if alias is not None and alias in labels:
        return Resolution(
            kind=kind, surface=surface, entity_id=alias, confidence=1.0, reason="exact_name"
        )

    exact = [eid for eid, label in labels.items() if label == text]
    if len(exact) == 1:
        return Resolution(
            kind=kind, surface=surface, entity_id=exact[0], confidence=1.0, reason="exact_name"
        )
    if len(exact) > 1:
        # 同名两个人。库里允许重名，消解不允许猜。
        return Resolution(
            kind=kind,
            surface=surface,
            reason="ambiguous",
            candidates=tuple(
                Candidate(entity_id=eid, label=labels[eid], distance=0) for eid in sorted(exact)
            ),
        )

    return _decide(kind, surface, _fuzzy(text, labels, max_distance=max_distance))


def resolve_person(
    surface: str,
    directory: EntityDirectory,
    *,
    max_distance: int = DEFAULT_MAX_DISTANCE,
) -> Resolution:
    """把人员表述解析为 `person_id`。"""
    return _resolve_by_label(
        "person", surface, directory, id_pattern=_PERSON_ID, max_distance=max_distance
    )


def resolve_mission(
    surface: str,
    directory: EntityDirectory,
    *,
    max_distance: int = DEFAULT_MAX_DISTANCE,
) -> Resolution:
    """把课目表述解析为 `mission_id`。"""
    return _resolve_by_label(
        "mission", surface, directory, id_pattern=_MISSION_ID, max_distance=max_distance
    )


def resolve_aircraft(
    surface: str,
    directory: EntityDirectory,
    *,
    max_distance: int = DEFAULT_MAX_DISTANCE,
) -> Resolution:
    """把飞机表述解析为 `aircraft_id`。

    比人员多一档：机号在口语里常被剥掉前缀（「49 号机」「就用 49」）。裸数字
    补回 `AC` 前缀后仍要**回名录核对**——`AC99` 不在库里就是查不到，不是
    「大概是那架」。
    """
    text = surface.strip().upper()
    if _AIRCRAFT_ID.match(text):
        if text in directory.aircraft:
            return Resolution(
                kind="aircraft",
                surface=surface,
                entity_id=text,
                confidence=1.0,
                reason="exact_id",
            )
        return Resolution(kind="aircraft", surface=surface, reason="not_found")

    bare = _BARE_NUMBER.match(text)
    if bare is not None:
        guess = f"AC{bare.group(1)}"
        if guess in directory.aircraft:
            return Resolution(
                kind="aircraft",
                surface=surface,
                entity_id=guess,
                confidence=1.0,
                reason="exact_id",
            )
        return Resolution(kind="aircraft", surface=surface, reason="not_found")

    return _resolve_by_label(
        "aircraft", surface, directory, id_pattern=_AIRCRAFT_ID, max_distance=max_distance
    )


def monday_of(day: date) -> date:
    """所在 ISO 周的周一。排班周恒为周一~周日。"""
    return day - timedelta(days=day.weekday())


def resolve_week(surface: str, *, today: date) -> Resolution:
    """把周表述解析为 ISO 周（`2026W02`）。

    `today` **必须由调用方传入**，不在这里调 `date.today()`：同一条轨迹在
    重放时若因为「今天」变了而解出另一周，重放一致率就成了随机数
    （v6 §12.5.2 要求 100%）。
    """
    text = surface.strip()

    iso = _ISO_WEEK.match(text)
    if iso is not None:
        year, week = int(iso.group(1)), int(iso.group(2))
        if 1 <= week <= 53:
            return Resolution(
                kind="week",
                surface=surface,
                entity_id=f"{year}W{week:02d}",
                confidence=1.0,
                reason="exact_id",
            )
        return Resolution(kind="week", surface=surface, reason="not_found")

    day = _parse_day(text, today=today)
    if day is not None:
        return _week_resolution(surface, day, reason="exact_id")

    # ⚠️ **必须按表述长度降序匹配**（M5 实测修复）。
    #
    # 「上周」是「上上周」的子串，「下周」是「下下周」的子串。按字典插入顺序扫，
    # 「上上周」会先命中「上周」——用户说上上周、系统查上周，**而且不报错**：
    # 它返回的是一个格式完全正确的 ISO 周，只是差了一周。这类错答在检索层
    # 表现为「召回到的是另一周的记录」，排查时看不出任何异常。
    for phrase in sorted(_RELATIVE_WEEKS, key=len, reverse=True):
        if phrase in text:
            return _week_resolution(
                surface,
                monday_of(today) + timedelta(weeks=_RELATIVE_WEEKS[phrase]),
                reason="relative",
            )

    return Resolution(kind="week", surface=surface, reason="not_found")


def _parse_day(text: str, *, today: date) -> date | None:
    iso_date = _ISO_DATE.match(text)
    if iso_date is not None:
        return _safe_date(int(iso_date.group(1)), int(iso_date.group(2)), int(iso_date.group(3)))
    cn = _CN_DATE.match(text)
    if cn is not None:
        return _safe_date(today.year, int(cn.group(1)), int(cn.group(2)))
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _week_resolution(surface: str, day: date, *, reason: str) -> Resolution:
    iso_year, iso_week, _ = day.isocalendar()
    return Resolution(
        kind="week",
        surface=surface,
        entity_id=f"{iso_year}W{iso_week:02d}",
        confidence=1.0,
        reason=reason,
    )


def iso_week_of(day: date) -> str:
    """`2026-01-05` → `2026W02`。:func:`week_start_of` 的逆。

    **两个方向放在一起**：它们必须始终互逆，分散在各处的私有实现迟早会漂移
    （本函数落地时仓库里已经有三处等价写法）。
    """
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}W{iso_week:02d}"


def week_start_of(iso_week: str) -> date:
    """`2026W02` → 该周周一（`2026-01-05`）。"""
    match = _ISO_WEEK.match(iso_week)
    if match is None:
        raise ValueError(f"不是合法的 ISO 周表述：{iso_week!r}")
    return date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)


def directory_from_session(session: Any, snapshot_id: str) -> EntityDirectory:
    """从当前快照装配名录。

    放在这里而不是让调用方自己写 SQL，是为了让「名录只能来自快照」这件事有
    唯一入口——散在各处的话，迟早有人图省事塞一份硬编码名单进去。
    """
    from sqlalchemy import select

    from backend.models.entities import Aircraft, Mission, Person

    persons = {
        row.person_id: row.name
        for row in session.execute(
            select(Person).where(Person.snapshot_id == snapshot_id)
        ).scalars()
    }
    aircraft = {
        row.aircraft_id: row.aircraft_type
        for row in session.execute(
            select(Aircraft).where(Aircraft.snapshot_id == snapshot_id)
        ).scalars()
    }
    missions = {
        row.mission_id: row.name
        for row in session.execute(
            select(Mission).where(Mission.snapshot_id == snapshot_id)
        ).scalars()
    }
    return EntityDirectory(persons=persons, aircraft=aircraft, missions=missions)


def collect_ambiguities(resolutions: Iterable[Resolution]) -> list[dict[str, Any]]:
    """把一批消解结果里需要反问的挑出来。

    **`not_found` 也要反问**，不只是 `ambiguous`：用户说了「AC99」而库里没有，
    静默忽略等于把「这架飞机不存在」这件事藏起来。
    """
    return [r.as_ambiguity() for r in resolutions if not r.resolved]


__all__ = [
    "DEFAULT_MAX_DISTANCE",
    "Candidate",
    "EntityDirectory",
    "Resolution",
    "collect_ambiguities",
    "directory_from_session",
    "iso_week_of",
    "levenshtein",
    "monday_of",
    "resolve_aircraft",
    "resolve_mission",
    "resolve_person",
    "resolve_week",
    "week_start_of",
]
