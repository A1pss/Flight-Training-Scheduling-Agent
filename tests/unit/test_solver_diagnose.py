"""不可行诊断：冲突集 / 归因 / 松弛提案 / 实证验证 / 探针预算（v6 §3.9 / §3.10）。"""

from __future__ import annotations

from datetime import time, timedelta

import pytest
from pydantic import ValidationError

from backend.schemas.solver import ProbeResult, RelaxationProposal
from backend.solver.candidates import enumerate_candidates
from backend.solver.data import ScenarioOverrides
from backend.solver.diagnose import (
    BUDGET_EXHAUSTED_NOTE,
    DROP_TO_RULE,
    PROBE_TIMEOUT_NOTE,
    ProbeBudget,
    ProposalDraft,
    attribute,
    diagnose,
    draft_proposals,
    find_conflict_core,
    probe_solve,
    structurally_unsatisfiable,
    verify_proposals,
)
from backend.solver.model import RelaxationSettings
from tests.fixtures.solver_facts import TEST_WEEK_START, make_bundle

#: 一个必然不可行的构造：B-1 绑定的空域整周关闭，而 B-1 是本周必排项
INFEASIBLE_OVERRIDES = ScenarioOverrides(airspace_capacity={"NAV": 0})


def _infeasible_bundle(**kwargs: object):  # type: ignore[no-untyped-def]
    return make_bundle(overrides=INFEASIBLE_OVERRIDES, time_limit_s=20.0, **kwargs)


# ─────────────────────────────────────────────────────────────────────
# 探针预算池（v6 §3.9.2）
# ─────────────────────────────────────────────────────────────────────
def test_probe_budget_defaults_match_v6() -> None:
    budget = ProbeBudget.from_settings()
    assert (budget.per_call_s, budget.max_calls, budget.total_s) == (30.0, 5, 120.0)
    assert budget.is_exhausted() is False


def test_probe_budget_trips_on_call_count() -> None:
    """单请求 5 次上限：第 6 次不许再跑。"""
    budget = ProbeBudget(per_call_s=30.0, max_calls=5, total_s=120.0)
    for _ in range(5):
        budget.record(0.1)
    assert budget.is_exhausted() is True
    assert budget.next_limit() > 0  # 累计预算还有余，但次数用光了
    bundle = _infeasible_bundle()
    result, outcome = probe_solve(bundle, relaxation=RelaxationSettings(tier=1), budget=budget)
    assert (result, outcome) == (None, None)
    assert budget.calls == 5, "熔断后不该再消耗次数"


def test_probe_budget_trips_on_total_seconds() -> None:
    """累计 120s 上限：用光之后 `next_limit()` 归 0，探针直接不跑。"""
    budget = ProbeBudget(per_call_s=30.0, max_calls=99, total_s=1.0)
    budget.record(1.5)
    assert budget.is_exhausted() is True
    assert budget.next_limit() == 0.0
    result, _ = probe_solve(
        _infeasible_bundle(), relaxation=RelaxationSettings(tier=1), budget=budget
    )
    assert result is None


def test_probe_call_limit_caps_single_run_time() -> None:
    """单次探针时限 = min(单次上限, 累计余额)。"""
    budget = ProbeBudget(per_call_s=30.0, max_calls=5, total_s=8.0)
    budget.record(5.0)
    assert budget.next_limit() == pytest.approx(3.0)


def test_probe_snapshot_is_reportable() -> None:
    budget = ProbeBudget(per_call_s=30.0, max_calls=5, total_s=120.0)
    budget.record(1.25)
    snap = budget.snapshot()
    assert snap["calls"] == 1.0 and snap["spent_s"] == 1.25
    assert snap["max_calls"] == 5.0 and snap["total_s"] == 120.0


def test_probe_solve_is_read_only_and_records_cost() -> None:
    bundle = _infeasible_bundle()
    budget = ProbeBudget(per_call_s=15.0, max_calls=5, total_s=60.0)
    result, outcome = probe_solve(bundle, relaxation=RelaxationSettings(tier=1), budget=budget)
    assert result is not None and outcome is not None
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert budget.calls == 1 and budget.spent_s > 0
    # 探针不改动传入的 bundle（只读）
    assert bundle.spec.solver_time_limit_s == 20.0


# ─────────────────────────────────────────────────────────────────────
# 最小冲突集与归因
# ─────────────────────────────────────────────────────────────────────
def test_conflict_core_is_empty_when_feasible() -> None:
    bundle = make_bundle()
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    core = find_conflict_core(bundle, cset, time_limit_s=20.0)
    assert core.status in ("OPTIMAL", "FEASIBLE")
    assert core.group_ids == ()


def test_conflict_core_names_the_frequency_group() -> None:
    bundle = _infeasible_bundle()
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    core = find_conflict_core(bundle, cset, time_limit_s=20.0)
    assert core.status == "INFEASIBLE"
    assert "C13_frequency" in core.group_ids
    assert core.num_candidates == len(cset.candidates)


def test_structural_augmentation_catches_groups_the_sat_core_omits() -> None:
    """CP-SAT 的 core 是极小的；结构性不可满足组要另外补上（召回率 100% 的要求）。"""
    bundle = make_bundle(
        overrides=ScenarioOverrides(airspace_capacity={"LAC": 0, "LAD": 0, "NAV": 0}),
        time_limit_s=20.0,
    )
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    structural = structurally_unsatisfiable(bundle, cset)
    assert set(structural) == {"C03_weekly", "C13_frequency"}
    core = find_conflict_core(bundle, cset, time_limit_s=20.0)
    assert {"C03_weekly", "C13_frequency"} <= set(core.group_ids)
    assert set(core.sat_core_ids) <= set(core.group_ids)


def test_attribution_names_the_real_root_cause() -> None:
    """归因要说出「为什么这个组合一个候选都没有」，而不只是重复组名。"""
    bundle = _infeasible_bundle()
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    core = find_conflict_core(bundle, cset, time_limit_s=20.0)
    items = attribute(bundle, cset, core)
    assert items
    freq = next(i for i in items if i.group_id == "C13_frequency")
    assert "C06" in freq.rule_ids, "空域关闭的真实根因（约束6）没有进冲突项"
    assert any("NAV" in s or "空域" in s for s in freq.subjects)
    assert freq.tier == "R2"


def test_drop_to_rule_maps_maintenance_to_both_c06_and_c07() -> None:
    """v6 §12.3 的 I2 把「机队全部维护」标为约束6，建模落点却在约束7 —— 两个都报。"""
    from backend.solver.candidates import DROP_AIRCRAFT_MAINTENANCE

    assert set(DROP_TO_RULE[DROP_AIRCRAFT_MAINTENANCE]) == {6, 7}


def test_attribution_covers_resource_groups() -> None:
    """跑道全关 → 冲突集里要出现约束9（v6 §12.3 I5 的验证目标）。"""
    bundle = make_bundle(
        overrides=ScenarioOverrides(closed_runways=frozenset({"RWY-7", "RWY-8"})),
        time_limit_s=20.0,
    )
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    core = find_conflict_core(bundle, cset, time_limit_s=20.0)
    assert core.status == "INFEASIBLE"
    items = attribute(bundle, cset, core)
    rule_ids = {rid for item in items for rid in item.rule_ids}
    assert "C09" in rule_ids, f"跑道模型没进冲突集：{rule_ids}"


# ─────────────────────────────────────────────────────────────────────
# 松弛提案：R0 恒不可松弛
# ─────────────────────────────────────────────────────────────────────
def test_proposal_contract_rejects_r0() -> None:
    """契约层就堵死 R0（v6 §3.10）。"""
    with pytest.raises(ValidationError, match="R0"):
        RelaxationProposal(
            proposal_id="bad",
            tier=1,
            action="放宽跑道密度",
            cost="不可接受",
            affected_rules=["C09"],
            rule_tier="R0",
            authority="排班员",
            verified=False,
            note="x",
        )


def test_drafts_never_target_r0_groups() -> None:
    """跑道全关的场景里冲突集全是 R0 → 一条提案都不该生成。"""
    bundle = make_bundle(
        overrides=ScenarioOverrides(closed_runways=frozenset({"RWY-7", "RWY-8"})),
        time_limit_s=20.0,
    )
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    core = find_conflict_core(bundle, cset, time_limit_s=20.0)
    drafts = draft_proposals(bundle, core)
    assert all(d.rule_tier != "R0" for d in drafts)


def test_drafts_follow_the_ladder() -> None:
    bundle = _infeasible_bundle()
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    core = find_conflict_core(bundle, cset, time_limit_s=20.0)
    drafts = {d.proposal_id: d for d in draft_proposals(bundle, core)}
    assert "TIER1" in drafts
    assert drafts["TIER1"].tier == 1 and drafts["TIER1"].authority == "排班员"
    assert all("C09" not in d.affected_rules for d in drafts.values())


# ─────────────────────────────────────────────────────────────────────
# 实证验证（v6 §3.9.1）
# ─────────────────────────────────────────────────────────────────────
def test_verified_proposal_carries_probe_result() -> None:
    bundle = _infeasible_bundle()
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    core = find_conflict_core(bundle, cset, time_limit_s=20.0)
    budget = ProbeBudget(per_call_s=15.0, max_calls=5, total_s=60.0)
    proposals = verify_proposals(draft_proposals(bundle, core), bundle, budget=budget)
    verified = [p for p in proposals if p.verified]
    assert verified, "没有一条提案通过探针验证"
    for p in verified:
        assert p.verified_result is not None
        assert p.verified_result.status in ("OPTIMAL", "FEASIBLE")
        assert "探针实测" in p.cost
    assert sum(1 for p in proposals if p.recommended) == 1


def test_infeasible_proposal_is_discarded_not_shown() -> None:
    """`INFEASIBLE` 的提案**直接丢弃**（v6 §3.9.1）。

    构造：一个只放宽约束13 的提案，但真正掐死方案的是约束3（A 类空域全关）——
    Tier 1 不动约束3，所以探针必然仍判 INFEASIBLE。
    """
    bundle = make_bundle(
        overrides=ScenarioOverrides(airspace_capacity={"LAC": 0, "LAD": 0}),
        time_limit_s=15.0,
    )
    draft = ProposalDraft(
        proposal_id="TIER1",
        tier=1,
        action="只放宽约束13",
        cost="进度顺延",
        affected_rules=("C13",),
        rule_tier="R2",
        authority="排班员",
        settings=RelaxationSettings(tier=1),
    )
    budget = ProbeBudget(per_call_s=15.0, max_calls=5, total_s=60.0)
    assert verify_proposals([draft], bundle, budget=budget) == ()
    assert budget.calls == 1, "提案被丢弃了，但探针确实跑过一次"


def test_budget_exhausted_proposal_is_flagged_not_hidden() -> None:
    bundle = _infeasible_bundle()
    draft = ProposalDraft(
        proposal_id="TIER1",
        tier=1,
        action="放宽约束13",
        cost="进度顺延",
        affected_rules=("C13",),
        rule_tier="R2",
        authority="排班员",
        settings=RelaxationSettings(tier=1),
    )
    spent = ProbeBudget(per_call_s=30.0, max_calls=0, total_s=120.0)
    proposals = verify_proposals([draft], bundle, budget=spent)
    assert len(proposals) == 1
    assert proposals[0].verified is False
    assert proposals[0].note == BUDGET_EXHAUSTED_NOTE
    assert proposals[0].verified_result is None


def test_unverified_proposal_must_carry_a_note() -> None:
    """契约层：未验证的提案必须说明原因，不许隐瞒（v6 §3.9.1）。"""
    with pytest.raises(ValidationError, match="不得隐瞒"):
        RelaxationProposal(
            proposal_id="x",
            tier=1,
            action="a",
            cost="c",
            affected_rules=["C13"],
            rule_tier="R2",
            authority="排班员",
            verified=False,
        )
    with pytest.raises(ValidationError, match="verified_result"):
        RelaxationProposal(
            proposal_id="x",
            tier=1,
            action="a",
            cost="c",
            affected_rules=["C13"],
            rule_tier="R2",
            authority="排班员",
            verified=True,
        )
    ok = RelaxationProposal(
        proposal_id="x",
        tier=1,
        action="a",
        cost="c",
        affected_rules=["C13"],
        rule_tier="R2",
        authority="排班员",
        verified=False,
        note=PROBE_TIMEOUT_NOTE,
    )
    assert ok.note == PROBE_TIMEOUT_NOTE


def test_r1_proposal_probes_the_minimum_relaxation() -> None:
    """Tier 3 的放宽量是**探出来的**，不是代码拍的（v6 §3.9 第 2 步）。

    构造：单人单日 ≤3，但 A 类两门课目各需 2 次、只有 1 天可飞 → 需要日上限 +1。
    """
    off = frozenset(TEST_WEEK_START + timedelta(days=i) for i in range(1, 7))
    bundle = make_bundle(
        student_completed=frozenset(),
        student_unavailable=off,
        aircraft_count=4,
        airspace_capacity=2,
        time_limit_s=15.0,
    )
    draft = ProposalDraft(
        proposal_id="TIER3-C12",
        tier=3,
        action="放宽约束12",
        cost="疲劳风险",
        affected_rules=("C12",),
        rule_tier="R1",
        authority="训练主任",
        settings=RelaxationSettings(tier=3),
        escalating_field="daily_sorties_bonus",
    )
    budget = ProbeBudget(per_call_s=15.0, max_calls=5, total_s=60.0)
    proposals = verify_proposals([draft], bundle, budget=budget)
    if proposals and proposals[0].verified:
        assert "探针测出的最小放宽量" in proposals[0].action
        assert proposals[0].authority == "训练主任"
    else:
        # 放宽 R1 也解不开时不得凭空呈现，只能是丢弃或标注未验证
        assert all(not p.verified for p in proposals)


# ─────────────────────────────────────────────────────────────────────
# 编排
# ─────────────────────────────────────────────────────────────────────
def test_diagnose_end_to_end_on_infeasible_case() -> None:
    bundle = _infeasible_bundle()
    result = diagnose(bundle, time_limit_s=20.0, budget=ProbeBudget(30.0, 5, 60.0))
    assert result.status == "INFEASIBLE"
    assert result.conflicts
    assert result.verified_proposals
    assert result.useful_proposals
    assert result.escalate is False
    assert result.budget["calls"] >= 1


def test_diagnose_escalates_when_only_empty_plans_are_reachable() -> None:
    """I2 那一类：资源被抹平 → 松弛只能「取消本周」→ 升级人工（v6 §12.3 的合格输出）。

    关键断言是 `useful_proposals == ()`：Tier 2 确实能「可行」，但它给出的方案是
    **0 架次** —— 那不是排班。此时必须升级人工，不能把「一个都不排」端给用户当方案。
    """
    bundle = make_bundle(
        overrides=ScenarioOverrides(closed_runways=frozenset({"RWY-7", "RWY-8"})),
        time_limit_s=20.0,
    )
    result = diagnose(bundle, time_limit_s=20.0, budget=ProbeBudget(15.0, 5, 30.0))
    assert result.status == "INFEASIBLE"
    assert result.useful_proposals == ()
    assert result.escalate is True
    assert "升级人工" in result.escalation_reason
    assert "R0" in result.escalation_reason or "调配资源" in result.escalation_reason


def test_diagnose_refuses_to_call_feasible_case_infeasible() -> None:
    bundle = make_bundle()
    result = diagnose(bundle, time_limit_s=20.0, budget=ProbeBudget(15.0, 5, 30.0))
    assert result.status != "INFEASIBLE"
    assert result.conflicts == () and result.proposals == ()
    assert "无需诊断" in result.escalation_reason


def test_diagnose_never_labels_unknown_as_infeasible() -> None:
    """铁律 8：诊断求解本身超时 → UNKNOWN，绝不当成 INFEASIBLE。"""
    bundle = make_bundle(
        student_completed=frozenset(),
        aircraft_count=4,
        overrides=ScenarioOverrides(window_end=time(7, 0)),
    )
    result = diagnose(bundle, time_limit_s=0.02, budget=ProbeBudget(1.0, 1, 1.0))
    assert result.status != "INFEASIBLE" or result.conflicts
    if result.status == "UNKNOWN":
        assert "不得当作 INFEASIBLE" in result.escalation_reason


def test_probe_result_contract_keeps_status() -> None:
    result = ProbeResult(status="UNKNOWN", sorties=0)
    assert result.status == "UNKNOWN" and result.debts == []
