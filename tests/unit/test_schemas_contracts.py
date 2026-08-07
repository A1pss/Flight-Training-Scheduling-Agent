"""其余契约的单测：意图 / 校验 / 求解 / 检索 / 通用（v6 §3.9、§4.2、§6.5、§7.3）。"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from backend.core.errors import ErrorCode
from backend.schemas import (
    RULE_IDS,
    CheckResult,
    Citation,
    ConflictItem,
    ConstraintSpec,
    DateRange,
    EntityRef,
    ErrorItem,
    GroundedClaim,
    GroundingReport,
    HumanDecision,
    IncrementalConstraint,
    ObjectiveWeights,
    ProbeResult,
    RelaxationProposal,
    RewrittenQuery,
    SchemaCheckReport,
    SolveIntent,
    SolverStats,
    TraceEvent,
    ValidationReport,
    Violation,
)

WEIGHTS = ObjectiveWeights(progress=1.0, disruption=0.3, balance=0.2)


# ─── ObjectiveWeights ────────────────────────────────────────────────


def test_objective_weights_reject_all_zero() -> None:
    with pytest.raises(ValidationError, match="不得同时为 0"):
        ObjectiveWeights(progress=0.0, disruption=0.0, balance=0.0)


def test_objective_weights_reject_negative() -> None:
    with pytest.raises(ValidationError):
        ObjectiveWeights(progress=-1.0, disruption=0.0, balance=1.0)


# ─── SolveIntent ─────────────────────────────────────────────────────


def _intent(**over: object) -> SolveIntent:
    base: dict[str, object] = {
        "scope_persons": "ALL",
        "scope_missions": "ALL",
        "freeze_policy": "BALANCED",
        "freeze_reason": "首轮排班，无既有方案需保护",
        "objective_weights": WEIGHTS,
        "estimated_blast_radius": 0,
    }
    base.update(over)
    return SolveIntent(**base)  # type: ignore[arg-type]


def test_solve_intent_ok() -> None:
    intent = _intent(scope_persons=["P05", "P08"])
    assert intent.scope_persons == ["P05", "P08"]
    assert intent.open_questions == []


def test_solve_intent_rejects_out_of_range_tier() -> None:
    with pytest.raises(ValidationError, match="松弛档位必须在 0~3"):
        _intent(pre_authorized_tiers=[5])


def test_solve_intent_forbids_extra_knobs() -> None:
    """SolveIntent 只能调四类旋钮，不能增删硬约束（v6 §7.3.2）。"""
    with pytest.raises(ValidationError):
        _intent(disable_rule=7)


def test_solve_intent_requires_freeze_reason() -> None:
    with pytest.raises(ValidationError):
        _intent(freeze_reason="")


# ─── IncrementalConstraint ───────────────────────────────────────────


def test_incremental_constraint_keeps_origin_utterance() -> None:
    ic = IncrementalConstraint(
        kind="PIN_RUNWAY",
        targets=["S000117"],
        params={"runway": "RWY-2"},
        origin_utterance="这几个都走 2 号跑道",
        round_no=1,
    )
    assert ic.origin_utterance == "这几个都走 2 号跑道"


def test_incremental_constraint_kinds_include_pin_runway() -> None:
    """PIN_RUNWAY 是 v6 因双跑道新增的修订类型。"""
    for kind in (
        "FORBID",
        "PIN_TIME",
        "PIN_RESOURCE",
        "SHIFT_WINDOW",
        "REDUCE_DENSITY",
        "PIN_RUNWAY",
    ):
        IncrementalConstraint(
            kind=kind,  # type: ignore[arg-type]
            targets=["P04"],
            origin_utterance="x",
            round_no=1,
        )


def test_incremental_constraint_needs_target_and_round() -> None:
    with pytest.raises(ValidationError):
        IncrementalConstraint(kind="FORBID", targets=[], origin_utterance="x", round_no=1)
    with pytest.raises(ValidationError):
        IncrementalConstraint(kind="FORBID", targets=["P04"], origin_utterance="x", round_no=0)


# ─── ConstraintSpec ──────────────────────────────────────────────────


def test_constraint_spec_ok() -> None:
    spec = ConstraintSpec(
        snapshot_id="snap-1",
        ruleset_version="1.3.0",
        semantics_version="1.0.0",
        iso_week="2026W02",
        week_start=date(2026, 1, 5),
        week_end=date(2026, 1, 11),
        scope_persons="ALL",
        scope_missions="ALL",
        relaxation_tier=0,
        objective_weights=WEIGHTS,
        runway_model="dual_runway",
        runways={"RWY-1": ["JL-8", "JL-9"], "RWY-2": ["JL-8"]},
        density_scope={"window_20min": "per_runway", "separation_7min": "airport_wide"},
        airspace_capacity={"SAA": 2, "SAB": 2, "IFR": 1, "RT1": 1, "RT2": 1, "RNG": 1},
        freq_days={"missionA-1": 3, "missionC-1": 7},
        req_max={"missionA-1": 3, "missionC-1": 1},
    )
    assert spec.solver_seed == 42  # 可复现性默认值
    assert spec.density_scope["separation_7min"] == "airport_wide"  # D-2


def test_constraint_spec_week_span() -> None:
    with pytest.raises(ValidationError, match="必须为 7 天"):
        ConstraintSpec(
            snapshot_id="s",
            ruleset_version="1.3.0",
            semantics_version="1.0.0",
            iso_week="2026W02",
            week_start=date(2026, 1, 5),
            week_end=date(2026, 1, 9),
            scope_persons="ALL",
            scope_missions="ALL",
            relaxation_tier=0,
            objective_weights=WEIGHTS,
            runway_model="dual_runway",
        )


# ─── Violation / CheckResult / ValidationReport ──────────────────────


def test_rule_ids_are_fourteen() -> None:
    assert len(RULE_IDS) == 14
    assert RULE_IDS[0] == "C01" and RULE_IDS[-1] == "C14"


def test_violation_rule_id_pattern() -> None:
    Violation(rule_id="C09", detail="跑道 RWY-1 起飞 3 次 > 2")
    with pytest.raises(ValidationError):
        Violation(rule_id="C15", detail="x")
    with pytest.raises(ValidationError):
        Violation(rule_id="C00", detail="x")


def _report(results: list[CheckResult]) -> ValidationReport:
    return ValidationReport(
        plan_id="P1", ruleset_version="1.3.0", semantics_version="1.0.0", results=results
    )


def _pass(rid: str) -> CheckResult:
    return CheckResult(rule_id=rid, rule_title=rid, passed=True, checked_items=7, duration_ms=1.0)


def test_validation_report_all_passed() -> None:
    rpt = _report([_pass(r) for r in RULE_IDS])
    assert rpt.all_passed is True
    assert rpt.missing_rules() == []
    assert rpt.total_checked_items == 14 * 7


def test_empty_report_is_not_passed() -> None:
    """空结果集不算通过——那是没跑，不是过了。"""
    assert _report([]).all_passed is False


def test_missing_rules_detected() -> None:
    rpt = _report([_pass(r) for r in RULE_IDS[:13]])
    assert rpt.missing_rules() == ["C14"]
    assert rpt.all_passed is True  # 跑过的都过了…
    assert rpt.missing_rules()  # …但没跑全，不得宣称 100% 合规


def test_all_violations_collected() -> None:
    v = Violation(rule_id="C06", detail="空域 IFR 并发 2 架 > 容量 1", subjects=["IFR"])
    failing = CheckResult(
        rule_id="C06",
        rule_title="资源有效性与容量",
        passed=False,
        checked_items=3,
        violations=[v],
        duration_ms=0.5,
    )
    rpt = _report([failing, _pass("C07")])
    assert rpt.all_passed is False
    assert [x.rule_id for x in rpt.all_violations()] == ["C06"]


def test_schema_check_report() -> None:
    r = SchemaCheckReport(passed=True, sheet_names=["Sheet1", "Sheet2", "Sheet3", "Sheet4"])
    assert r.diff == []


# ─── SolverStats：UNKNOWN ≠ INFEASIBLE ───────────────────────────────


def test_solver_stats_requires_objective_when_solved() -> None:
    with pytest.raises(ValidationError, match="objective_value 不得为空"):
        SolverStats(
            status="OPTIMAL",
            num_candidates=100,
            num_variables=50,
            num_constraints=80,
            wall_time_ms=120.0,
        )


@pytest.mark.parametrize("status", ["INFEASIBLE", "UNKNOWN", "MODEL_INVALID"])
def test_solver_stats_no_objective_when_unsolved(status: str) -> None:
    st = SolverStats(
        status=status,  # type: ignore[arg-type]
        num_candidates=100,
        num_variables=50,
        num_constraints=80,
        wall_time_ms=30_000.0,
    )
    assert st.objective_value is None
    assert st.random_seed == 42


def test_solver_stats_records_reproducibility_inputs() -> None:
    """seed 与 worker 数都是可复现性的组成部分（v6 §3.11）。"""
    st = SolverStats(
        status="FEASIBLE",
        num_candidates=1,
        num_variables=1,
        num_constraints=1,
        objective_value=3.0,
        wall_time_ms=1.0,
        num_workers=8,
        random_seed=42,
    )
    assert (st.random_seed, st.num_workers) == (42, 8)


# ─── RelaxationProposal：R0 不可松弛 + 必须实证验证 ───────────────────


def test_r0_proposal_rejected() -> None:
    """R0 安全刚性绝不可松弛——在契约层就堵死（v6 §3.10）。"""
    with pytest.raises(ValidationError, match="R0 安全刚性规则绝不可松弛"):
        RelaxationProposal(
            proposal_id="p1",
            tier=1,
            action="放宽跑道密度",
            cost="安全风险",
            affected_rules=["C09"],
            rule_tier="R0",
            authority="训练主任",
            note="x",
        )


def test_verified_proposal_needs_probe_result() -> None:
    with pytest.raises(ValidationError, match="必须携带 verified_result"):
        RelaxationProposal(
            proposal_id="p2",
            tier=1,
            action="顺延 missionF-1",
            cost="进度延迟 1 周",
            affected_rules=["C13"],
            rule_tier="R2",
            authority="排班员",
            verified=True,
        )


def test_unverified_proposal_must_state_reason() -> None:
    """未验证的提案必须明确标注，不得隐瞒（v6 §3.9.1）。"""
    with pytest.raises(ValidationError, match="必须在 note 中明确标注原因"):
        RelaxationProposal(
            proposal_id="p3",
            tier=1,
            action="顺延",
            cost="延迟",
            affected_rules=["C13"],
            rule_tier="R2",
            authority="排班员",
            verified=False,
        )


def test_verified_proposal_ok() -> None:
    p = RelaxationProposal(
        proposal_id="p4",
        tier=1,
        action="missionF-1 本周顺延",
        cost="进度延迟 1 周",
        affected_rules=["C13"],
        rule_tier="R2",
        authority="排班员",
        recommended=True,
        verified=True,
        verified_result=ProbeResult(status="FEASIBLE", sorties=13, wall_time_ms=800.0),
    )
    assert p.verified_result is not None
    assert p.verified_result.sorties == 13


def test_probe_timeout_marked_unknown() -> None:
    """探针超时 → UNKNOWN，明确标注不隐瞒（铁律 8）。"""
    p = RelaxationProposal(
        proposal_id="p5",
        tier=2,
        action="约束3 降级为软目标",
        cost="A 类熟练度下降",
        affected_rules=["C03"],
        rule_tier="R2",
        authority="排班员",
        verified=False,
        note="探针超时，未能确认此方案可行",
    )
    assert p.verified is False and p.note


def test_conflict_item() -> None:
    c = ConflictItem(
        group_id="grp-c13-teacher-capacity",
        rule_ids=["C13", "C11"],
        tier="R2",
        description="约束13 与约束11 在教员容量上互斥",
        subjects=["P01", "P02"],
    )
    assert c.rule_ids == ["C13", "C11"]


# ─── 通用契约 ────────────────────────────────────────────────────────


def test_date_range_ordering() -> None:
    DateRange(start=date(2026, 1, 5), end=date(2026, 1, 11))
    with pytest.raises(ValidationError, match="不得早于"):
        DateRange(start=date(2026, 1, 11), end=date(2026, 1, 5))


def test_entity_ref_keeps_surface() -> None:
    e = EntityRef(kind="person", entity_id="P08", surface="何超", confidence=0.9)
    assert (e.entity_id, e.surface) == ("P08", "何超")


def test_error_item() -> None:
    item = ErrorItem(
        code=ErrorCode.SNAPSHOT_STALE_ON_RESUME,
        message="快照已变更且影响本方案",
        severity="WARN",
        stage="solve",
        retryable=True,
    )
    assert item.code == ErrorCode.SNAPSHOT_STALE_ON_RESUME


def test_trace_event() -> None:
    ev = TraceEvent(seq=0, ts=datetime(2026, 1, 5, 9, 0), agent="planner", kind="decision")
    assert ev.duration_ms is None
    with pytest.raises(ValidationError):
        TraceEvent(seq=0, ts=datetime.now(), agent="x", kind="not_a_kind")  # type: ignore[arg-type]


def test_human_decision() -> None:
    d = HumanDecision(decision="APPROVE", user_id="u1", role="director", authorized_tiers=[3])
    assert d.authorized_tiers == [3]
    with pytest.raises(ValidationError, match="松弛档位必须在 0~3"):
        HumanDecision(decision="APPROVE", user_id="u1", role="director", authorized_tiers=[9])


# ─── 检索契约 ────────────────────────────────────────────────────────


def test_rewritten_query_ambiguity_triggers_clarification() -> None:
    """同时命中「何超/高超」→ 不自行选择，触发反问（v6 §6.5.3）。"""
    q = RewrittenQuery(
        original_query="给高超排班",
        ambiguities=["人名『高超』同时命中 P02 高超 与 P08 何超"],
    )
    assert q.needs_clarification is True


def test_rewritten_query_without_ambiguity() -> None:
    q = RewrittenQuery(original_query="本周有几个架次", semantic_query="本周架次总数")
    assert q.needs_clarification is False


def test_grounding_report_ratio() -> None:
    rpt = GroundingReport(
        claims=[
            GroundedClaim(
                claim="何超本周排了 2 个 A-2",
                citations=[Citation(source_kind="structured", source_id="plan:S000001")],
                supported=True,
            ),
            GroundedClaim(claim="教员容量紧张", supported=False),
        ]
    )
    assert rpt.supported_ratio == 0.5
    assert rpt.unsupported_claims == ["教员容量紧张"]


def test_empty_grounding_report() -> None:
    assert GroundingReport().supported_ratio == 0.0
