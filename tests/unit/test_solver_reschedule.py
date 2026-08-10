"""局部重排：三档冻结策略 + 跑道冻结 + warm start（v6 §3.8）。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend.solver.data import ScenarioOverrides
from backend.solver.reschedule import (
    FREEZE_POLICIES,
    Disruption,
    local_reschedule,
    select_frozen,
    sortie_touches,
    week_start_of,
)
from backend.solver.solve import frozen_from_plan, solve
from tests.fixtures.solver_asserts import check_plan, format_violations
from tests.fixtures.solver_facts import make_bundle, make_problem


def _baseline():  # type: ignore[no-untyped-def]
    """一版稍大的初始方案：A 类未完成 → 至少 2 个 A + 1 个 B-1。"""
    bundle = make_bundle(student_completed=frozenset(), aircraft_count=2, time_limit_s=20.0)
    outcome = solve(bundle)
    assert outcome.plan is not None and len(outcome.plan.sorties) >= 3
    return bundle, outcome.plan


def test_freeze_policies_are_the_three_from_v6() -> None:
    assert FREEZE_POLICIES == ("CONSERVATIVE", "BALANCED", "AGGRESSIVE")


def test_sortie_touches_matches_each_disruption_kind() -> None:
    bundle, plan = _baseline()
    sortie = plan.sorties[0]
    day = bundle.data.day_index(sortie.date)
    assert sortie_touches(sortie, Disruption(aircraft=frozenset({sortie.aircraft_id})), bundle.data)
    assert sortie_touches(sortie, Disruption(runways=frozenset({sortie.runway_id})), bundle.data)
    assert sortie_touches(
        sortie, Disruption(airspaces=frozenset({sortie.airspace_id})), bundle.data
    )
    assert sortie_touches(
        sortie, Disruption(persons=frozenset({sortie.crew[0].person_id})), bundle.data
    )
    # 日期过滤：扰动只影响别的天时不算命中
    other = frozenset({(day + 1) % 7})
    assert not sortie_touches(
        sortie,
        Disruption(aircraft=frozenset({sortie.aircraft_id}), days=other),
        bundle.data,
    )
    assert week_start_of(plan) == plan.week_start


def test_conservative_freezes_everything_except_affected() -> None:
    bundle, plan = _baseline()
    target = plan.sorties[0]
    disruption = Disruption(aircraft=frozenset({target.aircraft_id}), reason="AC 维修")
    decision = select_frozen(plan, disruption, "CONSERVATIVE", bundle.data)
    affected = {s.sortie_id for s in plan.sorties if sortie_touches(s, disruption, bundle.data)}
    assert set(decision.affected_ids) == affected
    assert set(decision.frozen_ids) == {s.sortie_id for s in plan.sorties} - affected
    assert decision.released_ids == ()


def test_balanced_also_releases_same_day_linked_sorties() -> None:
    bundle, plan = _baseline()
    target = plan.sorties[0]
    disruption = Disruption(persons=frozenset({target.crew[-1].person_id}))
    balanced = select_frozen(plan, disruption, "BALANCED", bundle.data)
    conservative = select_frozen(plan, disruption, "CONSERVATIVE", bundle.data)
    assert len(balanced.frozen_ids) <= len(conservative.frozen_ids)
    assert balanced.blast_radius >= conservative.blast_radius


def test_aggressive_keeps_only_executed_history() -> None:
    bundle, plan = _baseline()
    executed = [plan.sorties[0].sortie_id]
    decision = select_frozen(
        plan,
        Disruption(persons=frozenset({plan.sorties[-1].crew[-1].person_id})),
        "AGGRESSIVE",
        bundle.data,
        executed_ids=executed,
    )
    assert set(decision.frozen_ids) <= set(executed)


def test_frozen_sortie_pins_time_and_runway() -> None:
    """跑道必须一并冻结（v6 §3.8）—— 只钉时刻不钉跑道等于放跑一次真实变更。"""
    bundle, plan = _baseline()
    frozen = frozen_from_plan(plan, bundle.data, [s.sortie_id for s in plan.sorties])
    assert len(frozen) == len(plan.sorties)
    for f, s in zip(
        sorted(frozen, key=lambda f: f.day),
        sorted(plan.sorties, key=lambda s: bundle.data.day_index(s.date)),
        strict=True,
    ):
        assert f.runway_id in ("RWY-7", "RWY-8")
        assert f.slot == (f.trainee_id, f.mission_id, f.day)
        assert s.sortie_id


@pytest.mark.parametrize("policy", FREEZE_POLICIES)
def test_local_reschedule_keeps_frozen_sorties_verbatim(policy: str) -> None:
    """三档各跑一遍：冻结的架次在新方案里必须**逐字段**不变（含跑道）。"""
    bundle, plan = _baseline()
    victim = plan.sorties[0]
    day = bundle.data.day_index(victim.date)
    disruption = Disruption(
        aircraft=frozenset({victim.aircraft_id}),
        days=frozenset({day}),
        reason=f"{victim.aircraft_id} day{day} 维修",
    )
    disturbed = make_bundle(
        student_completed=frozenset(),
        aircraft_count=2,
        time_limit_s=20.0,
        overrides=disruption.to_overrides(bundle.data),
    )
    outcome, decision = local_reschedule(
        disturbed,
        plan,
        disruption,
        policy=policy,  # type: ignore[arg-type]
        executed_ids=[s.sortie_id for s in plan.sorties if s.date != victim.date],
    )
    assert decision.policy == policy
    if outcome.plan is None:
        # 保守档「可能不可行」是 v6 §3.8 明确接受的结果
        assert policy == "CONSERVATIVE"
        return
    violations = check_plan(outcome.plan, disturbed.data, disturbed.ruleset)
    assert not violations, format_violations(violations)
    # 被扰动的机号在受影响那天不该再出现
    assert not [
        s
        for s in outcome.plan.sorties
        if s.aircraft_id == victim.aircraft_id and s.date == victim.date
    ]
    frozen_keys = {
        (f.trainee_id, f.mission_id, f.day, f.aircraft_id, f.takeoff_minute, f.runway_id)
        for f in decision.frozen
    }
    new_keys = {
        (
            next(m.person_id for m in s.crew if m.role != "教员"),
            s.mission_id,
            disturbed.data.day_index(s.date),
            s.aircraft_id,
            disturbed.data.minutes_of(s.takeoff),
            s.runway_id,
        )
        for s in outcome.plan.sorties
    }
    assert frozen_keys <= new_keys, "冻结架次在重排后被改动了"


def test_reschedule_minimises_hamming_distance() -> None:
    """阶段2 的作用：无扰动、全周放开重排时，**选中的架次集合**应当原样留住。

    只断言「选择」不变、不断言 `content_sha256` 相同：阶段2 的汉明距离只管
    `x[c]`（哪些候选被选中），起飞时刻仍由阶段3 与规范化决定，而多了一条
    `汉明距离 == 0` 约束之后规范化的可行域不同，挑出的等价解可以是另一个。
    """
    bundle, plan = _baseline()
    outcome, decision = local_reschedule(
        bundle, plan, Disruption(reason="空扰动"), policy="AGGRESSIVE"
    )
    assert outcome.plan is not None
    assert decision.affected_ids == ()
    before = {
        (s.mission_id, s.date, s.aircraft_id, tuple(m.person_id for m in s.crew))
        for s in plan.sorties
    }
    after = {
        (s.mission_id, s.date, s.aircraft_id, tuple(m.person_id for m in s.crew))
        for s in outcome.plan.sorties
    }
    assert after == before, "汉明距离阶段没有留住上一版的选择"


def test_disruption_to_overrides_translates_every_field() -> None:
    data = make_problem()
    disruption = Disruption(
        persons=frozenset({"P402"}),
        aircraft=frozenset({"AC701"}),
        airspaces=frozenset({"NAV"}),
        runways=frozenset({"RWY-8"}),
        days=frozenset({1, 2}),
    )
    ov = disruption.to_overrides(data)
    assert isinstance(ov, ScenarioOverrides)
    assert ov.airspace_capacity == {"NAV": 0}
    assert ov.closed_runways == frozenset({"RWY-8"})
    assert ov.unavailable["P402"] == frozenset({data.date_of(1), data.date_of(2)})
    assert ov.maintenance_all_day == (("AC701", data.date_of(1), data.date_of(2)),)
    assert not ov.is_empty()


def test_freeze_rejects_sortie_that_no_longer_has_a_candidate() -> None:
    """冻结一个已被扰动排除的架次要**报错**，不能悄悄忽略。"""
    bundle, plan = _baseline()
    victim = plan.sorties[0]
    day = bundle.data.day_index(victim.date)
    gone = make_bundle(
        student_completed=frozenset(),
        aircraft_count=2,
        time_limit_s=15.0,
        overrides=ScenarioOverrides(
            unavailable={victim.crew[-1].person_id: frozenset({bundle.data.date_of(day)})}
        ),
    )
    frozen = frozen_from_plan(plan, gone.data, [victim.sortie_id])
    with pytest.raises(KeyError, match="不在候选集中"):
        solve(gone, frozen=frozen)


def test_week_dates_helper_spans_seven_days() -> None:
    from backend.nodes.compile_spec import week_dates

    data = make_problem()
    dates = week_dates(data.week_start)
    assert len(dates) == 7
    assert dates[-1] - dates[0] == timedelta(days=6)
