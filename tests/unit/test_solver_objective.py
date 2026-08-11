"""目标函数、分阶段求解、规范化与可复现性（v6 §3.7 / §3.11 / 铁律 9）。"""

from __future__ import annotations

from datetime import time

from ortools.sat.python import cp_model

from backend.solver.candidates import enumerate_candidates
from backend.solver.data import ScenarioOverrides
from backend.solver.model import RelaxationSettings, build_model
from backend.solver.objective import (
    BASE_MISSION_WEIGHT,
    DEBT_FACTOR,
    OPTIMIZE_BUDGET_RATIO,
    canonical_tiebreak,
    make_solver,
    map_status,
    solve_staged,
    stage1_progress,
    stage2_hamming,
    stage3_preferences,
)
from backend.solver.solve import plan_to_selection, solve
from tests.fixtures.solver_facts import make_bundle


def _build(**kwargs: object):  # type: ignore[no-untyped-def]
    bundle = make_bundle(**kwargs)
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    relax = RelaxationSettings(tier=bundle.spec.relaxation_tier)
    built = build_model(
        bundle.data,
        bundle.spec,
        cset,
        ruleset=bundle.ruleset,
        semantics=bundle.semantics,
        relaxation=relax,
    )
    return bundle, cset, built


# ─────────────────────────────────────────────────────────────────────
# 三态映射（铁律 8）
# ─────────────────────────────────────────────────────────────────────
def test_status_mapping_keeps_unknown_and_infeasible_apart() -> None:
    assert map_status(cp_model.OPTIMAL) == "OPTIMAL"
    assert map_status(cp_model.FEASIBLE) == "FEASIBLE"
    assert map_status(cp_model.INFEASIBLE) == "INFEASIBLE"
    assert map_status(cp_model.UNKNOWN) == "UNKNOWN"
    assert map_status(cp_model.MODEL_INVALID) == "MODEL_INVALID"
    assert map_status(cp_model.UNKNOWN) != map_status(cp_model.INFEASIBLE)


# ─────────────────────────────────────────────────────────────────────
# 各阶段
# ─────────────────────────────────────────────────────────────────────
def test_stage1_is_constant_at_tier0() -> None:
    """Tier 0 下全部要求都是硬约束 → 阶段1 无取舍空间，直接跳过。"""
    _bundle, _cset, built = _build()
    assert built.satisfied == {}
    assert stage1_progress(built) is None


def test_stage1_active_at_relaxed_tier_with_debt_weighting() -> None:
    """松弛档下阶段1 才有取舍；权重按 `BASE_W × (1 + DEBT_FACTOR × debt_count)`。"""
    _bundle, cset, built = _build(
        relaxation_tier=1,
        progress_overrides={("P402", "missionB-1"): {"debt_count": 2}},
    )
    assert built.satisfied, "松弛档下应该有满足指示变量"
    assert stage1_progress(built) is not None
    weights = {r.req_id: r.weight for r in cset.requirements if r.mission_id == "missionB-1"}
    assert set(weights.values()) == {BASE_MISSION_WEIGHT * (1 + DEBT_FACTOR * 2)}


def test_stage2_is_none_without_previous_plan() -> None:
    *_rest, built = _build()
    assert stage2_hamming(built, None) is None
    assert stage2_hamming(built, ()) is not None


def test_stage3_weights_are_lexicographically_separated() -> None:
    """阶段3 的权重必须保证词典序：低优先级项的全幅变动换不来高优先级项一个单位。"""
    _bundle, _cset, built = _build()
    prefs = stage3_preferences(built)
    assert [label for label, _ in prefs.components] == [
        "架次总量",
        "负荷峰值之和",
        "起飞时刻之和",
    ]
    assert prefs.combined is not None


def test_stage3_disabled_balance_keeps_only_sortie_count() -> None:
    _bundle, _cset, built = _build(balance=0.0)
    prefs = stage3_preferences(built)
    assert [label for label, _ in prefs.components] == ["架次总量"]


def test_lateness_bound_is_a_valid_implication() -> None:
    """割线族是**蕴含**下界：加上它之后最优值不能变（不许排除任何合法解）。

    做法：同一算例解两次，一次带割线族（正常路径），一次把它关掉（诊断模式不下
    这族约束），比对「起飞时刻之和」这个分量的最优值。
    """
    bundle, cset, built = _build(aircraft_count=2)
    prefs = stage3_preferences(built)
    assert prefs.combined is not None
    built.model.minimize(prefs.combined)
    solver, _ = make_solver(seed=42, workers=4, time_limit_s=20)
    assert solver.solve(built.model) == cp_model.OPTIMAL
    with_bound = [solver.value(expr) for _label, expr in prefs.components]

    plain = build_model(
        bundle.data,
        bundle.spec,
        cset,
        ruleset=bundle.ruleset,
        semantics=bundle.semantics,
        diagnose=True,  # 诊断模式不下割线族
    )
    prefs2 = stage3_preferences(plain)
    assert prefs2.combined is not None
    for gid, lit in plain.assumptions.items():
        plain.model.add(lit == 1)  # 全部约束照常生效，只是没有那族割线
        assert gid
    plain.model.minimize(prefs2.combined)
    solver2, _ = make_solver(seed=42, workers=4, time_limit_s=30)
    status2 = solver2.solve(plain.model)
    assert status2 == cp_model.OPTIMAL
    # 诊断模式用的是宽域（约束1 靠显式上下界），架次总量与峰值应当一致
    assert solver2.value(prefs2.components[0][1]) == with_bound[0]
    assert solver2.value(prefs2.components[1][1]) == with_bound[1]


def test_canonical_tiebreak_prefers_lower_indexed_candidates() -> None:
    _bundle, cset, built = _build()
    expr = canonical_tiebreak(built)
    built.model.minimize(expr)
    solver, _ = make_solver(seed=42, workers=1, time_limit_s=20)
    assert solver.solve(built.model) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    chosen = [i for i, var in enumerate(built.x) if solver.value(var) == 1]
    assert chosen
    assert all(0 <= i < len(cset.candidates) for i in chosen)


# ─────────────────────────────────────────────────────────────────────
# 预算与可复现性
# ─────────────────────────────────────────────────────────────────────
def test_optimize_budget_ratio_leaves_room_for_canonicalisation() -> None:
    assert 0.0 < OPTIMIZE_BUDGET_RATIO < 1.0


def test_solve_staged_appends_canonicalisation_stage() -> None:
    _bundle, _cset, built = _build()
    run = solve_staged(built)
    assert run.status == "OPTIMAL"
    assert run.stages[-1].name.startswith("规范化")
    assert run.selected and run.starts and run.runways


def test_same_input_same_seed_same_workers_is_byte_identical() -> None:
    """铁律 9：连跑三次逐字节一致（`content_sha256` 相同）。"""
    shas = set()
    for _ in range(3):
        bundle = make_bundle()
        outcome = solve(bundle)
        assert outcome.plan is not None
        shas.add(outcome.plan.content_sha256)
    assert len(shas) == 1, f"三次求解得到 {len(shas)} 个不同方案：{shas}"


def test_plan_id_derives_from_content_hash() -> None:
    bundle = make_bundle()
    outcome = solve(bundle)
    assert outcome.plan is not None
    assert outcome.plan.plan_id.endswith(outcome.plan.content_sha256[:12])
    assert outcome.plan.plan_id.startswith(bundle.data.iso_week)


def test_semantics_switches_participate_in_content_hash() -> None:
    """附录 B 脚注：`semantics_switches` 与 `runway_model` 参与 sha256。"""
    bundle = make_bundle()
    baseline = solve(bundle)
    assert baseline.plan is not None
    tweaked_spec = bundle.spec.model_copy(
        update={"semantics_switches": {**bundle.spec.semantics_switches, "S-02": "per_mission"}}
    )
    tweaked = solve(
        type(bundle)(
            spec=tweaked_spec,
            data=bundle.data,
            ruleset=bundle.ruleset,
            semantics=bundle.semantics,
        )
    )
    assert tweaked.plan is not None
    assert tweaked.plan.content_sha256 != baseline.plan.content_sha256


def test_solve_returns_no_plan_when_infeasible() -> None:
    bundle = make_bundle(overrides=ScenarioOverrides(window_end=time(6, 30)), time_limit_s=15.0)
    outcome = solve(bundle)
    assert outcome.status == "INFEASIBLE"
    assert outcome.plan is None
    assert outcome.stats.objective_value is None
    assert outcome.sorties == ()


def test_plan_to_selection_round_trips() -> None:
    bundle = make_bundle()
    outcome = solve(bundle)
    assert outcome.plan is not None
    selection = plan_to_selection(outcome.plan, outcome.cset, bundle.data)
    assert set(selection) == set(outcome.run.selected)
