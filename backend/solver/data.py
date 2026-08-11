"""求解侧的数据读取层：按 `snapshot_id` 从 PG 把实体读成不可变的求解输入。

## 三条硬性口径

1. **实体一律从 PG 按 `snapshot_id` 读。** 8 人 / 8 机 / 12 课目 / 6 空域 /
   `P\\d{2}` / `JL-8` / 类别 A~H 一个都不出现在本模块（CLAUDE.md §11、v6 §5.1.1）。
   基准数据集只是「数据长什么样」的样本，不是系统上限。
2. **规则参数取 `rules/*.yaml`，实体数据取 PG。** 周转时间读
   `aircraft.turnaround_minutes`（逐机一列）而不是 ruleset 里按机型抄录的那份；
   空域容量读 `airspaces.capacity` 而不是 ruleset 的 `params.airspace_capacity`。
3. **`ScenarioOverrides` 是外部扰动输入，不是松弛动作。** 空域关闭 = 容量降为 0、
   跑道关闭、临时维护、临时不可用、到期日改期，全部走这个对象
   （v6 §3.4 / §12.3 的单点扰动与 I1~I5 构造）。**它不放宽任何约束**——
   放宽约束是 `diagnose.py` 里 R1/R2 的事，两者不要混（v6 §3.10）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

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

#: 排班周长度（天）。ISO 周恒为 7 天，参数化只为让「7」的来源显式。
WEEK_DAYS: int = 7


# ─────────────────────────────────────────────────────────────────────
# 实体
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Qualification:
    """人员在某个课目类别上的资质（`person_qualifications` 一行）。"""

    mission_class: str
    level: str
    expiry: date | None

    def valid_on(self, day: date, *, expiry_inclusive: bool = True) -> bool:
        """约束2 字面语义：到期日**当日**仍可执行，次日起失效。"""
        if self.expiry is None:
            return True
        return day <= self.expiry if expiry_inclusive else day < self.expiry

    def expired_on(self, day: date, *, expiry_inclusive: bool = True) -> bool:
        return not self.valid_on(day, expiry_inclusive=expiry_inclusive)


@dataclass(frozen=True)
class PersonInfo:
    person_id: str
    name: str
    identity: str
    aircraft_types: frozenset[str]
    qualifications: Mapping[str, Qualification]
    unavailable: frozenset[date]
    completed: frozenset[str]

    def qual(self, mission_class: str) -> Qualification | None:
        return self.qualifications.get(mission_class)


@dataclass(frozen=True)
class MaintenanceWindow:
    """维护时段（`aircraft_maintenance` 一行）。"""

    start: datetime
    end: datetime
    kind: str
    all_day: bool

    def covers_day(self, day: date) -> bool:
        return self.start.date() <= day <= self.end.date()

    def blocks_whole_window(self, day: date, win_start: time, win_end: time) -> bool:
        """该维护是否把当日的训练窗整段封死（封死则静态预筛剔除候选）。"""
        if not self.covers_day(day):
            return False
        return self.start <= datetime.combine(day, win_start) and self.end >= datetime.combine(
            day, win_end
        )

    def minute_span(self, day: date, origin: time) -> tuple[int, int] | None:
        """把维护时段折算成「当日 origin 起的分钟数」区间，与当日无交集返回 None。"""
        base = datetime.combine(day, origin)
        day_end = base + timedelta(days=1)
        lo = max(self.start, base)
        hi = min(self.end, day_end)
        if hi <= lo:
            return None
        return (
            int((lo - base).total_seconds() // 60),
            int((hi - base).total_seconds() // 60),
        )


@dataclass(frozen=True)
class AircraftInfo:
    aircraft_id: str
    aircraft_type: str
    seats: int
    window_start: time
    window_end: time
    turnaround_minutes: int
    missions: frozenset[str]
    maintenance: tuple[MaintenanceWindow, ...]


@dataclass(frozen=True)
class MissionInfo:
    mission_id: str
    name: str
    mission_class: str
    duration_minutes: int
    cycle_weeks: int
    freq_days: int
    weekly_required: bool
    dual_required: bool
    airspace_id: str
    aircraft_types: frozenset[str]
    #: (prereq_ref, ref_kind) 原样，类展开交给 `retrieval.prereq_cte`（v6 §6.1）
    prereqs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class AirspaceInfo:
    airspace_id: str
    name: str
    capacity: int


@dataclass(frozen=True)
class RunwayInfo:
    runway_id: str
    name: str
    aircraft_types: frozenset[str]


@dataclass(frozen=True)
class ProgressInfo:
    """`training_progress` 一行（v6 §6.3，物化视图语义）。"""

    person_id: str
    mission_id: str
    cycle_start: date
    status: str
    completed_count: int
    last_done_date: date | None
    cycle_weeks: int
    debt_count: int
    prereq_met: bool
    blocked_reason: str | None
    is_recurrent: bool
    recurrent_since: date | None

    @property
    def completed(self) -> bool:
        return self.status == "COMPLETED"


# ─────────────────────────────────────────────────────────────────────
# 外部扰动
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScenarioOverrides:
    """外部扰动输入（v6 §12.3 单点/组合扰动、I1~I5 构造）。

    **不是松弛**：它只改变「世界的样子」（谁不在、哪架机在修、哪个空域关了），
    从不放宽任何一条约束。
    """

    #: 训练窗压缩（v6 §12.3 的 I4 / I5）
    window_start: time | None = None
    window_end: time | None = None
    #: 空域容量覆盖。**关闭 = 容量降为 0**（v6 §3.4）
    airspace_capacity: Mapping[str, int] = field(default_factory=dict)
    #: 跑道关闭（I5）。关闭的跑道不出现在任何候选的可选跑道集里
    closed_runways: frozenset[str] = frozenset()
    #: 追加的人员不可用日期
    unavailable: Mapping[str, frozenset[date]] = field(default_factory=dict)
    #: 整周不可用的人员（I1）
    unavailable_all_week: frozenset[str] = frozenset()
    #: 追加的全天维护：(aircraft_id, 首日, 末日)
    maintenance_all_day: tuple[tuple[str, date, date], ...] = ()
    #: 资质到期日覆盖：(person_id, mission_class) → 到期日（S-11 专项用）
    qual_expiry: Mapping[tuple[str, str], date] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (
            self.window_start
            or self.window_end
            or self.airspace_capacity
            or self.closed_runways
            or self.unavailable
            or self.unavailable_all_week
            or self.maintenance_all_day
            or self.qual_expiry
        )


NO_OVERRIDES: ScenarioOverrides = ScenarioOverrides()


# ─────────────────────────────────────────────────────────────────────
# 求解输入
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ProblemData:
    """一次排班所需的全部实体，按 `snapshot_id` 读定后不再变。"""

    snapshot_id: str
    week_start: date
    window_start: time
    window_end: time
    persons: Mapping[str, PersonInfo]
    aircraft: Mapping[str, AircraftInfo]
    missions: Mapping[str, MissionInfo]
    airspaces: Mapping[str, AirspaceInfo]
    runways: Mapping[str, RunwayInfo]
    progress: Mapping[tuple[str, str], ProgressInfo]
    overrides: ScenarioOverrides = NO_OVERRIDES

    # ── 时间与日期 ──────────────────────────────────────────────────
    @property
    def week_end(self) -> date:
        return self.week_start + timedelta(days=WEEK_DAYS - 1)

    @property
    def iso_week(self) -> str:
        iso = self.week_start.isocalendar()
        return f"{iso.year}W{iso.week:02d}"

    @property
    def days(self) -> tuple[int, ...]:
        return tuple(range(WEEK_DAYS))

    def date_of(self, day: int) -> date:
        return self.week_start + timedelta(days=day)

    def day_index(self, when: date) -> int:
        return (when - self.week_start).days

    @property
    def horizon_minutes(self) -> int:
        """训练窗长度（分钟）。时间一律编码为「当日 window_start 起的分钟数」。"""
        start = self.window_start.hour * 60 + self.window_start.minute
        end = self.window_end.hour * 60 + self.window_end.minute
        return end - start

    def minutes_of(self, clock: time) -> int:
        base = self.window_start.hour * 60 + self.window_start.minute
        return clock.hour * 60 + clock.minute - base

    def clock_of(self, minutes: int) -> time:
        base = self.window_start.hour * 60 + self.window_start.minute + minutes
        return time(base // 60, base % 60)

    # ── 派生视图 ────────────────────────────────────────────────────
    @property
    def mission_classes(self) -> tuple[str, ...]:
        return tuple(sorted({m.mission_class for m in self.missions.values()}))

    def missions_of_class(self, mission_class: str) -> tuple[str, ...]:
        return tuple(
            sorted(mid for mid, m in self.missions.items() if m.mission_class == mission_class)
        )

    @property
    def weekly_required_classes(self) -> tuple[str, ...]:
        """约束3 的适用类别 —— 由 `missions.weekly_required`（「每周必飞」列）决定。

        基准数据里只有 A-1/A-2 是「每周必飞」，故这里得到 `("A",)`。**这是从
        上传数据算出来的，不是把「A 类」写死**（换一批数据、别的类别标了每周必飞，
        约束3 自动跟着变）。
        """
        return tuple(sorted({m.mission_class for m in self.missions.values() if m.weekly_required}))

    def prereq_map(self) -> dict[str, tuple[tuple[str, str], ...]]:
        return {mid: m.prereqs for mid, m in self.missions.items()}

    def capacity_of(self, airspace_id: str) -> int:
        """空域同时段容量，应用扰动覆盖后的有效值（关闭 → 0）。"""
        if airspace_id in self.overrides.airspace_capacity:
            return self.overrides.airspace_capacity[airspace_id]
        space = self.airspaces.get(airspace_id)
        return space.capacity if space else 0

    def allowed_runways(self, aircraft_type: str) -> tuple[str, ...]:
        """该机型可用的跑道（S-05，按 `runway_aircraft_types` 读，关闭的跑道剔除）。"""
        return tuple(
            sorted(
                rid
                for rid, rwy in self.runways.items()
                if aircraft_type in rwy.aircraft_types and rid not in self.overrides.closed_runways
            )
        )

    def progress_of(self, person_id: str, mission_id: str) -> ProgressInfo | None:
        return self.progress.get((person_id, mission_id))


# ─────────────────────────────────────────────────────────────────────
# 加载
# ─────────────────────────────────────────────────────────────────────
def _grouped(pairs: Iterable[tuple[str, str]]) -> dict[str, frozenset[str]]:
    acc: dict[str, set[str]] = {}
    for key, value in pairs:
        acc.setdefault(key, set()).add(value)
    return {k: frozenset(v) for k, v in acc.items()}


def load_problem_data(
    session: Session,
    *,
    snapshot_id: str,
    week_start: date,
    window_start: time,
    window_end: time,
    overrides: ScenarioOverrides = NO_OVERRIDES,
) -> ProblemData:
    """把一个快照的全部实体读成 :class:`ProblemData`。

    `window_start` / `window_end` 来自 ruleset 约束1，扰动可覆盖（I4/I5）。
    """
    week_dates = frozenset(week_start + timedelta(days=i) for i in range(WEEK_DAYS))

    person_types = _grouped(
        session.execute(
            select(PersonAircraftType.person_id, PersonAircraftType.aircraft_type).where(
                PersonAircraftType.snapshot_id == snapshot_id
            )
        )
        .tuples()
        .all()
    )
    completed = _grouped(
        session.execute(
            select(PersonCompletedMission.person_id, PersonCompletedMission.mission_id).where(
                PersonCompletedMission.snapshot_id == snapshot_id
            )
        )
        .tuples()
        .all()
    )
    quals: dict[str, dict[str, Qualification]] = {}
    for qrow in session.execute(
        select(PersonQualification).where(PersonQualification.snapshot_id == snapshot_id)
    ).scalars():
        expiry = overrides.qual_expiry.get((qrow.person_id, qrow.mission_class), qrow.expiry_date)
        quals.setdefault(qrow.person_id, {})[qrow.mission_class] = Qualification(
            mission_class=qrow.mission_class, level=qrow.level, expiry=expiry
        )
    # 到期日覆盖也可能落在一条本不存在的资质上（构造场景），此处不无声新建资质：
    # 没有资质就没有候选，静态预筛会如实记下原因。

    unavailable: dict[str, set[date]] = {}
    for urow in session.execute(
        select(PersonUnavailability).where(PersonUnavailability.snapshot_id == snapshot_id)
    ).scalars():
        unavailable.setdefault(urow.person_id, set()).add(urow.unavailable_date)

    persons: dict[str, PersonInfo] = {}
    for prow_person in session.execute(
        select(Person).where(Person.snapshot_id == snapshot_id)
    ).scalars():
        days_off = set(unavailable.get(prow_person.person_id, set()))
        days_off |= set(overrides.unavailable.get(prow_person.person_id, frozenset()))
        if prow_person.person_id in overrides.unavailable_all_week:
            days_off |= week_dates
        persons[prow_person.person_id] = PersonInfo(
            person_id=prow_person.person_id,
            name=prow_person.name,
            identity=prow_person.identity,
            aircraft_types=person_types.get(prow_person.person_id, frozenset()),
            qualifications=quals.get(prow_person.person_id, {}),
            unavailable=frozenset(days_off),
            completed=completed.get(prow_person.person_id, frozenset()),
        )

    capability = _grouped(
        session.execute(
            select(
                AircraftMissionCapability.aircraft_id, AircraftMissionCapability.mission_id
            ).where(AircraftMissionCapability.snapshot_id == snapshot_id)
        )
        .tuples()
        .all()
    )
    maint: dict[str, list[MaintenanceWindow]] = {}
    for mrow in session.execute(
        select(AircraftMaintenance).where(AircraftMaintenance.snapshot_id == snapshot_id)
    ).scalars():
        maint.setdefault(mrow.aircraft_id, []).append(
            MaintenanceWindow(
                start=mrow.start_ts, end=mrow.end_ts, kind=mrow.kind, all_day=mrow.all_day
            )
        )
    for aircraft_id, first, last in overrides.maintenance_all_day:
        maint.setdefault(aircraft_id, []).append(
            MaintenanceWindow(
                start=datetime.combine(first, time(0, 0)),
                end=datetime.combine(last, time(23, 59, 59)),
                kind="构造扰动：全天维护",
                all_day=True,
            )
        )

    aircraft: dict[str, AircraftInfo] = {}
    for arow in session.execute(
        select(Aircraft).where(Aircraft.snapshot_id == snapshot_id)
    ).scalars():
        aircraft[arow.aircraft_id] = AircraftInfo(
            aircraft_id=arow.aircraft_id,
            aircraft_type=arow.aircraft_type,
            seats=arow.seats,
            window_start=arow.daily_window_start,
            window_end=arow.daily_window_end,
            turnaround_minutes=arow.turnaround_minutes,
            missions=capability.get(arow.aircraft_id, frozenset()),
            maintenance=tuple(
                sorted(maint.get(arow.aircraft_id, []), key=lambda w: (w.start, w.end))
            ),
        )

    mission_types = _grouped(
        session.execute(
            select(MissionAircraftType.mission_id, MissionAircraftType.aircraft_type).where(
                MissionAircraftType.snapshot_id == snapshot_id
            )
        )
        .tuples()
        .all()
    )
    prereqs: dict[str, list[tuple[str, str]]] = {}
    for prow in session.execute(
        select(MissionPrereq).where(MissionPrereq.snapshot_id == snapshot_id)
    ).scalars():
        prereqs.setdefault(prow.mission_id, []).append((prow.prereq_ref, prow.ref_kind))

    missions: dict[str, MissionInfo] = {}
    for misrow in session.execute(
        select(Mission).where(Mission.snapshot_id == snapshot_id)
    ).scalars():
        missions[misrow.mission_id] = MissionInfo(
            mission_id=misrow.mission_id,
            name=misrow.name,
            mission_class=misrow.mission_class,
            duration_minutes=misrow.duration_minutes,
            cycle_weeks=misrow.cycle_weeks,
            freq_days=misrow.freq_days,
            weekly_required=misrow.weekly_required,
            dual_required=misrow.dual_required,
            airspace_id=misrow.airspace_id,
            aircraft_types=mission_types.get(misrow.mission_id, frozenset()),
            prereqs=tuple(sorted(prereqs.get(misrow.mission_id, []))),
        )

    airspaces = {
        row.airspace_id: AirspaceInfo(
            airspace_id=row.airspace_id, name=row.name, capacity=row.capacity
        )
        for row in session.execute(
            select(Airspace).where(Airspace.snapshot_id == snapshot_id)
        ).scalars()
    }

    runway_types = _grouped(
        session.execute(
            select(RunwayAircraftType.runway_id, RunwayAircraftType.aircraft_type).where(
                RunwayAircraftType.snapshot_id == snapshot_id
            )
        )
        .tuples()
        .all()
    )
    runways = {
        row.runway_id: RunwayInfo(
            runway_id=row.runway_id,
            name=row.name,
            aircraft_types=runway_types.get(row.runway_id, frozenset()),
        )
        for row in session.execute(
            select(Runway).where(Runway.snapshot_id == snapshot_id)
        ).scalars()
    }

    progress: dict[tuple[str, str], ProgressInfo] = {}
    for tprow in session.execute(
        select(TrainingProgress).where(TrainingProgress.snapshot_id == snapshot_id)
    ).scalars():
        progress[tprow.person_id, tprow.mission_id] = ProgressInfo(
            person_id=tprow.person_id,
            mission_id=tprow.mission_id,
            cycle_start=tprow.cycle_start,
            status=tprow.status,
            completed_count=tprow.completed_count,
            last_done_date=tprow.last_done_date,
            cycle_weeks=tprow.cycle_weeks,
            debt_count=tprow.debt_count,
            prereq_met=tprow.prereq_met,
            blocked_reason=tprow.blocked_reason,
            is_recurrent=tprow.is_recurrent,
            recurrent_since=tprow.recurrent_since,
        )

    return ProblemData(
        snapshot_id=snapshot_id,
        week_start=week_start,
        window_start=overrides.window_start or window_start,
        window_end=overrides.window_end or window_end,
        persons=persons,
        aircraft=aircraft,
        missions=missions,
        airspaces=airspaces,
        runways=runways,
        progress=progress,
        overrides=overrides,
    )


__all__ = [
    "NO_OVERRIDES",
    "WEEK_DAYS",
    "AircraftInfo",
    "AirspaceInfo",
    "MaintenanceWindow",
    "MissionInfo",
    "PersonInfo",
    "ProblemData",
    "ProgressInfo",
    "Qualification",
    "RunwayInfo",
    "ScenarioOverrides",
    "load_problem_data",
]
