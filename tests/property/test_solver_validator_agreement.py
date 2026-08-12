"""属性测试 · **求解器只要出解，独立校验器必过**（v6 §12.1 第一条）。

```python
@given(scenario=arbitrary_scenario())
@settings(max_examples=500, deadline=None)
def test_solver_output_always_passes_validator(scenario):
    result = solve(scenario)
    assume(result.status in (OPTIMAL, FEASIBLE))
    assert run_all_checks(result.plan, scenario.ctx).all_passed
```

**反例 = 规格理解分歧 bug（FTS-3003，CRITICAL）。** 按 CLAUDE.md §7 第 5 条，
出现反例要停下来定位到具体条款报告业务方，**不许**改 solver 或 validator 去抹平。
本文件的断言消息因此都带上「哪条规则 + 具体架次」，好让反例一眼能定位。

同一批随机场景顺带跑第三条通道（`tests/naive_checker.py`），于是 v6 §12.3 的
**三重独立验证**在属性测试这一层就已经成立。
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, assume, given, settings

from backend.core.ruleset import IDENTITY_INSTRUCTOR, IDENTITY_STUDENT
from backend.schemas.plan import TRAINING_WINDOW_END, TRAINING_WINDOW_START
from backend.solver.solve import SolveOutcome, solve
from backend.validator import run_all_checks, verify_format
from tests.naive_checker import blocked_disclosure_gaps, naive_check_all
from tests.property.scenario import ScenarioSpec, arbitrary_scenario

pytestmark = pytest.mark.property

SOLVED = ("OPTIMAL", "FEASIBLE")

#: 核心不变量跑满 v6 §12.1 要求的 500 例
CORE = settings(
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
#: 其余属性用 120 例 —— 它们与核心那条共用同一个生成器，样本空间已被覆盖
AUX = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


def _solved(scenario: ScenarioSpec) -> SolveOutcome:
    return solve(scenario.to_bundle())


def _fail_message(scenario: ScenarioSpec, outcome: SolveOutcome, details: list[str]) -> str:
    return (
        "★ FTS-3003（CRITICAL）求解器与校验器判定分歧 —— 按 CLAUDE.md §7 第 5 条停下来报告，"
        "不许改代码抹平。\n"
        f"  场景：{len(scenario.persons)} 人 / {len(scenario.aircraft)} 机 / "
        f"{len(scenario.missions)} 课目 / 容量覆盖 {dict(scenario.airspace_capacity_override)} / "
        f"关闭跑道 {scenario.closed_runways} / 窗口 {scenario.window_start}-{scenario.window_end}\n"
        f"  状态：{outcome.status}，{len(outcome.sorties)} 架次\n"
        "  违规：\n    " + "\n    ".join(details)
    )


# ─────────────────────────────────────────────────────────────────────
# 核心不变量
# ─────────────────────────────────────────────────────────────────────
@given(scenario=arbitrary_scenario())
@CORE
def test_solver_output_always_passes_validator(scenario: ScenarioSpec) -> None:
    """**本系统的杀手锏。** 求解器只要出解，独立校验器 14 条必须全过。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    report = run_all_checks(outcome.plan, scenario.to_validation_context())
    assert report.all_passed, _fail_message(
        scenario, outcome, [f"[{v.rule_id}] {v.detail}" for v in report.all_violations()]
    )
    assert report.missing_rules() == [], "14 条没跑全，不能宣称合规"


@given(scenario=arbitrary_scenario())
@AUX
def test_solver_output_passes_naive_checker(scenario: ScenarioSpec) -> None:
    """第三方 naive checker（v6 §12.3 度量方式第 2 条）同样必须全过。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    naive = naive_check_all(outcome.plan, scenario.to_validation_context())
    assert naive.passed, _fail_message(
        scenario, outcome, [f"[{v.rule_id}] {v.detail}" for v in naive.violations]
    )


@given(scenario=arbitrary_scenario())
@AUX
def test_two_checkers_agree_on_solver_output(scenario: ScenarioSpec) -> None:
    """对拍：主校验器与 naive checker 在同一批解上判定逐条一致。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    ctx = scenario.to_validation_context()
    main = frozenset(
        v.rule_id
        for v in run_all_checks(outcome.plan, ctx).all_violations()
        if v.severity == "HARD"
    )
    naive = naive_check_all(outcome.plan, ctx).violated_rules()
    assert main == naive, f"仅主校验器 {sorted(main - naive)}；仅 naive {sorted(naive - main)}"


@given(scenario=arbitrary_scenario())
@AUX
def test_format_gate_passes_for_solver_output(scenario: ScenarioSpec) -> None:
    """闸门2（Schema + 引用完整性 + 三表交叉一致性）对求解器产物必须 100% 通过。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    report = verify_format(outcome.plan, scenario.to_validation_context())
    assert report.passed, list(report.all_errors())


# ─────────────────────────────────────────────────────────────────────
# 三态与 BLOCKED
# ─────────────────────────────────────────────────────────────────────
@given(scenario=arbitrary_scenario())
@AUX
def test_three_states_stay_separated(scenario: ScenarioSpec) -> None:
    """铁律 8：`plan is None` 当且仅当没有可行解；UNKNOWN 不得被当成 INFEASIBLE。"""
    outcome = _solved(scenario)
    assert outcome.status in ("OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN", "MODEL_INVALID")
    assert (outcome.plan is None) == (outcome.status not in SOLVED)
    if outcome.status == "INFEASIBLE":
        assert outcome.stats.objective_value is None


@given(scenario=arbitrary_scenario())
@AUX
def test_blocked_combinations_are_never_scheduled(scenario: ScenarioSpec) -> None:
    """先修未满足的 (学员, 课目) 在方案中出现次数必须为 0（v6 §12.3 BLOCKED ①）。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    blocked = {(b.person_id, b.mission_id) for b in outcome.plan.blocked_items}
    for sortie in outcome.plan.sorties:
        trainee = next(m.person_id for m in sortie.crew if m.role != "教员")
        assert (trainee, sortie.mission_id) not in blocked, (
            f"{sortie.sortie_id}: {trainee} × {sortie.mission_id} 已被判 BLOCKED 却排上了"
        )


@given(scenario=arbitrary_scenario())
@AUX
def test_blocked_items_are_fully_disclosed(scenario: ScenarioSpec) -> None:
    """披露率 100%（v6 §0.3 第四条断言）：先修未满足的组合必须全部出现在方案里。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    gaps = blocked_disclosure_gaps(outcome.plan, scenario.to_validation_context())
    assert not gaps, gaps


@given(scenario=arbitrary_scenario())
@AUX
def test_blocked_reason_wording_is_canonical(scenario: ScenarioSpec) -> None:
    """措辞统一为「`<课目编号> 未完成`」，多门用「、」连接（v6 §12.3 ②）。"""
    outcome = _solved(scenario)
    for item in outcome.blocked_items:
        parts = item.reason.split("、")
        assert parts == [f"{m} 未完成" for m in item.missing_prereqs], item.reason


@given(scenario=arbitrary_scenario())
@AUX
def test_tier_zero_never_produces_debts(scenario: ScenarioSpec) -> None:
    """Tier 0 下所有要求都是硬约束 → 欠账必然为空（v6 §0.3）。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    assert outcome.plan.relaxation_tier == 0
    assert outcome.plan.debts == []


# ─────────────────────────────────────────────────────────────────────
# 扰动真的被两侧同时看到
# ─────────────────────────────────────────────────────────────────────
@given(scenario=arbitrary_scenario())
@AUX
def test_unavailable_person_is_never_scheduled(scenario: ScenarioSpec) -> None:
    """请假（约束2）：不可用当日不得出现在任何机组里。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    off = {p.person_id: set(p.unavailable) for p in scenario.persons}
    for sortie in outcome.plan.sorties:
        for member in sortie.crew:
            assert sortie.date not in off[member.person_id], (
                f"{sortie.sortie_id}: {member.person_id} 在 {sortie.date} 不可用"
            )


@given(scenario=arbitrary_scenario())
@AUX
def test_maintained_aircraft_is_never_scheduled(scenario: ScenarioSpec) -> None:
    """维修（约束7）：全天维护的飞机当日不得出现。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    down = {a.aircraft_id: set(a.maintenance_days) for a in scenario.aircraft}
    for sortie in outcome.plan.sorties:
        assert sortie.date not in down[sortie.aircraft_id], (
            f"{sortie.sortie_id}: {sortie.aircraft_id} 在 {sortie.date} 全天维护"
        )


@given(scenario=arbitrary_scenario())
@AUX
def test_closed_runway_is_never_used(scenario: ScenarioSpec) -> None:
    """跑道关闭（v6 §12.3 单点扰动，M2-C 新增覆盖）：关闭的跑道不得出现在方案里。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    for sortie in outcome.plan.sorties:
        assert sortie.runway_id not in scenario.closed_runways, sortie.sortie_id


@given(scenario=arbitrary_scenario())
@AUX
def test_zero_capacity_airspace_is_never_used(scenario: ScenarioSpec) -> None:
    """空域容量降为 0（v6 §3.4）：绑定该空域的课目不得出现在方案里。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    closed = {aid for aid, cap in scenario.airspace_capacity_override.items() if cap == 0}
    for sortie in outcome.plan.sorties:
        assert sortie.airspace_id not in closed, f"{sortie.sortie_id} 用了容量为 0 的空域"


@given(scenario=arbitrary_scenario())
@AUX
def test_runway_serves_the_aircraft_type(scenario: ScenarioSpec) -> None:
    """S-05：跑道必须服务该架次的机型。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    types = {a.aircraft_id: a.aircraft_type for a in scenario.aircraft}
    serves = {r.runway_id: set(r.aircraft_types) for r in scenario.runways}
    for sortie in outcome.plan.sorties:
        assert types[sortie.aircraft_id] in serves[sortie.runway_id], sortie.sortie_id


# ─────────────────────────────────────────────────────────────────────
# 编成与身份（D-1 / S-09）
# ─────────────────────────────────────────────────────────────────────
@given(scenario=arbitrary_scenario())
@AUX
def test_instructors_never_appear_as_trainee(scenario: ScenarioSpec) -> None:
    """S-09：教员只占带飞教员岗，不作为受训人。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    identity = {p.person_id: p.identity for p in scenario.persons}
    for sortie in outcome.plan.sorties:
        for member in sortie.crew:
            if member.role != "教员":
                assert identity[member.person_id] != IDENTITY_INSTRUCTOR, sortie.sortie_id


@given(scenario=arbitrary_scenario())
@AUX
def test_student_non_dual_missions_are_flown_solo(scenario: ScenarioSpec) -> None:
    """D-1 + §3.1.1 判定式：`需带飞 = (带飞==是) ∧ (身份==学员)`。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    identity = {p.person_id: p.identity for p in scenario.persons}
    dual_required = {m.mission_id: m.dual_required for m in scenario.missions}
    for sortie in outcome.plan.sorties:
        trainee = next(m.person_id for m in sortie.crew if m.role != "教员")
        expected = (
            2 if (dual_required[sortie.mission_id] and identity[trainee] == IDENTITY_STUDENT) else 1
        )
        assert len(sortie.crew) == expected, (
            f"{sortie.sortie_id}: {sortie.mission_id} × {identity[trainee]} 应为 {expected} 人机组"
        )


@given(scenario=arbitrary_scenario())
@AUX
def test_recurrent_sorties_are_single_seat(scenario: ScenarioSpec) -> None:
    """S-11：复训架次机组人数为 1，角色为「复训」。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    for sortie in outcome.plan.sorties:
        if sortie.is_recurrent:
            assert [m.role for m in sortie.crew] == ["复训"], sortie.sortie_id


# ─────────────────────────────────────────────────────────────────────
# 结构与可复现性
# ─────────────────────────────────────────────────────────────────────
@given(scenario=arbitrary_scenario())
@AUX
def test_sorties_fit_the_training_window(scenario: ScenarioSpec) -> None:
    """约束1：架次一律落在 06:00-18:00 之内且不跨日。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    for sortie in outcome.plan.sorties:
        assert TRAINING_WINDOW_START <= sortie.takeoff < sortie.landing <= TRAINING_WINDOW_END
        assert sortie.takeoff >= scenario.window_start
        assert sortie.landing <= scenario.window_end


@given(scenario=arbitrary_scenario())
@AUX
def test_sortie_ids_are_unique_and_sequential(scenario: ScenarioSpec) -> None:
    """架次号按 (日期, 起飞时刻, 课目, 机号) 排序后顺序发号（铁律 9）。"""
    outcome = _solved(scenario)
    assume(outcome.status in SOLVED)
    assert outcome.plan is not None
    ids = [s.sortie_id for s in outcome.plan.sorties]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert ids == [f"S{i + 1:06d}" for i in range(len(ids))]


@given(scenario=arbitrary_scenario())
@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_solving_twice_is_byte_reproducible(scenario: ScenarioSpec) -> None:
    """铁律 9：同场景 + 同 seed + 同 worker 数 → `content_sha256` 逐字节一致。

    **边界见 v6 §3.11.1**：`OPTIMAL` 保证逐字节可复现，`FEASIBLE`（被预算截断）
    不保证 —— 所以这里只对 `OPTIMAL` 断言。
    """
    first = _solved(scenario)
    assume(first.status == "OPTIMAL")
    second = _solved(scenario)
    assert second.status == "OPTIMAL"
    assert first.plan is not None and second.plan is not None
    assert first.plan.content_sha256 == second.plan.content_sha256


@given(scenario=arbitrary_scenario())
@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_infeasible_scenarios_are_diagnosable(scenario: ScenarioSpec) -> None:
    """INFEASIBLE 时必须给出冲突集，且不得混成 UNKNOWN（铁律 8 + v6 §3.9）。

    ⚠️ **这条不能靠 `assume(status == INFEASIBLE)` 去随机场景里捞。** 一开始就是那么
    写的，结果随机场景绝大多数可解，Hypothesis 直接抛 `FailedHealthCheck`
    （`filter_too_much`）—— 而且过滤率会随生成器的任何调整而漂，今天绿明天红。
    改成**构造性**的：把全部空域容量压到 0，则任何架次都排不出来，而生成器保证
    至少有 1 名学员、约束3 要求他本周至少飞 1 次每周必飞类 → **必然 INFEASIBLE**。
    不确定的部分（人员/机队/课目/其余扰动）照旧随机。
    """
    from backend.solver.diagnose import find_conflict_core

    grounded = scenario.with_(
        airspace_capacity_override={s.airspace_id: 0 for s in scenario.airspaces}
    )
    outcome = _solved(grounded)
    assert outcome.status == "INFEASIBLE", (
        f"全部空域容量压到 0 仍判 {outcome.status} —— 期望 INFEASIBLE"
    )
    assert outcome.plan is None
    core = find_conflict_core(grounded.to_bundle(), outcome.cset, time_limit_s=30.0)
    assert core.status == "INFEASIBLE"
    assert core.group_ids, "INFEASIBLE 却给不出冲突集"
    # 冲突集 = SAT core ∪ 结构性不可满足组（v6 §3.9，M2-A §3.10）
    assert set(core.group_ids) == set(core.sat_core_ids) | set(core.structural_ids)
