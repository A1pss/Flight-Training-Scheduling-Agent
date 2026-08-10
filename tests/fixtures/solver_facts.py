"""求解器单元测试用的**手工构造**实体集，不连库、不用基准数据。

## 为什么手工造而不是读基准快照

两个理由，都很实在：

1. **速度**：基准周 2276 个候选、求解 ~19 秒。单元测试要跑几十个用例，
   必须有一个「十几个候选、毫秒级」的算例。
2. **证明没写死基准数据**：这里的人员编号、机型名、机队规模、课目数量
   **全都和 `data/origin/` 那批不一样**（机型叫 `TX-1`，人员是 P41/P42，
   只有 1 架飞机、3 门课目）。求解器要是偷偷依赖了 8 人/8 机/`JL-8`，
   这套算例第一个跑不过（CLAUDE.md §11、v6 §5.1.1）。

## 唯一沿用基准取值的两处，以及为什么

`airspace_id` 与 `runway_id` 沿用 `SAA`/`RT2`/`RWY-1` —— **不是偷懒**：
v6 附录 B 把 `Sortie.airspace_id` 与 `Sortie.runway_id` 定义成基准取值的
`Literal`，`person_id` 也钉了 `^P\\d{2}$`。要造出能通过契约校验的 `Sortie`，
这几处只能落在那个集合里。**这与 v6 §5.1.1「编号与机型由数据决定」相互矛盾**，
已列入收工报告的待裁定问题（附录 B 是冻结契约，本窗口不擅自改）。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from backend.core.ruleset import (
    IDENTITY_INSTRUCTOR,
    IDENTITY_MATURE,
    IDENTITY_STUDENT,
    LEVEL_DUAL,
    LEVEL_INSTRUCTOR,
    LEVEL_SOLO,
    get_ruleset,
    get_semantics,
    req_max_for,
)
from backend.nodes.compile_spec import SpecBundle
from backend.schemas.intent import ConstraintSpec, ObjectiveWeights
from backend.solver.data import (
    NO_OVERRIDES,
    AircraftInfo,
    AirspaceInfo,
    MaintenanceWindow,
    MissionInfo,
    PersonInfo,
    ProblemData,
    ProgressInfo,
    Qualification,
    RunwayInfo,
    ScenarioOverrides,
)

#: 合成算例的周一。刻意不是基准周，避免测试和基准数据的日期纠缠。
TEST_WEEK_START: date = date(2026, 3, 2)

#: 合成算例的机型名 —— 基准数据里没有这个机型
TEST_TYPE: str = "TX-1"


def _qual(mission_class: str, level: str, expiry: date | None = None) -> Qualification:
    return Qualification(mission_class=mission_class, level=level, expiry=expiry)


def all_day(when: date) -> MaintenanceWindow:
    """把某一天整天封死的维护窗。"""
    return MaintenanceWindow(
        start=datetime.combine(when, time(0, 0)),
        end=datetime.combine(when, time(23, 59, 59)),
        kind="定检维护",
        all_day=True,
    )


def make_problem(
    *,
    week_start: date = TEST_WEEK_START,
    student_completed: frozenset[str] = frozenset({"missionA-1"}),
    with_mature: bool = False,
    mature_expiry: date | None = None,
    turnaround: int = 20,
    seats: int = 2,
    window: tuple[time, time] = (time(6, 0), time(18, 0)),
    maintenance: tuple[MaintenanceWindow, ...] = (),
    overrides: ScenarioOverrides = NO_OVERRIDES,
    aircraft_count: int = 1,
    airspace_capacity: int = 1,
    student_unavailable: frozenset[date] = frozenset(),
    progress_overrides: dict[tuple[str, str], dict[str, object]] | None = None,
) -> ProblemData:
    """一个「1 教员 + 1 学员 + N 架 TX-1 + 3 门课目」的迷你训练点。

    课目设计刻意覆盖三种编成与一条先修链：

    - `missionA-1` / `missionA-2`：A 类、带飞=否 → **学员单飞**（D-1 的形态），
      每周必飞、freq 3。两门同类课目是为了让 S-01「该类**全部**课目完成」有戏可演
    - `missionB-1`：B 类、带飞=是 → **1 教员 + 1 学员**，freq 7，先修 `missionA-1`（课目引用）
    - `missionC-1`：C 类、带飞=是，先修「A类」（**类别引用**）→ 只完成 A-1 时 **BLOCKED**
    """

    def _off(person_id: str, base: frozenset[date]) -> frozenset[date]:
        """把 `ScenarioOverrides` 的不可用日期合进来。

        `load_problem_data` 对真实快照做的就是这件事；合成算例必须**同样**应用
        扰动，否则「扰动后重排」这类测试会拿到一个没被扰动过的世界。
        """
        extra = set(overrides.unavailable.get(person_id, frozenset()))
        if person_id in overrides.unavailable_all_week:
            extra |= {week_start + timedelta(days=i) for i in range(7)}
        return frozenset(base | extra)

    def _expiry(person_id: str, mission_class: str, base: date | None) -> date | None:
        return overrides.qual_expiry.get((person_id, mission_class), base)

    persons: dict[str, PersonInfo] = {
        "P41": PersonInfo(
            person_id="P41",
            name="教员甲",
            identity=IDENTITY_INSTRUCTOR,
            aircraft_types=frozenset({TEST_TYPE}),
            qualifications={
                cls: _qual(cls, LEVEL_INSTRUCTOR, _expiry("P41", cls, None))
                for cls in ("A", "B", "C")
            },
            unavailable=_off("P41", frozenset()),
            completed=frozenset({"missionA-1", "missionA-2", "missionB-1", "missionC-1"}),
        ),
        "P42": PersonInfo(
            person_id="P42",
            name="学员乙",
            identity=IDENTITY_STUDENT,
            aircraft_types=frozenset({TEST_TYPE}),
            qualifications={
                "A": _qual("A", LEVEL_SOLO, _expiry("P42", "A", None)),
                "B": _qual("B", LEVEL_DUAL, _expiry("P42", "B", None)),
                "C": _qual("C", LEVEL_DUAL, _expiry("P42", "C", None)),
            },
            unavailable=_off("P42", student_unavailable),
            completed=student_completed,
        ),
    }
    if with_mature:
        persons["P43"] = PersonInfo(
            person_id="P43",
            name="成熟丙",
            identity=IDENTITY_MATURE,
            aircraft_types=frozenset({TEST_TYPE}),
            qualifications={
                "A": _qual("A", LEVEL_SOLO, _expiry("P43", "A", None)),
                "B": _qual("B", LEVEL_SOLO, _expiry("P43", "B", None)),
                "C": _qual("C", LEVEL_SOLO, _expiry("P43", "C", mature_expiry)),
            },
            unavailable=_off("P43", frozenset()),
            completed=frozenset({"missionA-1", "missionA-2", "missionB-1", "missionC-1"}),
        )

    extra_maint: dict[str, list[MaintenanceWindow]] = {}
    for aircraft_id, first, last in overrides.maintenance_all_day:
        span = (last - first).days + 1
        extra_maint.setdefault(aircraft_id, []).extend(
            all_day(first + timedelta(days=d)) for d in range(max(1, span))
        )

    aircraft = {
        f"AC7{i}": AircraftInfo(
            aircraft_id=f"AC7{i}",
            aircraft_type=TEST_TYPE,
            seats=seats,
            window_start=window[0],
            window_end=window[1],
            turnaround_minutes=turnaround,
            missions=frozenset({"missionA-1", "missionA-2", "missionB-1", "missionC-1"}),
            maintenance=((maintenance if i == 1 else ()) + tuple(extra_maint.get(f"AC7{i}", []))),
        )
        for i in range(1, aircraft_count + 1)
    }

    missions = {
        "missionA-1": MissionInfo(
            mission_id="missionA-1",
            name="本场起落",
            mission_class="A",
            duration_minutes=30,
            cycle_weeks=12,
            freq_days=3,
            weekly_required=True,
            dual_required=False,
            airspace_id="SAA",
            aircraft_types=frozenset({TEST_TYPE}),
            prereqs=(),
        ),
        "missionA-2": MissionInfo(
            mission_id="missionA-2",
            name="本场起落 2",
            mission_class="A",
            duration_minutes=27,
            cycle_weeks=12,
            freq_days=3,
            weekly_required=True,
            dual_required=False,
            airspace_id="SAB",
            aircraft_types=frozenset({TEST_TYPE}),
            prereqs=(),
        ),
        "missionB-1": MissionInfo(
            mission_id="missionB-1",
            name="导航",
            mission_class="B",
            duration_minutes=40,
            cycle_weeks=16,
            freq_days=7,
            weekly_required=False,
            dual_required=True,
            airspace_id="RT2",
            aircraft_types=frozenset({TEST_TYPE}),
            prereqs=(("missionA-1", "mission"),),
        ),
        "missionC-1": MissionInfo(
            mission_id="missionC-1",
            name="仪表",
            mission_class="C",
            duration_minutes=35,
            cycle_weeks=16,
            freq_days=7,
            weekly_required=False,
            dual_required=True,
            airspace_id="IFR",
            aircraft_types=frozenset({TEST_TYPE}),
            prereqs=(("A类", "class"),),
        ),
    }

    airspaces = {
        "SAA": AirspaceInfo(airspace_id="SAA", name="小区 A", capacity=airspace_capacity),
        "SAB": AirspaceInfo(airspace_id="SAB", name="小区 B", capacity=airspace_capacity),
        "RT2": AirspaceInfo(airspace_id="RT2", name="航线 2", capacity=airspace_capacity),
        "IFR": AirspaceInfo(airspace_id="IFR", name="仪表航线", capacity=airspace_capacity),
    }
    runways = {
        "RWY-1": RunwayInfo(
            runway_id="RWY-1", name="跑道 1", aircraft_types=frozenset({TEST_TYPE})
        ),
        "RWY-2": RunwayInfo(
            runway_id="RWY-2", name="跑道 2", aircraft_types=frozenset({TEST_TYPE})
        ),
    }

    progress: dict[tuple[str, str], ProgressInfo] = {}
    for person_id, person in persons.items():
        for mission_id in missions:
            done = mission_id in person.completed
            row = ProgressInfo(
                person_id=person_id,
                mission_id=mission_id,
                cycle_start=week_start,
                status="COMPLETED" if done else "NOT_STARTED",
                completed_count=1 if done else 0,
                last_done_date=None,
                cycle_weeks=missions[mission_id].cycle_weeks,
                debt_count=0,
                prereq_met=True,
                blocked_reason=None,
                is_recurrent=False,
                recurrent_since=None,
            )
            patch = (progress_overrides or {}).get((person_id, mission_id))
            if patch:
                row = ProgressInfo(**{**row.__dict__, **patch})  # type: ignore[arg-type]
            progress[person_id, mission_id] = row

    return ProblemData(
        snapshot_id="snap_test0000",
        week_start=week_start,
        window_start=overrides.window_start or window[0],
        window_end=overrides.window_end or window[1],
        persons=persons,
        aircraft=aircraft,
        missions=missions,
        airspaces=airspaces,
        runways=runways,
        progress=progress,
        overrides=overrides,
    )


def make_spec(
    data: ProblemData,
    *,
    relaxation_tier: int = 0,
    time_limit_s: float = 10.0,
    workers: int = 4,
    seed: int = 42,
    balance: float = 1.0,
) -> ConstraintSpec:
    """按 :func:`make_problem` 的实体编出对应的 `ConstraintSpec`。"""
    sem = get_semantics()
    rules = get_ruleset()
    return ConstraintSpec(
        snapshot_id=data.snapshot_id,
        ruleset_version=rules.version,
        semantics_version=sem.version,
        semantics_switches=sem.snapshot(),
        iso_week=data.iso_week,
        week_start=data.week_start,
        week_end=data.week_end,
        scope_persons="ALL",
        scope_missions="ALL",
        relaxation_tier=relaxation_tier,
        objective_weights=ObjectiveWeights(progress=1.0, disruption=1.0, balance=balance),
        incremental_constraints=[],
        runway_model="dual_runway" if sem.s05_dual_runway else "single_runway",
        runways={
            rid: sorted(r.aircraft_types)
            for rid, r in sorted(data.runways.items())
            if rid not in data.overrides.closed_runways
        },
        density_scope=dict(sem.s05_density_scope),
        airspace_capacity={aid: data.capacity_of(aid) for aid in sorted(data.airspaces)},
        freq_days={mid: m.freq_days for mid, m in sorted(data.missions.items())},
        req_max={mid: req_max_for(m.freq_days) for mid, m in sorted(data.missions.items())},
        solver_seed=seed,
        solver_workers=workers,
        solver_time_limit_s=time_limit_s,
    )


def make_bundle(**kwargs: object) -> SpecBundle:
    """一步造出 `SpecBundle`（`make_problem` 的参数原样透传）。"""
    spec_kwargs = {
        k: kwargs.pop(k)
        for k in ("relaxation_tier", "time_limit_s", "workers", "seed", "balance")
        if k in kwargs
    }
    data = make_problem(**kwargs)  # type: ignore[arg-type]
    return SpecBundle(
        spec=make_spec(data, **spec_kwargs),  # type: ignore[arg-type]
        data=data,
        ruleset=get_ruleset(),
        semantics=get_semantics(),
    )


__all__ = [
    "TEST_TYPE",
    "TEST_WEEK_START",
    "all_day",
    "make_bundle",
    "make_problem",
    "make_spec",
]
