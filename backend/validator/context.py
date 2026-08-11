"""校验器自己的只读事实视图（v6 §4.2 的 `DataContext`）。

## 为什么校验器要有一份自己的 context

v6 §4.1 要求闸门1「另一套代码，只读解，依 §3.2 重算 14 条」，且**不引用 solver
任何模块**。求解侧有自己的数据装配（`compile_spec` → `SpecBundle`），校验侧如果
去复用它，`validator → solver` 的依赖就成立了 —— import-linter 的禁令一会当场
报红，而更要命的是双通道校验的证据基础没了（CLAUDE.md 铁律 2）。

所以本模块从 **PG 的事实表**（`backend.models`）直接装配一份只读快照，与求解侧
各读各的。两边共用的只有：

- `backend.schemas.plan` —— 数据形状（不含约束表达）
- `backend.core.ruleset` —— 规则参数的类型化加载（YAML 读成对象，不表达约束）
- PG 里的同一批事实数据

## 规则参数取 YAML，实体数据取 PG

这条口径继承自 `backend.core.ruleset` 的模块文档：**能从上传数据里读到的，就不许
从 YAML 里读**。所以周转时间取 `aircraft.turnaround_minutes`（逐机一列）、空域
容量取 `airspaces.capacity`，而不是 `ruleset` 里那份抄录 —— 用户换一批数据时
变的是数据不是规则（CLAUDE.md §11：`JL-8` / `8 机` / `A~H` 一个都不许写成常量）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from backend.core.errors import RequiredInputMissingError
from backend.core.ruleset import (
    IDENTITY_STUDENT,
    Ruleset,
    Semantics,
    get_ruleset,
    get_semantics,
)
from backend.models.entities import (
    Aircraft,
    AircraftMaintenance,
    AircraftMissionCapability,
    Airspace,
    Mission,
    MissionAircraftType,
    MissionPrereq,
    Person,
    PersonAircraftType,
    PersonCompletedMission,
    PersonQualification,
    PersonUnavailability,
    Runway,
    RunwayAircraftType,
)
from backend.models.progress import TrainingProgress

#: 一周七天（排班周恒为周一~周日，见 `SchedulePlan._week_span`）
WEEK_DAYS = 7


# ─────────────────────────────────────────────────────────────────────
# 事实对象
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class QualificationFacts:
    """一条类别资质（`personnel.pdf` 二、课目级资质明细）。"""

    mission_class: str
    level: str
    expiry_date: date | None


@dataclass(frozen=True)
class PersonFacts:
    """人员及其多值属性。"""

    person_id: str
    name: str
    identity: str
    aircraft_types: frozenset[str] = frozenset()
    qualifications: Mapping[str, QualificationFacts] = field(default_factory=dict)
    unavailable_dates: frozenset[date] = frozenset()
    completed_missions: frozenset[str] = frozenset()

    @property
    def is_student(self) -> bool:
        return self.identity == IDENTITY_STUDENT

    def qualification_of(self, mission_class: str) -> QualificationFacts | None:
        return self.qualifications.get(mission_class)


@dataclass(frozen=True)
class MaintenanceWindow:
    """一段维护时段（`aircraft.pdf`「维护计划」列）。"""

    start: datetime
    end: datetime
    kind: str = "定检维护"
    all_day: bool = False

    def overlaps(self, day: date, takeoff: time, landing: time) -> bool:
        """半开区间相交：`[takeoff, landing)` 与 `[start, end)`。

        贴边不算重叠 —— 维护 08:00 结束、架次 08:00 起飞是允许的（规格只说
        「维护时段内不得安排架次」，没要求维护前后另留周转时间）。
        """
        s = datetime.combine(day, takeoff)
        e = datetime.combine(day, landing)
        return s < self.end and self.start < e


@dataclass(frozen=True)
class AircraftFacts:
    """飞机及其适配课目与维护计划。"""

    aircraft_id: str
    aircraft_type: str
    seats: int
    turnaround_minutes: int
    daily_window_start: time
    daily_window_end: time
    capable_missions: frozenset[str] = frozenset()
    maintenance: tuple[MaintenanceWindow, ...] = ()


@dataclass(frozen=True)
class PrereqRef:
    """一条先修引用。`kind` 为 `mission`（课目编号）或 `class`（类别，S-01）。"""

    ref: str
    kind: str


@dataclass(frozen=True)
class MissionFacts:
    """课目。"""

    mission_id: str
    name: str
    mission_class: str
    duration_minutes: int
    freq_days: int
    cycle_weeks: int
    dual_required: bool
    weekly_required: bool
    airspace_id: str
    aircraft_types: frozenset[str] = frozenset()
    prereqs: tuple[PrereqRef, ...] = ()


@dataclass(frozen=True)
class AirspaceFacts:
    """空域/航线。`capacity` 即同时段容量（S-10 硬约束，并入约束6）。"""

    airspace_id: str
    name: str
    capacity: int


@dataclass(frozen=True)
class RunwayFacts:
    """跑道及其服务机型。

    ⚠️ **不是「RWY-1=JL-8、RWY-2=JL-9」**：`RWY-1` 服务 JL-8 与 JL-9，
    `RWY-2` 只服务 JL-8（v6 §1.3.5）。
    """

    runway_id: str
    name: str
    aircraft_types: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ProgressFacts:
    """(人员, 课目) 的跨周进度锚点（v6 §6.3）。

    `last_done_date` 为 None 是**正常状态**（原始 PDF 没有这个字段），按 S-12
    处理为「窗口从本周周一起算、不计欠账」—— 写成 `gap=999` 是 CLAUDE.md §11
    明列的反模式。
    """

    person_id: str
    mission_id: str
    status: str
    cycle_start: date | None = None
    last_done_date: date | None = None
    debt_count: int = 0
    prereq_met: bool = True
    blocked_reason: str | None = None
    is_recurrent: bool = False
    recurrent_since: date | None = None

    @property
    def completed(self) -> bool:
        return self.status == "COMPLETED"


# ─────────────────────────────────────────────────────────────────────
# 校验上下文
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ValidationContext:
    """校验 14 条规则所需的全部只读事实。"""

    week_start: date
    persons: Mapping[str, PersonFacts]
    aircraft: Mapping[str, AircraftFacts]
    missions: Mapping[str, MissionFacts]
    airspaces: Mapping[str, AirspaceFacts]
    runways: Mapping[str, RunwayFacts]
    progress: Mapping[tuple[str, str], ProgressFacts] = field(default_factory=dict)
    ruleset: Ruleset = field(default_factory=get_ruleset)
    semantics: Semantics = field(default_factory=get_semantics)
    snapshot_id: str = ""

    def __post_init__(self) -> None:
        if self.week_start.weekday() != 0:
            raise RequiredInputMissingError(
                f"week_start 必须是周一，实际 {self.week_start}（{self.week_start.weekday()}）"
            )

    # ── 派生查询 ────────────────────────────────────────────────────
    def day_offset(self, day: date) -> int:
        """日期 → 周内偏移（周一 = 0）。"""
        return (day - self.week_start).days

    def airspace_of(self, mission_id: str) -> str | None:
        """课目绑定的空域编号。**空域以课目表为准**，架次上那一列只是投影。"""
        mission = self.missions.get(mission_id)
        return mission.airspace_id if mission else None

    def runways_for_type(self, aircraft_type: str) -> frozenset[str]:
        """某机型可用的跑道集合。"""
        return frozenset(
            r.runway_id for r in self.runways.values() if aircraft_type in r.aircraft_types
        )

    def students(self) -> tuple[PersonFacts, ...]:
        return tuple(p for p in self.sorted_persons() if p.is_student)

    def sorted_persons(self) -> tuple[PersonFacts, ...]:
        """按编号排序 —— 校验报告必须逐字节可复现（铁律 9）。"""
        return tuple(self.persons[pid] for pid in sorted(self.persons))

    def weekly_required_classes(self) -> tuple[str, ...]:
        """约束3 的适用类别 —— 由课目表的 `weekly_required` 推出，**不写死「A 类」**。

        基准数据里只有 A-1/A-2 带「（每周必飞）」标记，故结果为 `("A",)`；换一批
        数据时它跟着数据走（CLAUDE.md §11：类别 A~H 不许写成常量）。
        """
        return tuple(sorted({m.mission_class for m in self.missions.values() if m.weekly_required}))

    def missions_of_class(self, mission_class: str) -> tuple[str, ...]:
        return tuple(
            sorted(m.mission_id for m in self.missions.values() if m.mission_class == mission_class)
        )

    def progress_of(self, person_id: str, mission_id: str) -> ProgressFacts | None:
        return self.progress.get((person_id, mission_id))


# ─────────────────────────────────────────────────────────────────────
# 装配：ORM 行 → ValidationContext
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ContextRows:
    """从 PG 取回的原始行集合。拆出来是为了让纯装配逻辑可单测（不必连库）。"""

    persons: Sequence[Person] = ()
    person_aircraft_types: Sequence[PersonAircraftType] = ()
    person_qualifications: Sequence[PersonQualification] = ()
    person_unavailability: Sequence[PersonUnavailability] = ()
    person_completed: Sequence[PersonCompletedMission] = ()
    aircraft: Sequence[Aircraft] = ()
    aircraft_capability: Sequence[AircraftMissionCapability] = ()
    maintenance: Sequence[AircraftMaintenance] = ()
    missions: Sequence[Mission] = ()
    mission_aircraft_types: Sequence[MissionAircraftType] = ()
    mission_prereq: Sequence[MissionPrereq] = ()
    airspaces: Sequence[Airspace] = ()
    runways: Sequence[Runway] = ()
    runway_aircraft_types: Sequence[RunwayAircraftType] = ()
    progress: Sequence[TrainingProgress] = ()


def _multi(pairs: Iterable[tuple[str, str]]) -> dict[str, frozenset[str]]:
    acc: dict[str, set[str]] = {}
    for key, value in pairs:
        acc.setdefault(key, set()).add(value)
    return {k: frozenset(v) for k, v in acc.items()}


def context_from_rows(
    rows: ContextRows,
    *,
    week_start: date,
    snapshot_id: str = "",
    ruleset: Ruleset | None = None,
    semantics: Semantics | None = None,
) -> ValidationContext:
    """把 ORM 行装配成 :class:`ValidationContext`（纯函数，不碰数据库）。"""
    types_by_person = _multi((r.person_id, r.aircraft_type) for r in rows.person_aircraft_types)
    unavail_by_person: dict[str, set[date]] = {}
    for u in rows.person_unavailability:
        unavail_by_person.setdefault(u.person_id, set()).add(u.unavailable_date)
    done_by_person = _multi((r.person_id, r.mission_id) for r in rows.person_completed)
    quals_by_person: dict[str, dict[str, QualificationFacts]] = {}
    for q in rows.person_qualifications:
        quals_by_person.setdefault(q.person_id, {})[q.mission_class] = QualificationFacts(
            mission_class=q.mission_class, level=q.level, expiry_date=q.expiry_date
        )

    persons = {
        p.person_id: PersonFacts(
            person_id=p.person_id,
            name=p.name,
            identity=p.identity,
            aircraft_types=types_by_person.get(p.person_id, frozenset()),
            qualifications=quals_by_person.get(p.person_id, {}),
            unavailable_dates=frozenset(unavail_by_person.get(p.person_id, set())),
            completed_missions=done_by_person.get(p.person_id, frozenset()),
        )
        for p in rows.persons
    }

    capable = _multi((r.aircraft_id, r.mission_id) for r in rows.aircraft_capability)
    maint_by_aircraft: dict[str, list[MaintenanceWindow]] = {}
    for m in rows.maintenance:
        maint_by_aircraft.setdefault(m.aircraft_id, []).append(
            MaintenanceWindow(start=m.start_ts, end=m.end_ts, kind=m.kind, all_day=m.all_day)
        )
    aircraft = {
        a.aircraft_id: AircraftFacts(
            aircraft_id=a.aircraft_id,
            aircraft_type=a.aircraft_type,
            seats=a.seats,
            turnaround_minutes=a.turnaround_minutes,
            daily_window_start=a.daily_window_start,
            daily_window_end=a.daily_window_end,
            capable_missions=capable.get(a.aircraft_id, frozenset()),
            maintenance=tuple(
                sorted(maint_by_aircraft.get(a.aircraft_id, []), key=lambda w: w.start)
            ),
        )
        for a in rows.aircraft
    }

    mission_types = _multi((r.mission_id, r.aircraft_type) for r in rows.mission_aircraft_types)
    prereq_by_mission: dict[str, list[PrereqRef]] = {}
    for pr in rows.mission_prereq:
        prereq_by_mission.setdefault(pr.mission_id, []).append(
            PrereqRef(ref=pr.prereq_ref, kind=pr.ref_kind)
        )
    missions = {
        m.mission_id: MissionFacts(
            mission_id=m.mission_id,
            name=m.name,
            mission_class=m.mission_class,
            duration_minutes=m.duration_minutes,
            freq_days=m.freq_days,
            cycle_weeks=m.cycle_weeks,
            dual_required=m.dual_required,
            weekly_required=m.weekly_required,
            airspace_id=m.airspace_id,
            aircraft_types=mission_types.get(m.mission_id, frozenset()),
            prereqs=tuple(sorted(prereq_by_mission.get(m.mission_id, []), key=lambda p: p.ref)),
        )
        for m in rows.missions
    }

    airspaces = {
        a.airspace_id: AirspaceFacts(airspace_id=a.airspace_id, name=a.name, capacity=a.capacity)
        for a in rows.airspaces
    }
    runway_types = _multi((r.runway_id, r.aircraft_type) for r in rows.runway_aircraft_types)
    runways = {
        r.runway_id: RunwayFacts(
            runway_id=r.runway_id,
            name=r.name,
            aircraft_types=runway_types.get(r.runway_id, frozenset()),
        )
        for r in rows.runways
    }

    progress: dict[tuple[str, str], ProgressFacts] = {}
    for row in sorted(rows.progress, key=lambda r: (r.person_id, r.mission_id, r.cycle_start)):
        key = (row.person_id, row.mission_id)
        current = progress.get(key)
        # 同一 (人, 课目) 可能有多轮周期：取 cycle_start ≤ week_start 的最后一轮；
        # 全都晚于 week_start 时退回最早的一轮（数据异常，但不能静默丢掉）
        if (
            current is not None
            and current.cycle_start is not None
            and row.cycle_start > week_start >= current.cycle_start
        ):
            continue
        progress[key] = ProgressFacts(
            person_id=row.person_id,
            mission_id=row.mission_id,
            status=row.status,
            cycle_start=row.cycle_start,
            last_done_date=row.last_done_date,
            debt_count=row.debt_count,
            prereq_met=row.prereq_met,
            blocked_reason=row.blocked_reason,
            is_recurrent=row.is_recurrent,
            recurrent_since=row.recurrent_since,
        )

    return ValidationContext(
        week_start=week_start,
        persons=persons,
        aircraft=aircraft,
        missions=missions,
        airspaces=airspaces,
        runways=runways,
        progress=progress,
        ruleset=ruleset or get_ruleset(),
        semantics=semantics or get_semantics(),
        snapshot_id=snapshot_id,
    )


def fetch_rows(session: Session, snapshot_id: str) -> ContextRows:
    """按快照把事实表整批取回。"""

    def _all(model: type) -> Sequence[Any]:
        stmt: Select[Any] = select(model).where(model.snapshot_id == snapshot_id)  # type: ignore[attr-defined]
        return list(session.execute(stmt).scalars().all())

    return ContextRows(
        persons=_all(Person),
        person_aircraft_types=_all(PersonAircraftType),
        person_qualifications=_all(PersonQualification),
        person_unavailability=_all(PersonUnavailability),
        person_completed=_all(PersonCompletedMission),
        aircraft=_all(Aircraft),
        aircraft_capability=_all(AircraftMissionCapability),
        maintenance=_all(AircraftMaintenance),
        missions=_all(Mission),
        mission_aircraft_types=_all(MissionAircraftType),
        mission_prereq=_all(MissionPrereq),
        airspaces=_all(Airspace),
        runways=_all(Runway),
        runway_aircraft_types=_all(RunwayAircraftType),
        progress=_all(TrainingProgress),
    )


def load_context(
    session: Session,
    *,
    snapshot_id: str,
    week_start: date,
    ruleset: Ruleset | None = None,
    semantics: Semantics | None = None,
) -> ValidationContext:
    """从 PG 装配校验上下文。

    **校验器自己读库**，不接受求解侧传过来的数据对象 —— 这样「求解器读错了一行
    数据」这类错误也在闸门1 的覆盖范围内（v6 §4.1 的三道闸门是独立的）。
    """
    return context_from_rows(
        fetch_rows(session, snapshot_id),
        week_start=week_start,
        snapshot_id=snapshot_id,
        ruleset=ruleset,
        semantics=semantics,
    )


__all__ = [
    "WEEK_DAYS",
    "AircraftFacts",
    "AirspaceFacts",
    "ContextRows",
    "MaintenanceWindow",
    "MissionFacts",
    "PersonFacts",
    "PrereqRef",
    "ProgressFacts",
    "QualificationFacts",
    "RunwayFacts",
    "ValidationContext",
    "context_from_rows",
    "fetch_rows",
    "load_context",
]
