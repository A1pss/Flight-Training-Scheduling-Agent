"""属性测试 · 场景生成器本身必须**自洽**（v6 §12.1 对 `arbitrary_scenario()` 的要求）。

「生成的场景必须自洽（引用完整性成立）」是 CC_PROMPTS W4 的硬性要求。若生成器
造得出一个引用不完整的世界，那么 `test_solver_output_always_passes_validator` 的
每一次绿都可能是假绿 —— 求解器和校验器只是在同一个坏世界里达成了一致。

本文件因此把 :func:`tests.property.scenario.build_scenario` 的每一条构造不变量
都做成断言，另加一组**两次投影一致性**：`ProblemData`（求解器侧）与
`ValidationContext`（校验器侧）必须描述同一个世界。
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings

from backend.core.ruleset import IDENTITY_STUDENT
from tests.property.scenario import ScenarioSpec, arbitrary_scenario

pytestmark = pytest.mark.property

FAST = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ─────────────────────────────────────────────────────────────────────
# 引用完整性
# ─────────────────────────────────────────────────────────────────────
@given(scenario=arbitrary_scenario())
@FAST
def test_mission_airspaces_exist(scenario: ScenarioSpec) -> None:
    known = {s.airspace_id for s in scenario.airspaces}
    for mission in scenario.missions:
        assert mission.airspace_id in known, mission.mission_id


@given(scenario=arbitrary_scenario())
@FAST
def test_mission_aircraft_types_are_present_in_the_fleet(scenario: ScenarioSpec) -> None:
    fleet = set(scenario.aircraft_types)
    for mission in scenario.missions:
        assert set(mission.aircraft_types) & fleet, mission.mission_id


@given(scenario=arbitrary_scenario())
@FAST
def test_aircraft_capable_missions_exist(scenario: ScenarioSpec) -> None:
    known = {m.mission_id for m in scenario.missions}
    for aircraft in scenario.aircraft:
        assert set(aircraft.capable_missions) <= known, aircraft.aircraft_id


@given(scenario=arbitrary_scenario())
@FAST
def test_every_aircraft_type_has_a_runway(scenario: ScenarioSpec) -> None:
    served = {t for r in scenario.runways for t in r.aircraft_types}
    assert set(scenario.aircraft_types) <= served


@given(scenario=arbitrary_scenario())
@FAST
def test_runway_types_are_present_in_the_fleet(scenario: ScenarioSpec) -> None:
    fleet = set(scenario.aircraft_types)
    for runway in scenario.runways:
        assert set(runway.aircraft_types) <= fleet, runway.runway_id


@given(scenario=arbitrary_scenario())
@FAST
def test_person_qualifications_cover_only_existing_classes(scenario: ScenarioSpec) -> None:
    classes = {m.mission_class for m in scenario.missions}
    for person in scenario.persons:
        assert set(person.levels) <= classes, person.person_id


@given(scenario=arbitrary_scenario())
@FAST
def test_person_aircraft_types_are_present_in_the_fleet(scenario: ScenarioSpec) -> None:
    fleet = set(scenario.aircraft_types)
    for person in scenario.persons:
        assert set(person.aircraft_types) <= fleet, person.person_id


@given(scenario=arbitrary_scenario())
@FAST
def test_completed_missions_exist(scenario: ScenarioSpec) -> None:
    known = {m.mission_id for m in scenario.missions}
    for person in scenario.persons:
        assert set(person.completed) <= known, person.person_id


@given(scenario=arbitrary_scenario())
@FAST
def test_prereqs_reference_existing_missions_or_classes(scenario: ScenarioSpec) -> None:
    ids = {m.mission_id for m in scenario.missions}
    classes = {m.mission_class for m in scenario.missions}
    for mission in scenario.missions:
        for ref, kind in mission.prereqs:
            if kind == "class":
                assert ref.removesuffix("类") in classes, (mission.mission_id, ref)
            else:
                assert ref in ids, (mission.mission_id, ref)


@given(scenario=arbitrary_scenario())
@FAST
def test_prereq_graph_is_acyclic(scenario: ScenarioSpec) -> None:
    """先修只指向下标更小的课目 → 天然无环。这里直接验拓扑序。"""
    order = {m.mission_id: i for i, m in enumerate(scenario.missions)}
    by_class: dict[str, list[str]] = {}
    for mission in scenario.missions:
        by_class.setdefault(mission.mission_class, []).append(mission.mission_id)
    for mission in scenario.missions:
        for ref, kind in mission.prereqs:
            targets = by_class.get(ref.removesuffix("类"), []) if kind == "class" else [ref]
            for target in targets:
                assert order[target] < order[mission.mission_id], (mission.mission_id, target)


@given(scenario=arbitrary_scenario())
@FAST
def test_unavailable_and_maintenance_days_fall_inside_the_week(scenario: ScenarioSpec) -> None:
    week = set(scenario.week_dates)
    for person in scenario.persons:
        assert set(person.unavailable) <= week, person.person_id
    for aircraft in scenario.aircraft:
        assert set(aircraft.maintenance_days) <= week, aircraft.aircraft_id


@given(scenario=arbitrary_scenario())
@FAST
def test_at_least_one_aircraft_stays_serviceable(scenario: ScenarioSpec) -> None:
    """生成器不许造出「全机队整周维护」—— 那是 I2，属于构造不可行族。"""
    assert any(len(a.maintenance_days) < 7 for a in scenario.aircraft)


@given(scenario=arbitrary_scenario())
@FAST
def test_progress_rows_exist_exactly_for_class_qualified_pairs(scenario: ScenarioSpec) -> None:
    """`training_progress` 只为「持有该课目类别资质」的组合建行 —— 与 M1 落库一致。"""
    expected = {
        (p.person_id, m.mission_id)
        for p in scenario.persons
        for m in scenario.missions
        if m.mission_class in p.levels
    }
    assert set(scenario.progress_keys()) == expected


@given(scenario=arbitrary_scenario())
@FAST
def test_week_start_is_monday(scenario: ScenarioSpec) -> None:
    assert scenario.week_start.weekday() == 0


# ─────────────────────────────────────────────────────────────────────
# 两次投影必须描述同一个世界
# ─────────────────────────────────────────────────────────────────────
@given(scenario=arbitrary_scenario())
@FAST
def test_both_projections_agree_on_entity_ids(scenario: ScenarioSpec) -> None:
    data = scenario.to_problem_data()
    ctx = scenario.to_validation_context()
    assert set(data.persons) == set(ctx.persons)
    assert set(data.aircraft) == set(ctx.aircraft)
    assert set(data.missions) == set(ctx.missions)
    assert set(data.airspaces) == set(ctx.airspaces)
    # 跑道两侧的表达方式不同：校验侧**直接删掉**关闭的跑道（关闭 = 不在册），
    # 求解侧保留在 `runways` 里、由 `allowed_runways()` 按 overrides 过滤。
    # 所以这里比的是「开着的那些」。
    assert set(ctx.runways) == {r.runway_id for r in scenario.open_runways()}
    assert set(ctx.runways) <= set(data.runways)


@given(scenario=arbitrary_scenario())
@FAST
def test_both_projections_agree_on_progress(scenario: ScenarioSpec) -> None:
    data = scenario.to_problem_data()
    ctx = scenario.to_validation_context()
    assert set(data.progress) == set(ctx.progress)
    for key, row in data.progress.items():
        mirror = ctx.progress[key]
        assert row.status == mirror.status
        assert row.prereq_met == mirror.prereq_met
        assert row.blocked_reason == mirror.blocked_reason
        assert row.is_recurrent == mirror.is_recurrent
        assert row.recurrent_since == mirror.recurrent_since
        assert row.last_done_date == mirror.last_done_date


@given(scenario=arbitrary_scenario())
@FAST
def test_both_projections_agree_on_disruptions(scenario: ScenarioSpec) -> None:
    """请假 / 维修 / 到期日：两侧看到的必须一模一样，否则属性测试会出假绿。"""
    data = scenario.to_problem_data()
    ctx = scenario.to_validation_context()
    for pid, person in data.persons.items():
        assert person.unavailable == ctx.persons[pid].unavailable_dates
        for cls, qual in person.qualifications.items():
            assert qual.expiry == ctx.persons[pid].qualifications[cls].expiry_date
    for aid, aircraft in data.aircraft.items():
        assert len(aircraft.maintenance) == len(ctx.aircraft[aid].maintenance)


@given(scenario=arbitrary_scenario())
@FAST
def test_airspace_capacity_override_reaches_both_sides(scenario: ScenarioSpec) -> None:
    data = scenario.to_problem_data()
    ctx = scenario.to_validation_context()
    for airspace_id in ctx.airspaces:
        assert data.capacity_of(airspace_id) == ctx.airspaces[airspace_id].capacity


@given(scenario=arbitrary_scenario())
@FAST
def test_closed_runways_are_invisible_to_both_sides(scenario: ScenarioSpec) -> None:
    ctx = scenario.to_validation_context()
    data = scenario.to_problem_data()
    for runway_id in scenario.closed_runways:
        assert runway_id not in ctx.runways
        for aircraft_type in scenario.aircraft_types:
            assert runway_id not in data.allowed_runways(aircraft_type)


@given(scenario=arbitrary_scenario())
@FAST
def test_students_only_hold_the_first_aircraft_type(scenario: ScenarioSpec) -> None:
    """学员只持一种机型 —— 复刻 v6 §1.4.1 的「双重排除」形状，让 BLOCKED 有戏演。"""
    for person in scenario.persons:
        if person.identity == IDENTITY_STUDENT:
            assert len(person.aircraft_types) == 1, person.person_id


@given(scenario=arbitrary_scenario())
@FAST
def test_scenario_is_deterministic(scenario: ScenarioSpec) -> None:
    """同一个 `ScenarioSpec` 投影两次必须完全相同（铁律 9 的前置条件）。"""
    first = scenario.to_problem_data()
    second = scenario.to_problem_data()
    assert first.persons.keys() == second.persons.keys()
    assert first.progress == second.progress
    assert scenario.to_spec(first).model_dump() == scenario.to_spec(second).model_dump()
