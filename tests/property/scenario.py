"""随机场景生成器 —— `arbitrary_scenario()`（v6 §12.1）。

## 一个场景要同时喂两条通道

属性测试 `test_solver_output_always_passes_validator` 的价值全在「两条通道看的是
**同一个世界**」上：求解器读 `ProblemData`（`backend.solver.data`），校验器读
`ValidationContext`（`backend.validator.context`），两个数据结构互不相干，是各自
从 PG 装配出来的。所以本模块的做法是：

    ScenarioSpec（唯一事实源）
        ├─ .to_bundle()              → SpecBundle（求解器侧）
        └─ .to_validation_context()  → ValidationContext（校验器侧）

**两次投影都从同一个 `ScenarioSpec` 出发**，扰动（请假 / 维修 / 资质到期 /
空域容量降为 0 / 跑道关闭）在投影之前就已并入实体，于是「求解器看到 AC701 在修、
校验器没看到」这类假绿是不可能出现的。

## 自洽（引用完整性）由构造保证

生成器不是「先随机再修补」，而是**先定骨架再随机填参数**：

- 课目的 `airspace_id` 只从已生成的空域里选；
- 课目的机型集只从已生成的机型里选，且**保证至少一架在册飞机是该机型**；
- 飞机的 `capable_missions` 是「机型匹配的课目」的**超集截断**，永远是已有课目；
- 跑道的服务机型只从已生成的机型里选，且**保证每个机型至少有一条跑道**；
- 人员的类别资质只覆盖已生成的课目类别，机型资质只取已生成的机型；
- 先修引用只指向**下标更小**的课目或其类别 → 天然无环；
- `training_progress` 只为「该人持有该课目类别资质」的组合建行 —— 这与 M1 摄取
  落库的形态一致（基准快照里 4 名学员各 7 行、其余各 12 行）。

`tests/property/test_scenario_generator.py` 把上面每一条都做成了断言。

## 规模刻意压得很小

3~6 人 / 1~3 机 / 3~4 课目 / 7 天，求解一次约 **150 毫秒**（实测），500 个样本
两分钟量级。基准周那种 2276 候选的算例一次要 20 秒，属性测试里跑不动。

## 编号与机型一律非基准取值

人员 `P4xx`（三位）、机号 `AC7xx`、机型 `TX-1`/`TX-2`、空域 `LAC`/`LAD`/`NAV`/`BLD`、
跑道 `RWY-7`/`RWY-8` —— 与 `data/origin/` 一个都不重合（CLAUDE.md §11、v6 §5.1.1）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta

from hypothesis import strategies as st

from backend.core.ruleset import (
    IDENTITY_INSTRUCTOR,
    IDENTITY_MATURE,
    IDENTITY_STUDENT,
    LEVEL_DUAL,
    LEVEL_INSTRUCTOR,
    LEVEL_SOLO,
    Ruleset,
    Semantics,
    get_ruleset,
    get_semantics,
    req_max_for,
)
from backend.nodes.compile_spec import SpecBundle
from backend.schemas.intent import ConstraintSpec, ObjectiveWeights
from backend.solver import data as sd
from backend.validator import context as vc

#: 合成世界的周一。刻意避开基准周 2026-01-05。
SCENARIO_WEEK_START: date = date(2026, 3, 2)

#: 合成机型（基准数据里没有）
TYPE_A: str = "TX-1"
TYPE_B: str = "TX-2"

#: 合成空域与跑道编号
AIRSPACE_IDS: tuple[str, ...] = ("LAC", "LAD", "NAV", "BLD")
RUNWAY_IDS: tuple[str, ...] = ("RWY-7", "RWY-8")

WEEK_DAYS: int = 7


# ─────────────────────────────────────────────────────────────────────
# 场景描述（唯一事实源）
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PersonSpec:
    person_id: str
    name: str
    identity: str
    aircraft_types: tuple[str, ...]
    #: 类别 → 等级（教员/带飞/单飞）
    levels: Mapping[str, str]
    #: 类别 → 资质到期日（None 表示不到期）
    expiry: Mapping[str, date] = field(default_factory=dict)
    completed: tuple[str, ...] = ()
    unavailable: tuple[date, ...] = ()


@dataclass(frozen=True)
class AircraftSpec:
    aircraft_id: str
    aircraft_type: str
    seats: int
    turnaround_minutes: int
    capable_missions: tuple[str, ...]
    #: 全天维护的日期
    maintenance_days: tuple[date, ...] = ()


@dataclass(frozen=True)
class MissionSpec:
    mission_id: str
    name: str
    mission_class: str
    duration_minutes: int
    freq_days: int
    dual_required: bool
    weekly_required: bool
    airspace_id: str
    aircraft_types: tuple[str, ...]
    prereqs: tuple[tuple[str, str], ...] = ()
    cycle_weeks: int = 16


@dataclass(frozen=True)
class AirspaceSpec:
    airspace_id: str
    name: str
    capacity: int


@dataclass(frozen=True)
class RunwaySpec:
    runway_id: str
    name: str
    aircraft_types: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioSpec:
    """一个自洽的小世界。**求解器与校验器都从这里投影出去。**"""

    label: str
    week_start: date
    persons: tuple[PersonSpec, ...]
    aircraft: tuple[AircraftSpec, ...]
    missions: tuple[MissionSpec, ...]
    airspaces: tuple[AirspaceSpec, ...]
    runways: tuple[RunwaySpec, ...]
    window_start: time = time(6, 0)
    window_end: time = time(18, 0)
    #: 空域容量覆盖（关闭 = 0）
    airspace_capacity_override: Mapping[str, int] = field(default_factory=dict)
    #: 关闭的跑道
    closed_runways: tuple[str, ...] = ()
    snapshot_id: str = "snap_prop00000"
    time_limit_s: float = 10.0
    seed: int = 42
    workers: int = 4
    relaxation_tier: int = 0

    # ── 派生 ────────────────────────────────────────────────────────
    @property
    def week_dates(self) -> tuple[date, ...]:
        return tuple(self.week_start + timedelta(days=i) for i in range(WEEK_DAYS))

    @property
    def mission_map(self) -> dict[str, MissionSpec]:
        return {m.mission_id: m for m in self.missions}

    @property
    def aircraft_types(self) -> tuple[str, ...]:
        return tuple(sorted({a.aircraft_type for a in self.aircraft}))

    def overrides(self) -> sd.ScenarioOverrides:
        """扰动对象。**人员不可用 / 维护 / 到期日已经并进实体了**，这里只留下
        求解器要在建模阶段消费的三项（窗口、空域容量、跑道关闭）。"""
        return sd.ScenarioOverrides(
            window_start=self.window_start,
            window_end=self.window_end,
            airspace_capacity=dict(self.airspace_capacity_override),
            closed_runways=frozenset(self.closed_runways),
        )

    def effective_capacity(self, airspace_id: str) -> int:
        if airspace_id in self.airspace_capacity_override:
            return self.airspace_capacity_override[airspace_id]
        for space in self.airspaces:
            if space.airspace_id == airspace_id:
                return space.capacity
        return 0

    def open_runways(self) -> tuple[RunwaySpec, ...]:
        return tuple(r for r in self.runways if r.runway_id not in self.closed_runways)

    def progress_keys(self) -> tuple[tuple[str, str], ...]:
        """`training_progress` 的行集合 —— 只为「持有该课目类别资质」的组合建行。"""
        out: list[tuple[str, str]] = []
        for person in self.persons:
            for mission in self.missions:
                if mission.mission_class in person.levels:
                    out.append((person.person_id, mission.mission_id))
        return tuple(sorted(out))

    # ── 投影 1：求解器侧 ─────────────────────────────────────────────
    def to_problem_data(self) -> sd.ProblemData:
        persons = {
            p.person_id: sd.PersonInfo(
                person_id=p.person_id,
                name=p.name,
                identity=p.identity,
                aircraft_types=frozenset(p.aircraft_types),
                qualifications={
                    cls: sd.Qualification(mission_class=cls, level=level, expiry=p.expiry.get(cls))
                    for cls, level in sorted(p.levels.items())
                },
                unavailable=frozenset(p.unavailable),
                completed=frozenset(p.completed),
            )
            for p in self.persons
        }
        aircraft = {
            a.aircraft_id: sd.AircraftInfo(
                aircraft_id=a.aircraft_id,
                aircraft_type=a.aircraft_type,
                seats=a.seats,
                window_start=self.window_start,
                window_end=self.window_end,
                turnaround_minutes=a.turnaround_minutes,
                missions=frozenset(a.capable_missions),
                maintenance=tuple(
                    sd.MaintenanceWindow(
                        start=datetime.combine(day, time(0, 0)),
                        end=datetime.combine(day, time(23, 59, 59)),
                        kind="定检维护",
                        all_day=True,
                    )
                    for day in sorted(a.maintenance_days)
                ),
            )
            for a in self.aircraft
        }
        missions = {
            m.mission_id: sd.MissionInfo(
                mission_id=m.mission_id,
                name=m.name,
                mission_class=m.mission_class,
                duration_minutes=m.duration_minutes,
                cycle_weeks=m.cycle_weeks,
                freq_days=m.freq_days,
                weekly_required=m.weekly_required,
                dual_required=m.dual_required,
                airspace_id=m.airspace_id,
                aircraft_types=frozenset(m.aircraft_types),
                prereqs=tuple(sorted(m.prereqs)),
            )
            for m in self.missions
        }
        airspaces = {
            s.airspace_id: sd.AirspaceInfo(
                airspace_id=s.airspace_id, name=s.name, capacity=s.capacity
            )
            for s in self.airspaces
        }
        runways = {
            r.runway_id: sd.RunwayInfo(
                runway_id=r.runway_id, name=r.name, aircraft_types=frozenset(r.aircraft_types)
            )
            for r in self.runways
        }
        progress = {
            (person_id, mission_id): sd.ProgressInfo(
                person_id=person_id,
                mission_id=mission_id,
                cycle_start=self.week_start,
                status=self._status_of(person_id, mission_id),
                completed_count=1 if self._is_done(person_id, mission_id) else 0,
                last_done_date=None,
                cycle_weeks=self.mission_map[mission_id].cycle_weeks,
                debt_count=0,
                prereq_met=self._prereq_met(person_id, mission_id)[0],
                blocked_reason=self._prereq_met(person_id, mission_id)[1],
                is_recurrent=self._is_recurrent(person_id, mission_id),
                recurrent_since=self._recurrent_since(person_id, mission_id),
            )
            for person_id, mission_id in self.progress_keys()
        }
        return sd.ProblemData(
            snapshot_id=self.snapshot_id,
            week_start=self.week_start,
            window_start=self.window_start,
            window_end=self.window_end,
            persons=persons,
            aircraft=aircraft,
            missions=missions,
            airspaces=airspaces,
            runways=runways,
            progress=progress,
            overrides=self.overrides(),
        )

    def to_spec(self, data: sd.ProblemData | None = None) -> ConstraintSpec:
        problem = data or self.to_problem_data()
        rules = get_ruleset()
        sem = get_semantics()
        return ConstraintSpec(
            snapshot_id=self.snapshot_id,
            ruleset_version=rules.version,
            semantics_version=sem.version,
            semantics_switches=sem.snapshot(),
            iso_week=problem.iso_week,
            week_start=self.week_start,
            week_end=problem.week_end,
            scope_persons="ALL",
            scope_missions="ALL",
            relaxation_tier=self.relaxation_tier,
            objective_weights=ObjectiveWeights(progress=1.0, disruption=1.0, balance=1.0),
            incremental_constraints=[],
            runway_model="dual_runway" if sem.s05_dual_runway else "single_runway",
            runways={
                r.runway_id: sorted(r.aircraft_types)
                for r in sorted(self.open_runways(), key=lambda x: x.runway_id)
            },
            density_scope=dict(sem.s05_density_scope),
            airspace_capacity={
                s.airspace_id: self.effective_capacity(s.airspace_id)
                for s in sorted(self.airspaces, key=lambda x: x.airspace_id)
            },
            freq_days={m.mission_id: m.freq_days for m in self.missions},
            req_max={m.mission_id: req_max_for(m.freq_days) for m in self.missions},
            solver_seed=self.seed,
            solver_workers=self.workers,
            solver_time_limit_s=self.time_limit_s,
        )

    def to_bundle(self) -> SpecBundle:
        data = self.to_problem_data()
        return SpecBundle(
            spec=self.to_spec(data),
            data=data,
            ruleset=get_ruleset(),
            semantics=get_semantics(),
        )

    # ── 投影 2：校验器侧 ─────────────────────────────────────────────
    def to_validation_context(
        self, *, ruleset: Ruleset | None = None, semantics: Semantics | None = None
    ) -> vc.ValidationContext:
        """**与 `to_problem_data` 完全并行的第二次投影。**

        扰动在这里同样生效：空域容量覆盖直接写进 `AirspaceFacts.capacity`、
        关闭的跑道**从跑道表里删掉**（校验器看到一个不在册的跑道就会报 C09，
        这正是「跑道关闭」应有的语义）。
        """
        persons = {
            p.person_id: vc.PersonFacts(
                person_id=p.person_id,
                name=p.name,
                identity=p.identity,
                aircraft_types=frozenset(p.aircraft_types),
                qualifications={
                    cls: vc.QualificationFacts(
                        mission_class=cls, level=level, expiry_date=p.expiry.get(cls)
                    )
                    for cls, level in sorted(p.levels.items())
                },
                unavailable_dates=frozenset(p.unavailable),
                completed_missions=frozenset(p.completed),
            )
            for p in self.persons
        }
        aircraft = {
            a.aircraft_id: vc.AircraftFacts(
                aircraft_id=a.aircraft_id,
                aircraft_type=a.aircraft_type,
                seats=a.seats,
                turnaround_minutes=a.turnaround_minutes,
                daily_window_start=self.window_start,
                daily_window_end=self.window_end,
                capable_missions=frozenset(a.capable_missions),
                maintenance=tuple(
                    vc.MaintenanceWindow(
                        start=datetime.combine(day, time(0, 0)),
                        end=datetime.combine(day, time(23, 59, 59)),
                        kind="定检维护",
                        all_day=True,
                    )
                    for day in sorted(a.maintenance_days)
                ),
            )
            for a in self.aircraft
        }
        missions = {
            m.mission_id: vc.MissionFacts(
                mission_id=m.mission_id,
                name=m.name,
                mission_class=m.mission_class,
                duration_minutes=m.duration_minutes,
                freq_days=m.freq_days,
                cycle_weeks=m.cycle_weeks,
                dual_required=m.dual_required,
                weekly_required=m.weekly_required,
                airspace_id=m.airspace_id,
                aircraft_types=frozenset(m.aircraft_types),
                prereqs=tuple(
                    sorted(
                        (vc.PrereqRef(ref=ref, kind=kind) for ref, kind in m.prereqs),
                        key=lambda p: p.ref,
                    )
                ),
            )
            for m in self.missions
        }
        airspaces = {
            s.airspace_id: vc.AirspaceFacts(
                airspace_id=s.airspace_id,
                name=s.name,
                capacity=self.effective_capacity(s.airspace_id),
            )
            for s in self.airspaces
        }
        runways = {
            r.runway_id: vc.RunwayFacts(
                runway_id=r.runway_id, name=r.name, aircraft_types=frozenset(r.aircraft_types)
            )
            for r in self.open_runways()
        }
        progress = {
            (person_id, mission_id): vc.ProgressFacts(
                person_id=person_id,
                mission_id=mission_id,
                status=self._status_of(person_id, mission_id),
                cycle_start=self.week_start,
                last_done_date=None,
                debt_count=0,
                prereq_met=self._prereq_met(person_id, mission_id)[0],
                blocked_reason=self._prereq_met(person_id, mission_id)[1],
                is_recurrent=self._is_recurrent(person_id, mission_id),
                recurrent_since=self._recurrent_since(person_id, mission_id),
            )
            for person_id, mission_id in self.progress_keys()
        }
        return vc.ValidationContext(
            week_start=self.week_start,
            persons=persons,
            aircraft=aircraft,
            missions=missions,
            airspaces=airspaces,
            runways=runways,
            progress=progress,
            ruleset=ruleset or get_ruleset(),
            semantics=semantics or get_semantics(),
            snapshot_id=self.snapshot_id,
        )

    # ── 进度推导（两次投影共用，保证两边一致）────────────────────────
    def _person(self, person_id: str) -> PersonSpec:
        return next(p for p in self.persons if p.person_id == person_id)

    def _is_done(self, person_id: str, mission_id: str) -> bool:
        return mission_id in self._person(person_id).completed

    def _status_of(self, person_id: str, mission_id: str) -> str:
        return "COMPLETED" if self._is_done(person_id, mission_id) else "NOT_STARTED"

    def _prereq_met(self, person_id: str, mission_id: str) -> tuple[bool, str | None]:
        """S-01：先修写「X类」时，该类**全部**课目完成才算达标。"""
        person = self._person(person_id)
        done = set(person.completed)
        missing: list[str] = []
        for ref, kind in self.mission_map[mission_id].prereqs:
            if kind == "class":
                cls = ref[0] if ref.endswith("类") else ref
                for other in self.missions:
                    if other.mission_class == cls and other.mission_id not in done:
                        missing.append(other.mission_id)
            elif ref not in done:
                missing.append(ref)
        if not missing:
            return True, None
        ordered = sorted(set(missing))
        return False, "、".join(f"{m} 未完成" for m in ordered)

    def _recurrent_since(self, person_id: str, mission_id: str) -> date | None:
        """S-11：成熟飞行员的到期资质自**到期次日**起进入复训周期。"""
        sem = get_semantics()
        if not sem.s11_enabled:
            return None
        person = self._person(person_id)
        if person.identity not in sem.s11_identities:
            return None
        expiry = person.expiry.get(self.mission_map[mission_id].mission_class)
        if expiry is None:
            return None
        since = expiry + timedelta(days=sem.s11_start_offset_days)
        week_end = self.week_start + timedelta(days=WEEK_DAYS - 1)
        return since if since <= week_end else None

    def _is_recurrent(self, person_id: str, mission_id: str) -> bool:
        return self._recurrent_since(person_id, mission_id) is not None

    # ── 便捷改写（构造派生场景用）───────────────────────────────────
    def with_(self, **changes: object) -> ScenarioSpec:
        return replace(self, **changes)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────
# 骨架构造：先定结构，再随机填参数
# ─────────────────────────────────────────────────────────────────────
def build_scenario(
    *,
    label: str = "scenario",
    n_instructors: int = 1,
    n_students: int = 2,
    n_mature: int = 0,
    n_aircraft: int = 2,
    n_missions: int = 3,
    two_types: bool = False,
    seats: int = 2,
    turnaround: int = 20,
    airspace_capacity: int = 1,
    durations: Sequence[int] = (30, 27, 40, 35),
    freq_days: Sequence[int] = (3, 3, 7, 7),
    week_start: date = SCENARIO_WEEK_START,
    window: tuple[time, time] = (time(6, 0), time(18, 0)),
    completed_depth: int = 2,
    unavailable: Mapping[str, tuple[date, ...]] = {},
    maintenance: Mapping[str, tuple[date, ...]] = {},
    expiry: Mapping[tuple[str, str], date] = {},
    airspace_capacity_override: Mapping[str, int] = {},
    closed_runways: tuple[str, ...] = (),
    time_limit_s: float = 10.0,
) -> ScenarioSpec:
    """造一个自洽的小世界。

    课目骨架固定为「A 类两门单飞（每周必飞）+ B/C 类带飞（带先修链）」，
    这样 D-1（学员 A 类单飞）、S-01（类达标）、S-02（A 类合并计数）、
    约束13 的滑窗都能在同一个算例里被踩到。参数（人数、机数、时长、频率、
    容量、扰动）随机。
    """
    types = (TYPE_A, TYPE_B) if two_types else (TYPE_A,)
    n_missions = max(2, min(n_missions, 4))

    # ── 空域：每门课目一个，容量随机 ────────────────────────────────
    airspaces = tuple(
        AirspaceSpec(airspace_id=AIRSPACE_IDS[i], name=f"空域{i}", capacity=airspace_capacity)
        for i in range(n_missions)
    )

    # ── 课目：A-1 / A-2 单飞每周必飞；B-1 / C-1 带飞且带先修 ─────────
    shapes: list[tuple[str, str, str, bool, bool, tuple[tuple[str, str], ...]]] = [
        ("missionA-1", "本场起落", "A", False, True, ()),
        ("missionA-2", "本场起落二", "A", False, True, ()),
        ("missionB-1", "导航飞行", "B", True, False, (("A类", "class"),)),
        ("missionC-1", "仪表飞行", "C", True, False, (("missionB-1", "mission"),)),
    ]
    missions: list[MissionSpec] = []
    for i in range(n_missions):
        mid, name, cls, dual, weekly, prereqs = shapes[i]
        missions.append(
            MissionSpec(
                mission_id=mid,
                name=name,
                mission_class=cls,
                duration_minutes=durations[i % len(durations)],
                freq_days=freq_days[i % len(freq_days)],
                dual_required=dual,
                weekly_required=weekly,
                airspace_id=airspaces[i].airspace_id,
                aircraft_types=types,
                prereqs=prereqs,
            )
        )
    mission_ids = tuple(m.mission_id for m in missions)
    classes = tuple(sorted({m.mission_class for m in missions}))

    # ── 飞机：机型轮流，保证每个机型至少一架 ─────────────────────────
    aircraft = tuple(
        AircraftSpec(
            aircraft_id=f"AC70{i + 1}",
            aircraft_type=types[i % len(types)],
            seats=seats,
            turnaround_minutes=turnaround,
            capable_missions=mission_ids,
            maintenance_days=tuple(sorted(maintenance.get(f"AC70{i + 1}", ()))),
        )
        for i in range(max(len(types), n_aircraft))
    )
    present_types = tuple(sorted({a.aircraft_type for a in aircraft}))

    # ── 跑道：RWY-7 服务全部机型，RWY-8 只服务第一种（S-05 的形状）──
    runways = (
        RunwaySpec(runway_id="RWY-7", name="跑道七", aircraft_types=present_types),
        RunwaySpec(runway_id="RWY-8", name="跑道八", aircraft_types=(present_types[0],)),
    )

    # ── 人员 ────────────────────────────────────────────────────────
    persons: list[PersonSpec] = []
    for i in range(n_instructors):
        pid = f"P40{i + 1}"
        persons.append(
            PersonSpec(
                person_id=pid,
                name=f"教员{i + 1}",
                identity=IDENTITY_INSTRUCTOR,
                aircraft_types=present_types,
                levels=dict.fromkeys(classes, LEVEL_INSTRUCTOR),
                expiry={cls: expiry[(pid, cls)] for cls in classes if (pid, cls) in expiry},
                completed=mission_ids,
                unavailable=tuple(sorted(unavailable.get(pid, ()))),
            )
        )
    for i in range(n_mature):
        pid = f"P42{i + 1}"
        persons.append(
            PersonSpec(
                person_id=pid,
                name=f"成熟{i + 1}",
                identity=IDENTITY_MATURE,
                aircraft_types=present_types,
                levels=dict.fromkeys(classes, LEVEL_SOLO),
                expiry={cls: expiry[(pid, cls)] for cls in classes if (pid, cls) in expiry},
                completed=mission_ids,
                unavailable=tuple(sorted(unavailable.get(pid, ()))),
            )
        )
    for i in range(n_students):
        pid = f"P41{i + 1}"
        # 已完成课目取前 `completed_depth` 门 → 先修链上留出 BLOCKED 的余地
        depth = max(0, min(completed_depth, len(mission_ids)))
        persons.append(
            PersonSpec(
                person_id=pid,
                name=f"学员{i + 1}",
                identity=IDENTITY_STUDENT,
                aircraft_types=(present_types[0],),
                levels={cls: (LEVEL_SOLO if cls == "A" else LEVEL_DUAL) for cls in classes},
                expiry={cls: expiry[(pid, cls)] for cls in classes if (pid, cls) in expiry},
                completed=mission_ids[:depth],
                unavailable=tuple(sorted(unavailable.get(pid, ()))),
            )
        )

    return ScenarioSpec(
        label=label,
        week_start=week_start,
        persons=tuple(persons),
        aircraft=aircraft,
        missions=tuple(missions),
        airspaces=airspaces,
        runways=runways,
        window_start=window[0],
        window_end=window[1],
        airspace_capacity_override=dict(airspace_capacity_override),
        closed_runways=closed_runways,
        time_limit_s=time_limit_s,
    )


# ─────────────────────────────────────────────────────────────────────
# Hypothesis 策略
# ─────────────────────────────────────────────────────────────────────
def _days(week_start: date) -> tuple[date, ...]:
    return tuple(week_start + timedelta(days=i) for i in range(WEEK_DAYS))


@st.composite
def person_unavailability(
    draw: st.DrawFn, person_ids: Sequence[str]
) -> dict[str, tuple[date, ...]]:
    """随机请假（v6 §12.3 单点扰动之一）。"""
    out: dict[str, tuple[date, ...]] = {}
    for pid in person_ids:
        n = draw(st.integers(min_value=0, max_value=2))
        if n:
            picked = draw(
                st.lists(
                    st.sampled_from(_days(SCENARIO_WEEK_START)), min_size=n, max_size=n, unique=True
                )
            )
            out[pid] = tuple(sorted(picked))
    return out


@st.composite
def aircraft_maintenance(
    draw: st.DrawFn, aircraft_ids: Sequence[str]
) -> dict[str, tuple[date, ...]]:
    """随机维修（v6 §12.3 单点扰动之一）。**不会把全部飞机都封死** —— 那属于 I2。"""
    out: dict[str, tuple[date, ...]] = {}
    for aid in aircraft_ids[:-1]:  # 至少留一架
        n = draw(st.integers(min_value=0, max_value=2))
        if n:
            picked = draw(
                st.lists(
                    st.sampled_from(_days(SCENARIO_WEEK_START)), min_size=n, max_size=n, unique=True
                )
            )
            out[aid] = tuple(sorted(picked))
    return out


@st.composite
def arbitrary_scenario(
    draw: st.DrawFn,
    *,
    allow_closures: bool = True,
    max_students: int = 3,
) -> ScenarioSpec:
    """v6 §12.1 的 `arbitrary_scenario()`：随机人员/飞机/**空域/跑道**/异常组合。

    覆盖的异常：请假、维修、资质到期、**空域容量降为 0**、**跑道关闭**。
    生成的场景一律自洽（引用完整性由 :func:`build_scenario` 的骨架保证）。

    ## S-11 的一类多门

    成熟飞行员的到期类别里有两门以上课目时，S-11 的粒度曾经在 v6 内部自相矛盾
    （§3.2 约束13 读成「每门课各自 7 天滑窗」、§12.3 的验收断言读成「该类任一门
    即可」），M2-C 的属性测试把它作为 **FTS-3003** 报了出来。业务方 2026-08-12
    裁定取**类别**粒度，两侧已对齐（见 `tests/property/test_s11_class_scope.py`），
    因此生成器不再回避这种形状 —— 它现在是被覆盖的正常样本。
    """
    n_students = draw(st.integers(min_value=1, max_value=max_students))
    n_instructors = draw(st.integers(min_value=1, max_value=2))
    n_mature = draw(st.integers(min_value=0, max_value=1))
    two_types = draw(st.booleans())
    n_aircraft = draw(st.integers(min_value=1, max_value=3))
    n_missions = draw(st.integers(min_value=2, max_value=4))
    seats = draw(st.integers(min_value=2, max_value=3))
    turnaround = draw(st.sampled_from([10, 20, 30, 40]))
    capacity = draw(st.integers(min_value=1, max_value=2))
    completed_depth = draw(st.integers(min_value=0, max_value=n_missions))
    durations = draw(st.lists(st.integers(min_value=20, max_value=70), min_size=4, max_size=4))
    freqs = draw(st.lists(st.sampled_from([3, 7, 14]), min_size=4, max_size=4))
    window_end_hour = draw(st.sampled_from([10, 14, 18]))

    person_ids = (
        [f"P40{i + 1}" for i in range(n_instructors)]
        + [f"P42{i + 1}" for i in range(n_mature)]
        + [f"P41{i + 1}" for i in range(n_students)]
    )
    aircraft_ids = [f"AC70{i + 1}" for i in range(max(2 if two_types else 1, n_aircraft))]

    unavailable = draw(person_unavailability(person_ids))
    maintenance = draw(aircraft_maintenance(aircraft_ids))

    # 资质到期（S-11 的入口：成熟飞行员到期 → 复训；学员/教员到期 → 字面剔除）
    #
    # 类别 → 本场景里该类有几门课目：A 类恒为 2 门（A-1/A-2），B/C 各 1 门。
    classes_present = {"A": min(2, n_missions), "B": 1 if n_missions >= 3 else 0}
    if n_missions >= 4:
        classes_present["C"] = 1
    expiry: dict[tuple[str, str], date] = {}
    if draw(st.booleans()):
        pid = draw(st.sampled_from(person_ids))
        options = [cls for cls, count in classes_present.items() if count >= 1]
        if options:
            cls = draw(st.sampled_from(sorted(options)))
            offset = draw(st.integers(min_value=-3, max_value=6))
            expiry[(pid, cls)] = SCENARIO_WEEK_START + timedelta(days=offset)

    cap_override: dict[str, int] = {}
    closed: tuple[str, ...] = ()
    if allow_closures:
        if draw(st.booleans()):
            # 空域容量降为 0（v6 §3.4 的「空域关闭」）
            idx = draw(st.integers(min_value=0, max_value=n_missions - 1))
            cap_override[AIRSPACE_IDS[idx]] = 0
        if draw(st.integers(min_value=0, max_value=4)) == 0:
            # 跑道关闭 —— 只关一条，另一条仍在（全关属于 I5）
            closed = (draw(st.sampled_from(RUNWAY_IDS)),)

    return build_scenario(
        label="arbitrary",
        n_instructors=n_instructors,
        n_students=n_students,
        n_mature=n_mature,
        n_aircraft=n_aircraft,
        n_missions=n_missions,
        two_types=two_types,
        seats=seats,
        turnaround=turnaround,
        airspace_capacity=capacity,
        durations=durations,
        freq_days=freqs,
        completed_depth=completed_depth,
        window=(time(6, 0), time(window_end_hour, 0)),
        unavailable=unavailable,
        maintenance=maintenance,
        expiry=expiry,
        airspace_capacity_override=cap_override,
        closed_runways=closed,
        time_limit_s=10.0,
    )


__all__ = [
    "AIRSPACE_IDS",
    "RUNWAY_IDS",
    "SCENARIO_WEEK_START",
    "TYPE_A",
    "TYPE_B",
    "WEEK_DAYS",
    "AircraftSpec",
    "AirspaceSpec",
    "MissionSpec",
    "PersonSpec",
    "RunwaySpec",
    "ScenarioSpec",
    "aircraft_maintenance",
    "arbitrary_scenario",
    "build_scenario",
    "person_unavailability",
]
