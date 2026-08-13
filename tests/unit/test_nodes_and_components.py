"""六个确定性节点与四个 LLM 节点的纯逻辑部分（v6 §7.2.3 / §7.2.4）。

这里只测**不需要 PG 的那一半**：状态判定、载荷装配、核验器、回滚。
连库的部分（compile_spec / solve / commit_plan 的四件事）在
`tests/integration/test_graph_live.py`。
"""

from __future__ import annotations

from datetime import date, time
from typing import Any, cast

import pytest

from backend.components.explain import (
    build_fact_index,
    fallback_text,
    rewrite_hint,
    split_claims,
    unsupported_ratio,
    verify_claim,
    verify_claims,
)
from backend.components.planner import apply_intent_tier, rollback_revision
from backend.components.route import clarification_command, route_node
from backend.graph.state import FTSState, initial_state, user_utterance
from backend.graph.state import get as state_get
from backend.nodes import DETERMINISTIC_NODE_NAMES
from backend.nodes.human_gate import DECISION_ROUTES, gate_payload, parse_decision
from backend.nodes.resume_guard import StalenessVerdict, change_dates, plan_entity_ids
from backend.nodes.validate import inject_nogoods
from backend.schemas.common import HumanDecision
from backend.schemas.intent import IncrementalConstraint, ObjectiveWeights, SolveIntent
from backend.schemas.validation import CheckResult, ValidationReport, Violation
from tests.fixtures.graph_fixtures import (
    FakeHarness,
    all_green_report,
    directory,
    plan,
    sortie,
    stats,
    text_output,
)
from tests.unit.test_planner import spec

TODAY = date(2026, 1, 7)


def state(**kwargs: Any) -> FTSState:
    base = initial_state(trace_id="t1", user_id="u1", snapshot_id="snap_test")
    cast(dict[str, Any], base).update(kwargs)
    return base


# ─────────────────────────────────────────────────────────────────────
# 黑板状态（v6 §7.4）
# ─────────────────────────────────────────────────────────────────────
def test_initial_state_has_every_key() -> None:
    """键齐全是刻意的：读取路径上只剩「值是不是 None」一种情况要判。"""
    s = cast(dict[str, Any], initial_state(trace_id="t", user_id="u"))
    for key in (
        "trace_id",
        "tenant_id",
        "user_id",
        "intent",
        "request",
        "intent_confidence",
        "snapshot_id",
        "ruleset_version",
        "semantics_version",
        "solve_intent",
        "revision_stack",
        "revision_round",
        "needs_clarification",
        "user_role",
        "constraint_spec",
        "relaxation_tier",
        "solve_attempts",
        "solution",
        "solver_stats",
        "validation",
        "schema_check",
        "conflict_set",
        "relaxation_proposals",
        "blocked_items",
        "workbook_path",
        "explanation",
        "grounding_report",
        "trace_events",
        "errors",
        "needs_human",
        "human_decision",
    ):
        assert key in s, f"initial_state 漏了 {key}"


def test_trace_events_and_errors_use_add_reducer() -> None:
    """v6 §7.4 末句：只增不改，多个组件并发写入时不相互覆盖。"""
    from operator import add
    from typing import get_args, get_origin, get_type_hints

    hints = get_type_hints(FTSState, include_extras=True)
    for field in ("trace_events", "errors"):
        annotation = hints[field]
        assert get_origin(annotation) is not None
        assert add in get_args(annotation), f"{field} 没有 add reducer"


def test_get_folds_none_into_the_default() -> None:
    s = state(explanation=None)
    assert state_get(s, "explanation", "缺省") == "缺省"


def test_user_utterance_accepts_dicts_and_objects() -> None:
    assert user_utterance(state(messages=[{"role": "user", "content": "你好"}])) == "你好"
    assert user_utterance(state(messages=[])) == ""


def test_deterministic_node_names_match_the_acl_ban_list() -> None:
    """两张表漂移 = 新节点没进禁令 = 它能被注册成 LLM 工具（铁律 4 当场失效）。"""
    from backend.harness.acl import FORBIDDEN_NODES

    assert set(DETERMINISTIC_NODE_NAMES) == FORBIDDEN_NODES


# ─────────────────────────────────────────────────────────────────────
# route 节点
# ─────────────────────────────────────────────────────────────────────
def test_route_sends_scheduling_intent_to_planner() -> None:
    command = route_node(
        state(messages=[{"role": "user", "content": "给所有人排班，2026W02"}]),
        directory=directory(),
        today=TODAY,
    )
    assert command.goto == "planner"
    update = cast(dict[str, Any], command.update)
    assert update["intent"] == "schedule"
    assert update["week_start"] == "2026-01-05"


def test_route_asks_when_the_name_is_ambiguous() -> None:
    """歧义反问走**二级路径**：槽位抽取由 LLM 给原文表述，消解由字典说了算。

    一级规则路径**只做精确匹配**（见 `scan_slots` 的文档）：它承诺「确定且可测」，
    没有把「郝超」认成人名的能力，也不该有——那是 NER，不是正则。
    """
    import json

    harness = FakeHarness(
        responses=[text_output("route", json.dumps({"intent": "schedule", "persons": ["郝超"]}))]
    )
    command = route_node(
        state(messages=[{"role": "user", "content": "把郝超那摊子事儿弄一弄"}]),
        directory=directory(),
        today=TODAY,
        harness=harness,
    )
    assert command.goto == "human_gate"
    update = cast(dict[str, Any], command.update)
    assert update["needs_human"] is True
    assert "高超(P02)" in update["explanation"]


def test_route_hands_off_query_intent_out_of_the_graph() -> None:
    command = route_node(
        state(messages=[{"role": "user", "content": "何超的训练进度"}]),
        directory=directory(),
        today=TODAY,
    )
    assert command.goto == "END"
    kinds = [e.kind for e in cast(dict[str, Any], command.update)["trace_events"]]
    assert "handoff" in kinds


def test_route_does_not_reclassify_on_planner_bounce_back() -> None:
    """第二个入口：回来是因为缺信息，不是意图变了。"""
    intent = SolveIntent(
        scope_persons="ALL",
        scope_missions="ALL",
        freeze_policy="BALANCED",
        freeze_reason="x",
        objective_weights=ObjectiveWeights(progress=1.0, disruption=1.0, balance=1.0),
        estimated_blast_radius=0,
        open_questions=["要连带调整教员吗？"],
    )
    s = state(needs_clarification=True, solve_intent=intent)
    command = route_node(s, directory=directory(), today=TODAY)
    assert command.goto == "human_gate"
    update = cast(dict[str, Any], command.update)
    assert "要连带调整教员吗？" in update["explanation"]
    assert update["needs_clarification"] is False


def test_clarification_command_merges_questions_and_ambiguities() -> None:
    s = state(ambiguities=[{"question": "「郝超」有多个可能"}])
    command = clarification_command(s)
    assert "「郝超」有多个可能" in cast(dict[str, Any], command.update)["explanation"]


def test_route_below_threshold_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from backend.core.config import Settings

    settings = Settings(_env_file=None, CONFIDENCE_THRESHOLD=0.99)  # type: ignore[call-arg]
    harness = FakeHarness(
        responses=[
            text_output("route", json.dumps({"intent": "schedule"})),
            text_output("route", json.dumps({"intent": "query"})),
            text_output("route", json.dumps({"intent": "export"})),
        ]
    )
    command = route_node(
        state(messages=[{"role": "user", "content": "看看这个"}]),
        directory=directory(),
        today=TODAY,
        harness=harness,
        settings=settings,
    )
    assert command.goto == "human_gate"
    assert "置信度" in cast(dict[str, Any], command.update)["explanation"]


# ─────────────────────────────────────────────────────────────────────
# validate 节点的 no-good cut
# ─────────────────────────────────────────────────────────────────────
def report_with_violation(subject: str) -> ValidationReport:
    return ValidationReport(
        plan_id="pl_test",
        ruleset_version="rs_1.3",
        semantics_version="sem_1.1",
        results=[
            CheckResult(
                rule_id="C07",
                rule_title="周转",
                passed=False,
                checked_items=3,
                duration_ms=1.0,
                violations=[Violation(rule_id="C07", subjects=[subject], detail="周转不足")],
            )
        ],
    )


def test_nogood_targets_the_named_sortie_not_the_whole_person() -> None:
    p = plan([sortie("S000001"), sortie("S000002", day=1, aircraft_id="AC27")])
    updated = inject_nogoods(spec(), report_with_violation("S000001"), p, round_no=2)
    cut = updated.incremental_constraints[-1]
    assert cut.kind == "FORBID"
    assert cut.targets == ["S000001"]
    assert "系统自检回灌" in cut.origin_utterance


def test_nogood_falls_back_to_the_subject_when_no_sortie_is_named() -> None:
    p = plan([sortie("S000001")])
    updated = inject_nogoods(spec(), report_with_violation("AC10"), p, round_no=2)
    assert updated.incremental_constraints[-1].targets == ["AC10"]


def test_nogood_is_a_no_op_when_there_is_nothing_to_forbid() -> None:
    p = plan([sortie("S000001")])
    empty = ValidationReport(
        plan_id="pl_test", ruleset_version="rs_1.3", semantics_version="sem_1.1", results=[]
    )
    assert inject_nogoods(spec(), empty, p, round_no=2).incremental_constraints == []


# ─────────────────────────────────────────────────────────────────────
# human_gate
# ─────────────────────────────────────────────────────────────────────
def test_decision_routes_match_v6() -> None:
    assert DECISION_ROUTES == {
        "APPROVE": "commit_plan",
        "REVISE": "planner",
        "REJECT": "END",
    }


def test_gate_payload_is_json_serializable() -> None:
    import json

    p = plan([sortie("S000001")])
    payload = gate_payload(
        state(
            solution=p, validation=all_green_report(), explanation="说明", workbook_path="/x.xlsx"
        )
    )
    json.dumps(payload)  # 不抛就说明可序列化 —— 跨日恢复的硬要求
    assert payload["plan"]["sorties"] == 1
    assert payload["validation"]["plan_id"] == "pl_test"


def test_gate_payload_without_a_plan() -> None:
    payload = gate_payload(state())
    assert payload["plan"] is None and payload["validation"] is None


@pytest.mark.parametrize("raw", ["APPROVE", "approve", {"decision": "APPROVE"}])
def test_parse_decision_accepts_three_shapes(raw: Any) -> None:
    assert parse_decision(raw).decision == "APPROVE"


def test_parse_decision_passes_through_the_object() -> None:
    decision = HumanDecision(decision="REJECT", user_id="u1", role="director")
    assert parse_decision(decision) is decision


def test_parse_decision_refuses_to_default_to_approve() -> None:
    """把看不懂的输入默认成 APPROVE 是这套系统里最贵的默认值。"""
    with pytest.raises(ValueError, match="无法解析人工决策"):
        parse_decision(42)


# ─────────────────────────────────────────────────────────────────────
# resume_guard 的判据
# ─────────────────────────────────────────────────────────────────────
def test_plan_entities_include_airspace_and_runway() -> None:
    """v6 §9.2 点名了空域 —— 一个都不能漏。"""
    p = plan([sortie("S000001", airspace_id="IFR", runway_id="RWY-2")])
    entities = plan_entity_ids(p)
    assert entities["airspace"] == frozenset({"IFR"})
    assert entities["runway"] == frozenset({"RWY-2"})
    assert entities["person"] == frozenset({"P01", "P06"})
    assert entities["aircraft"] == frozenset({"AC10"})


def test_change_dates_reads_nested_date_fields() -> None:
    from backend.ingestion.diff import Change

    change = Change(
        entity_type="person",
        entity_id="P03",
        kind="MODIFIED",
        before={"unavailable_dates": ["2026-01-05"]},
        after={"unavailable_dates": ["2026-01-05", "2026-01-09"]},
        changed_fields=("unavailable_dates",),
    )
    assert change_dates(change) == {date(2026, 1, 5), date(2026, 1, 9)}


def test_change_dates_is_empty_for_non_date_fields() -> None:
    from backend.ingestion.diff import Change

    change = Change(
        entity_type="person",
        entity_id="P03",
        kind="MODIFIED",
        before={"name": "吴鹏"},
        after={"name": "吴 鹏"},
        changed_fields=("name",),
    )
    assert change_dates(change) == set()


def test_staleness_summary_wording() -> None:
    unchanged = StalenessVerdict(old_snapshot_id="a", new_snapshot_id="a", changed=False)
    assert "未变更" in unchanged.summary()
    assert unchanged.affects_plan is False


# ─────────────────────────────────────────────────────────────────────
# 修订回滚（FTS-3005）
# ─────────────────────────────────────────────────────────────────────
def test_rollback_pops_the_last_revision_and_explains() -> None:
    constraint = IncrementalConstraint(
        kind="PIN_RUNWAY",
        targets=["AC84"],
        params={"runway_id": "RWY-2"},
        origin_utterance="AC84 那班也走 2 号跑道",
        round_no=1,
    )
    s = state(
        revision_stack=[constraint],
        constraint_spec=spec(incremental_constraints=[constraint]),
    )
    update = rollback_revision(s, reason="RWY-2 只服务 JL-8")
    assert update["revision_stack"] == []
    error = update["errors"][0]
    assert error.code.value == "FTS-3005"
    assert "AC84 那班也走 2 号跑道" in error.message
    assert update["constraint_spec"].incremental_constraints == []


def test_rollback_on_an_empty_stack_is_harmless() -> None:
    update = rollback_revision(state(), reason="x")
    assert update["revision_stack"] == []


def test_apply_intent_tier_takes_the_highest_authorized() -> None:
    intent = SolveIntent(
        scope_persons="ALL",
        scope_missions="ALL",
        freeze_policy="BALANCED",
        freeze_reason="x",
        objective_weights=ObjectiveWeights(progress=1.0, disruption=1.0, balance=1.0),
        pre_authorized_tiers=[0, 2],
        estimated_blast_radius=0,
    )
    assert apply_intent_tier(intent) == 2


# ─────────────────────────────────────────────────────────────────────
# explain 的确定性核验器
# ─────────────────────────────────────────────────────────────────────
def sample_plan() -> Any:
    return plan(
        [
            sortie("S000001", day=0, crew=(("P01", "教员"), ("P06", "学员"))),
            sortie(
                "S000002",
                day=0,
                takeoff=time(10, 0),
                aircraft_id="AC27",
                crew=(("P05", "单飞"),),
            ),
            sortie("S000003", day=2, aircraft_id="AC34", crew=(("P07", "单飞"),)),
        ]
    )


def test_fact_index_counts_only_what_it_can_count() -> None:
    index = build_fact_index(sample_plan(), all_green_report(), stats())
    assert index.supports_number("3")  # 架次总数
    assert index.supports_number("14")  # 已校验规则条数
    assert index.supports_entity("AC27")
    assert not index.supports_entity("AC99")


def test_split_claims_drops_empty_sentences() -> None:
    assert split_claims("本周共 3 个架次。其中 1 个带飞。\n\n") == [
        "本周共 3 个架次。",
        "其中 1 个带飞。",
    ]


def test_a_claim_with_a_wrong_number_is_unsupported() -> None:
    index = build_fact_index(sample_plan(), all_green_report(), stats())
    assert verify_claim("本周共 3 个架次。", index).supported
    assert not verify_claim("本周共 47 个架次。", index).supported


def test_a_claim_with_a_hallucinated_entity_is_unsupported() -> None:
    index = build_fact_index(sample_plan())
    assert not verify_claim("AC99 执行了一个架次。", index).supported


def test_a_sentence_without_facts_needs_no_verification() -> None:
    """核验一句没有事实内容的话，得到的只是一个虚高的比率。"""
    index = build_fact_index(sample_plan())
    assert verify_claim("本周排班已完成。", index).supported


def test_verify_claims_reports_the_offending_sentences() -> None:
    index = build_fact_index(sample_plan(), all_green_report(), stats())
    report = verify_claims("本周共 3 个架次。AC99 也飞了 9 次。", index)
    assert len(report.claims) == 2
    assert report.unsupported_claims == ["AC99 也飞了 9 次。"]
    assert unsupported_ratio(report) == pytest.approx(0.5)


def test_rewrite_hint_names_the_offending_tokens() -> None:
    index = build_fact_index(sample_plan(), all_green_report(), stats())
    report = verify_claims("AC99 也飞了 9 次。", index)
    hint = rewrite_hint(report, index)
    assert "AC99" in hint and "9" in hint
    assert "不确定的量就不要写" in hint


def test_fallback_text_is_assembled_facts_not_generated_prose() -> None:
    """LLM 挂了也不会出现查无实据的数 —— 因为它是拼出来的。"""
    p = sample_plan()
    text = fallback_text(p, all_green_report())
    index = build_fact_index(p, all_green_report(), stats())
    assert verify_claims(text, index).unsupported_claims == []
    assert "LLM 服务不可用" in text
