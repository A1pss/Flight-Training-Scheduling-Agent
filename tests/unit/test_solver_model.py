"""14 条规则的 CP-SAT 编码是否**真的**生效（v6 §3.2 逐条）。

每个用例的形状都一样：把合成算例调成「只有违反某条约束才排得下」的样子，
求解，然后断言求解器宁可不排也不违反。
"""

from __future__ import annotations

from datetime import time, timedelta
from itertools import pairwise

import pytest

from backend.core.ruleset import get_ruleset, get_semantics
from backend.solver.candidates import enumerate_candidates
from backend.solver.data import ScenarioOverrides
from backend.solver.model import (
    DIAGNOSE_HORIZON,
    ConstraintGroup,
    RelaxationSettings,
    build_model,
    requirement_scope,
)
from backend.solver.solve import model_size, solve
from tests.fixtures.solver_asserts import check_plan, format_violations
from tests.fixtures.solver_facts import TEST_WEEK_START, all_day, make_bundle


def _solve(**kwargs: object):  # type: ignore[no-untyped-def]
    bundle = make_bundle(**kwargs)
    return bundle, solve(bundle)


def _assert_compliant(bundle, outcome) -> None:  # type: ignore[no-untyped-def]
    assert outcome.plan is not None
    violations = check_plan(outcome.plan, bundle.data, bundle.ruleset)
    assert not violations, format_violations(violations)


# ─────────────────────────────────────────────────────────────────────
# 基本形态
# ─────────────────────────────────────────────────────────────────────
def test_mini_instance_solves_to_optimal_and_is_compliant() -> None:
    bundle, outcome = _solve()
    assert outcome.status == "OPTIMAL"
    _assert_compliant(bundle, outcome)
    # 要求：A-2 的 3 天滑窗 ≥2 次（同时满足约束3）+ B-1 每周 ≥1 次 → 恰好 3 个架次
    assert len(outcome.plan.sorties) == 3  # type: ignore[union-attr]
    assert {s.mission_id for s in outcome.plan.sorties} == {  # type: ignore[union-attr]
        "missionA-2",
        "missionB-1",
    }


def test_stats_carry_reproducibility_inputs() -> None:
    """§3.11：`random_seed` 与 `num_workers` 必须进 `SolverStats`。"""
    _bundle, outcome = _solve(seed=42, workers=4)
    assert outcome.stats.random_seed == 42
    assert outcome.stats.num_workers == 4
    assert outcome.stats.num_candidates == len(outcome.cset.candidates)
    num_vars, num_cts = model_size(outcome.built)
    assert (outcome.stats.num_variables, outcome.stats.num_constraints) == (num_vars, num_cts)


def test_blocked_items_do_not_make_it_infeasible() -> None:
    """v6 §3.6：BLOCKED 不影响求解状态，方案照出，但必须 100% 披露。"""
    _bundle, outcome = _solve()
    assert outcome.status in ("OPTIMAL", "FEASIBLE")
    assert [b.mission_id for b in outcome.blocked_items] == ["missionC-1"]
    assert outcome.plan is not None
    assert outcome.plan.blocked_items == list(outcome.blocked_items)


def test_tier0_has_no_debts() -> None:
    """Tier 0 下所有要求都是硬约束 → 欠账必然为 0。"""
    _bundle, outcome = _solve()
    assert outcome.debts == ()


# ─────────────────────────────────────────────────────────────────────
# 约束1 时间一致性
# ─────────────────────────────────────────────────────────────────────
def test_c01_window_bounds_are_structural() -> None:
    """常规模式下域即 `[lo, hi]`，结构性成立（v6 §3.1.3）。"""
    bundle = make_bundle()
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    built = build_model(
        bundle.data, bundle.spec, cset, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    horizon = bundle.data.horizon_minutes
    for slot in built.slot_start:
        dur = bundle.data.missions[slot[1]].duration_minutes
        lo, hi = built.slot_bounds_of(slot)
        assert lo >= 0 and hi <= horizon - dur


def test_c01_compressed_window_forbids_long_missions() -> None:
    """训练窗压到 30 分钟 → 40 分钟的 B-1 排不下，判 INFEASIBLE（不是硬塞进去）。"""
    _bundle, outcome = _solve(
        overrides=ScenarioOverrides(window_end=time(6, 30)), time_limit_s=20.0
    )
    assert outcome.status == "INFEASIBLE"


def test_diagnose_mode_uses_wide_domain() -> None:
    bundle = make_bundle()
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    built = build_model(
        bundle.data,
        bundle.spec,
        cset,
        ruleset=bundle.ruleset,
        semantics=bundle.semantics,
        diagnose=True,
    )
    slot = next(iter(built.slot_start))
    lo, hi = built.slot_bounds_of(slot)
    assert (lo, hi) == (0, DIAGNOSE_HORIZON - bundle.data.missions[slot[1]].duration_minutes)
    assert "C01_window" in built.assumptions


# ─────────────────────────────────────────────────────────────────────
# 约束3 / 13：要求集
# ─────────────────────────────────────────────────────────────────────
def test_c03_weekly_class_is_enforced() -> None:
    """约束3：把 A 类**两个**空域都关掉 → A 类每周必飞无法满足 → INFEASIBLE。

    只关一个不行 —— S-02 是「A 类整体 ≥1 次」，另一门 A 课目还能顶上。
    这个用例顺带把 S-02 的类级语义钉死了。
    """
    _bundle, half = _solve(
        overrides=ScenarioOverrides(airspace_capacity={"LAC": 0}), time_limit_s=20.0
    )
    assert half.status == "OPTIMAL", "只关一个 A 类空域不该不可行（S-02 类级计数）"
    _bundle2, outcome = _solve(
        overrides=ScenarioOverrides(airspace_capacity={"LAC": 0, "LAD": 0}),
        time_limit_s=20.0,
    )
    assert outcome.status == "INFEASIBLE"


def test_c13_frequency_windows_force_multiple_sorties() -> None:
    """A 类 freq_days=3 未完成时，5 个滑窗要求至少 2 个架次（不能都压在一天）。"""
    bundle, outcome = _solve(student_completed=frozenset())
    assert outcome.status == "OPTIMAL"
    days = sorted(
        bundle.data.day_index(s.date)
        for s in outcome.plan.sorties  # type: ignore[union-attr]
        if s.mission_id == "missionA-2"
    )
    assert len(days) >= 2
    for start in range(0, 5):
        assert any(start <= d < start + 3 for d in days), f"窗口 {start} 未被覆盖"


def test_requirement_scope_matches_candidates() -> None:
    bundle = make_bundle()
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    classes = {mid: m.mission_class for mid, m in bundle.data.missions.items()}
    for req in cset.requirements:
        scope = requirement_scope(cset, req, classes)
        assert scope, f"{req.req_id} 的候选范围为空"
        for i in scope:
            assert cset.candidates[i].trainee_id == req.person_id


# ─────────────────────────────────────────────────────────────────────
# 约束6 空域容量
# ─────────────────────────────────────────────────────────────────────
def test_c06_airspace_capacity_serialises_concurrent_sorties() -> None:
    """容量 1 的空域里两个架次不得并发。把两门课目挤到同一天验证。"""
    bundle, outcome = _solve(student_completed=frozenset(), aircraft_count=2)
    assert outcome.plan is not None
    _assert_compliant(bundle, outcome)


# ─────────────────────────────────────────────────────────────────────
# 约束7 周转与维护
# ─────────────────────────────────────────────────────────────────────
def test_c07_turnaround_enforced_when_single_aircraft() -> None:
    """单机 + 大周转：同机两个架次之间必须留够 `landing → takeoff` 的间隔（S-06）。"""
    bundle, outcome = _solve(student_completed=frozenset(), turnaround=120, aircraft_count=1)
    assert outcome.plan is not None
    _assert_compliant(bundle, outcome)


def test_c07_maintenance_window_is_respected() -> None:
    when = TEST_WEEK_START + timedelta(days=2)
    bundle, outcome = _solve(maintenance=(all_day(when),))
    assert outcome.plan is not None
    assert all(s.date != when for s in outcome.plan.sorties)
    _assert_compliant(bundle, outcome)


# ─────────────────────────────────────────────────────────────────────
# 约束8 间隔与休息
# ─────────────────────────────────────────────────────────────────────
def test_c08_gap_and_rest_hold_when_everything_crams_into_one_day() -> None:
    """把训练窗压到刚够 3 个架次，逼出「同日 3 架次」的形态，验 ≥10 与 ≥30。"""
    bundle, outcome = _solve(
        student_completed=frozenset(),
        aircraft_count=3,
        airspace_capacity=2,
        overrides=ScenarioOverrides(window_end=time(11, 0)),
        time_limit_s=25.0,
    )
    assert outcome.plan is not None
    _assert_compliant(bundle, outcome)


# ─────────────────────────────────────────────────────────────────────
# 约束9 起降密度与跑道
# ─────────────────────────────────────────────────────────────────────
def test_c09_runway_is_a_decision_variable_and_respects_type_mapping() -> None:
    bundle, outcome = _solve()
    assert outcome.plan is not None
    for s in outcome.plan.sorties:
        allowed = bundle.data.allowed_runways(bundle.data.aircraft[s.aircraft_id].aircraft_type)
        assert s.runway_id in allowed


def test_c09_closed_runway_never_used() -> None:
    bundle, outcome = _solve(overrides=ScenarioOverrides(closed_runways=frozenset({"RWY-8"})))
    assert outcome.plan is not None
    assert {s.runway_id for s in outcome.plan.sorties} == {"RWY-7"}
    _assert_compliant(bundle, outcome)


def test_c09_density_holds_under_pressure() -> None:
    """一天里塞 4 个架次，验 20 分钟/跑道 ≤2 与全场 ≥7 分钟同时成立。"""
    bundle, outcome = _solve(
        student_completed=frozenset(),
        aircraft_count=4,
        airspace_capacity=2,
        overrides=ScenarioOverrides(window_end=time(8, 0)),
        time_limit_s=25.0,
    )
    if outcome.plan is not None:
        _assert_compliant(bundle, outcome)


def test_c09_separation_is_airport_wide_not_per_runway() -> None:
    """D-2 的反模式护栏：7 分钟间隔是**全场**的，跨跑道也算。

    构造：一天必须起飞 2 架次、两条跑道都可用。若把 7 分钟实现成按跑道分组，
    求解器完全可以让两个架次同一时刻从两条跑道起飞。断言它没有。
    """
    bundle, outcome = _solve(aircraft_count=2)
    assert outcome.plan is not None
    by_day: dict[object, list[int]] = {}
    for s in outcome.plan.sorties:
        by_day.setdefault(s.date, []).append(s.takeoff.hour * 60 + s.takeoff.minute)
    for times in by_day.values():
        for a, b in pairwise(sorted(times)):
            assert b - a >= bundle.ruleset.separation_minutes


# ─────────────────────────────────────────────────────────────────────
# 约束10 / 11 / 12 / 14
# ─────────────────────────────────────────────────────────────────────
def test_c12_person_daily_cap_blocks_fourth_sortie() -> None:
    """单人单日 ≤3：让 A 类需要 4 次却只有 1 天可飞 → INFEASIBLE。"""
    days = frozenset(TEST_WEEK_START + timedelta(days=i) for i in range(1, 7))
    _bundle, outcome = _solve(
        student_completed=frozenset(),
        student_unavailable=days,
        aircraft_count=4,
        time_limit_s=20.0,
    )
    # 一天内 A 类最多排 req_max=3 次，5 个滑窗需要覆盖 day0..day6 → 不可行
    assert outcome.status == "INFEASIBLE"


def test_c14_req_max_caps_repeats() -> None:
    bundle, outcome = _solve(student_completed=frozenset(), aircraft_count=2)
    assert outcome.plan is not None
    counts: dict[str, int] = {}
    for s in outcome.plan.sorties:
        counts[s.mission_id] = counts.get(s.mission_id, 0) + 1
    for mission_id, count in counts.items():
        assert count <= bundle.spec.req_max[mission_id]


# ─────────────────────────────────────────────────────────────────────
# 约束组目录
# ─────────────────────────────────────────────────────────────────────
def test_group_catalog_covers_all_gateable_rules() -> None:
    bundle = make_bundle()
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    built = build_model(
        bundle.data,
        bundle.spec,
        cset,
        ruleset=bundle.ruleset,
        semantics=bundle.semantics,
        diagnose=True,
    )
    rules_covered = {rid for g in built.groups.values() for rid in g.rule_ids}
    # 约束2/5 是纯预筛，没有可开关的模型约束；其余 12 条都有组
    assert rules_covered == {1, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14}
    for gid, group in built.groups.items():
        assert isinstance(group, ConstraintGroup)
        assert gid in built.assumptions


def test_r0_groups_are_never_relaxable() -> None:
    bundle = make_bundle()
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    built = build_model(
        bundle.data, bundle.spec, cset, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    for group in built.groups.values():
        if group.tier == "R0":
            assert group.relaxable is False


def test_relaxation_settings_soft_rules_follow_ladder() -> None:
    rules = get_ruleset()
    assert RelaxationSettings(tier=0).soft_rules(rules) == frozenset()
    assert RelaxationSettings(tier=1).soft_rules(rules) == frozenset({13})
    assert RelaxationSettings(tier=2).soft_rules(rules) == frozenset({3, 13})
    assert RelaxationSettings(tier=3).soft_rules(rules) == frozenset({3, 10, 11, 12, 13})
    assert RelaxationSettings(tier=3).r1_active() is False
    assert RelaxationSettings(tier=3, weekly_sorties_bonus=4).r1_active() is True
    assert RelaxationSettings(tier=2, weekly_sorties_bonus=4).r1_active() is False


def test_semantics_switch_single_runway_falls_back_cleanly() -> None:
    """S-05 切成 single_runway 时不应重复计数（每个架次只占一次窗口）。"""
    bundle = make_bundle()
    spec = bundle.spec.model_copy(
        update={
            "runway_model": "single_runway",
            "density_scope": {"window_20min": "airport_wide", "separation_7min": "per_runway"},
        }
    )
    cset = enumerate_candidates(
        bundle.data, spec, ruleset=bundle.ruleset, semantics=get_semantics()
    )
    built = build_model(bundle.data, spec, cset, ruleset=bundle.ruleset, semantics=bundle.semantics)
    assert built.slot_runway  # 跑道变量照常存在


@pytest.mark.parametrize("tier", [1, 2])
def test_relaxed_tier_reports_debts_when_requirement_unsatisfiable(tier: int) -> None:
    """松弛档下欠账必须**显式披露**（v6 §0.3），不是静默吞掉。"""
    bundle = make_bundle(
        relaxation_tier=tier,
        overrides=ScenarioOverrides(airspace_capacity={"NAV": 0}),
        time_limit_s=20.0,
    )
    outcome = solve(bundle, relaxation=RelaxationSettings(tier=tier))
    assert outcome.status in ("OPTIMAL", "FEASIBLE")
    assert outcome.plan is not None
    debts = {(d.person_id, d.mission_id): d for d in outcome.plan.debts}
    assert ("P402", "missionB-1") in debts
    debt = debts["P402", "missionB-1"]
    assert (debt.required, debt.scheduled, debt.debt) == (1, 0, 1)
    assert debt.relaxed_by == f"TIER{tier}"


# ─────────────────────────────────────────────────────────────────────
# 增量约束（多轮修订的求解器侧落点，v6 §7.3.4）
# ─────────────────────────────────────────────────────────────────────
def _with_incremental(kind: str, targets: list[str], **params: object):  # type: ignore[no-untyped-def]
    from backend.schemas.intent import IncrementalConstraint

    bundle = make_bundle(student_completed=frozenset(), aircraft_count=2, time_limit_s=20.0)
    inc = IncrementalConstraint(
        kind=kind,  # type: ignore[arg-type]
        targets=targets,
        params=params,
        origin_utterance=f"测试用：{kind}",
        round_no=1,
    )
    spec = bundle.spec.model_copy(update={"incremental_constraints": [inc]})
    return type(bundle)(
        spec=spec, data=bundle.data, ruleset=bundle.ruleset, semantics=bundle.semantics
    )


def test_incremental_forbid_removes_person() -> None:
    """FORBID：点名的人/机在本轮完全不可用。学员被 FORBID → 无解。"""
    outcome = solve(_with_incremental("FORBID", ["P402"]))
    assert outcome.status == "INFEASIBLE"


def test_incremental_pin_resource_forces_one_aircraft() -> None:
    outcome = solve(_with_incremental("PIN_RESOURCE", ["P402"], aircraft_id="AC702"))
    assert outcome.plan is not None
    assert {s.aircraft_id for s in outcome.plan.sorties} == {"AC702"}


def test_incremental_pin_runway_forces_that_runway() -> None:
    outcome = solve(_with_incremental("PIN_RUNWAY", ["P402"], runway_id="RWY-8"))
    assert outcome.plan is not None
    assert {s.runway_id for s in outcome.plan.sorties} == {"RWY-8"}


def test_incremental_pin_runway_on_incompatible_type_kills_the_candidate() -> None:
    """v6 §7.3.4 第 3 条：机型用不了那条跑道时该候选不可选（Planner 侧据此回滚）。

    合成算例里 `TX-1` 两条跑道都能用，所以先关掉一条造出「不可用」的情形。
    """
    from backend.schemas.intent import IncrementalConstraint

    bundle = make_bundle(
        overrides=ScenarioOverrides(closed_runways=frozenset({"RWY-8"})), time_limit_s=15.0
    )
    inc = IncrementalConstraint(
        kind="PIN_RUNWAY",
        targets=["P402"],
        params={"runway_id": "RWY-8"},
        origin_utterance="都走 2 号跑道",
        round_no=1,
    )
    spec = bundle.spec.model_copy(update={"incremental_constraints": [inc]})
    outcome = solve(
        type(bundle)(
            spec=spec, data=bundle.data, ruleset=bundle.ruleset, semantics=bundle.semantics
        )
    )
    assert outcome.status == "INFEASIBLE"


def test_incremental_shift_window_moves_takeoffs() -> None:
    outcome = solve(
        _with_incremental("SHIFT_WINDOW", ["P402"], earliest_minute=120, latest_minute=400)
    )
    assert outcome.plan is not None
    for sortie in outcome.plan.sorties:
        minute = sortie.takeoff.hour * 60 + sortie.takeoff.minute - 6 * 60
        assert minute >= 120


def test_incremental_pin_time_fixes_takeoff() -> None:
    outcome = solve(_with_incremental("PIN_TIME", ["P402"], takeoff_minute=90))
    assert outcome.plan is not None
    for sortie in outcome.plan.sorties:
        assert sortie.takeoff.hour * 60 + sortie.takeoff.minute - 6 * 60 == 90


def test_incremental_reduce_density_caps_daily_takeoffs() -> None:
    outcome = solve(_with_incremental("REDUCE_DENSITY", ["ALL"], max_takeoffs_per_day=1))
    assert outcome.plan is not None
    per_day: dict[object, int] = {}
    for sortie in outcome.plan.sorties:
        per_day[sortie.date] = per_day.get(sortie.date, 0) + 1
    assert max(per_day.values()) <= 1
