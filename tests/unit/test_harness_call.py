"""`Harness.call()` 的八项职责串起来跑（v6 §7.7 伪码）。

这组用例走的是真 ACL、真校验器、真预算账本、真缓存，只有**模型是假的**——
这也正是 Harness 该被测的形态：它的价值全在「模型犯浑时系统还稳」。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.errors import ArchitecturalBanError, ToolPermissionDeniedError
from backend.harness.budget import BudgetLedger, BudgetLimits
from backend.harness.context import ContextBlock
from backend.harness.harness import constrained_schema, extract_tool_calls
from backend.harness.types import AgentSpec, FailureMode
from backend.llm.mock import text_response, tool_response
from backend.llm.types import LLMResponse, RawToolCall
from tests.fixtures.harness_fixtures import build_harness, harness_settings

ROUTE = AgentSpec(name="route", tools=("resolve_person", "resolve_week", "ask_user"))
PLANNER = AgentSpec(name="planner", tools=("resolve_person", "estimate_scope"))
KNOWLEDGE = AgentSpec(name="knowledge", tools=("prereq_cte", "sql_query", "memory.search"))
DIAGNOSIS = AgentSpec(name="diagnosis", tools=("min_conflict_set", "probe_solve"))


# ─── 正常路径 ────────────────────────────────────────────────────────


def test_happy_path_executes_the_tool() -> None:
    harness, provider, handlers = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})]
    )
    out = harness.call(ROUTE, [ContextBlock(kind="summary", content="快照：8 人 8 机")])

    assert out.calls[0].name == "resolve_person"
    assert out.results[0].value == {"person_id": "P08", "surface": "何超"}
    assert out.first_pass is True
    assert out.degraded is False
    assert out.llm_calls == 1
    assert handlers["resolve_person"].calls == 1
    assert provider.call_count == 1


def test_prompt_version_is_attached_to_output_and_trace() -> None:
    """§7.7.1 第 8 行：提示词版本随 trace 记录。"""
    harness, _, _ = build_harness([tool_response("resolve_person", {"surface": "何超"})])
    out = harness.call(ROUTE)
    assert out.prompt_version == "route/system@v1"
    assert harness.recorder.events[0].prompt_version == "route/system@v1"  # type: ignore[union-attr]


def test_system_prompt_is_prepended() -> None:
    harness, _, _ = build_harness([tool_response("resolve_person", {"surface": "何超"})])
    harness.call(ROUTE)
    request = harness.recorder.events[0].request  # type: ignore[union-attr]
    assert request.messages[0]["role"] == "system"
    assert "意图路由" in request.messages[0]["content"]


def test_tool_schemas_are_exposed_in_native_mode() -> None:
    harness, _, _ = build_harness([tool_response("resolve_person", {"surface": "何超"})])
    harness.call(ROUTE)
    request = harness.recorder.events[0].request  # type: ignore[union-attr]
    assert [t.name for t in request.tools] == list(ROUTE.tools)
    assert request.format_schema is None


def test_multiple_tool_calls_in_one_turn() -> None:
    response = LLMResponse(
        tool_calls=(
            RawToolCall(name="resolve_person", arguments={"surface": "何超"}),
            RawToolCall(name="resolve_week", arguments={"surface": "本周"}),
        )
    )
    harness, _, _ = build_harness([response])
    out = harness.call(ROUTE)
    assert [c.name for c in out.calls] == ["resolve_person", "resolve_week"]
    assert harness.usage().tool_calls == 2


# ─── 契约失败 → 回灌 → 重试 ─────────────────────────────────────────


def test_retry_after_contract_failure() -> None:
    harness, _, _ = build_harness(
        [
            tool_response("resolve_person", {}),  # 缺 surface
            tool_response("resolve_person", {"surface": "何超"}),
        ]
    )
    out = harness.call(ROUTE)
    assert out.degraded is False
    assert out.first_pass is False
    assert out.llm_calls == 2
    assert [f.mode for a in out.attempts for f in a.failures] == [FailureMode.MISSING_FIELD]


def test_feedback_carries_field_expected_actual() -> None:
    """回灌的那条消息必须带「哪个字段、期望什么、实际收到什么」。"""
    harness, _, _ = build_harness(
        [
            tool_response("prereq_cte", {"person_id": "何超", "mission_id": "missionB-1"}),
            tool_response("prereq_cte", {"person_id": "P08", "mission_id": "missionB-1"}),
        ]
    )
    harness.call(KNOWLEDGE)
    second_request = harness.recorder.events[2].request  # type: ignore[union-attr]
    feedback = second_request.messages[-1]["content"]
    assert "person_id" in feedback
    assert "何超" in feedback
    assert "entity_hallucination" in feedback
    # 模型自己上一轮的输出也要回放给它看
    assert "prereq_cte" in second_request.messages[-2]["content"]


def test_degrades_to_form_after_two_retries() -> None:
    """重试 ≤2 次，仍失败 → FTS-4002 转人工表单。"""
    harness, provider, _ = build_harness([tool_response("resolve_person", {})] * 3)
    out = harness.call(ROUTE)
    assert out.degraded is True
    assert out.error_code == "FTS-4002"
    assert len(out.attempts) == 3  # 首次 + 2 次重试
    assert provider.call_count == 3
    assert harness.stats.degraded == 1


def test_prose_instead_of_tool_call_is_a_failure() -> None:
    harness, _, _ = build_harness([text_response("我建议给何超排三个架次。")] * 3)
    out = harness.call(ROUTE)
    assert out.degraded is True
    assert out.attempts[0].failures[0].mode is FailureMode.JSON_MALFORMED


def test_text_only_component_accepts_prose() -> None:
    spec = AgentSpec(name="explain", tools=(), requires_tool_call=False)
    harness, _, _ = build_harness([text_response("本周共 14 个架次。")])
    out = harness.call(spec)
    assert out.degraded is False
    assert out.text == "本周共 14 个架次。"
    assert out.calls == ()


# ─── 越权（运行时拦截，越权即抛）────────────────────────────────────


def test_calling_a_deterministic_node_raises_architectural_ban() -> None:
    harness, _, _ = build_harness([tool_response("solve", {"iso_week": "2026W02"})])
    with pytest.raises(ArchitecturalBanError, match="确定性节点"):
        harness.call(PLANNER)
    assert harness.stats.acl_denials == 1


def test_calling_another_components_tool_raises() -> None:
    harness, _, _ = build_harness([tool_response("render_workbook", {"plan_id": "p1"})])
    with pytest.raises(ToolPermissionDeniedError, match="无权调用"):
        harness.call(PLANNER)


def test_acl_denial_is_not_retried() -> None:
    """越权不能走重试路径——允许重试等于允许模型试探到成功为止。"""
    harness, provider, _ = build_harness([tool_response("solve", {})] * 3)
    with pytest.raises(ArchitecturalBanError):
        harness.call(PLANNER)
    assert provider.call_count == 1


def test_hallucinated_tool_name_is_a_contract_failure_not_acl() -> None:
    """编出来的名字属于契约失败（可重试）；越权是另一回事，别混。"""
    harness, _, _ = build_harness(
        [
            tool_response("resolve_pilot", {"surface": "何超"}),
            tool_response("resolve_person", {"surface": "何超"}),
        ]
    )
    out = harness.call(ROUTE)
    assert out.degraded is False
    assert out.attempts[0].failures[0].mode is FailureMode.ENUM_OUT_OF_RANGE
    assert harness.stats.acl_denials == 0


def test_exposing_a_tool_outside_the_row_fails_before_any_llm_call() -> None:
    harness, provider, _ = build_harness([])
    with pytest.raises(ToolPermissionDeniedError):
        harness.call(AgentSpec(name="route", tools=("probe_solve",)))
    assert provider.call_count == 0


def test_tool_allowed_by_matrix_but_not_exposed_is_retryable() -> None:
    """矩阵允许、这轮没给 → 属于「记岔了工具表」，回灌重试，不算越权。"""
    harness, _, _ = build_harness(
        [
            tool_response("ask_user", {"question": "哪一周？", "resolution": "answer"}),
            tool_response("resolve_person", {"surface": "何超"}),
        ]
    )
    out = harness.call(AgentSpec(name="route", tools=("resolve_person",)))
    assert out.degraded is False
    assert harness.stats.acl_denials == 0


# ─── 预算 ────────────────────────────────────────────────────────────


def test_budget_break_returns_fts_4003_with_partial_results() -> None:
    cfg = harness_settings()
    ledger = BudgetLedger(BudgetLimits(max_llm_calls=1), trace_id="t")
    ledger.charge_llm(10, measured=True)
    harness, provider, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})], settings=cfg, ledger=ledger
    )
    out = harness.call(ROUTE)
    assert out.degraded is True
    assert out.error_code == "FTS-4003"
    assert provider.call_count == 0  # 熔断发生在发出请求**之前**
    assert harness.stats.budget_breaks == 1


def test_budget_break_between_tool_calls_keeps_completed_work() -> None:
    response = LLMResponse(
        tool_calls=(
            RawToolCall(name="resolve_person", arguments={"surface": "何超"}),
            RawToolCall(name="resolve_week", arguments={"surface": "本周"}),
        )
    )
    harness, _, handlers = build_harness(
        [response], ledger=BudgetLedger(BudgetLimits(max_tool_calls=1))
    )
    out = harness.call(ROUTE)
    assert out.error_code == "FTS-4003"
    assert handlers["resolve_person"].calls == 1
    assert handlers["resolve_week"].calls == 0


def test_probe_uses_the_independent_pool() -> None:
    harness, _, _ = build_harness(
        [tool_response("probe_solve", {"iso_week": "2026W02", "relaxations": ["R1"]})]
    )
    out = harness.call(DIAGNOSIS)
    assert out.results[0].ok is True
    usage = harness.usage()
    assert usage.probe_calls == 1
    assert usage.tool_calls == 1


# ─── 缓存 ────────────────────────────────────────────────────────────


def test_second_identical_call_hits_cache() -> None:
    call = tool_response("prereq_cte", {"person_id": "P08", "mission_id": "missionB-1"})
    harness, _, handlers = build_harness([call, call])
    first = harness.call(KNOWLEDGE)
    second = harness.call(KNOWLEDGE)
    assert first.results[0].cached is False
    assert second.results[0].cached is True
    assert handlers["prereq_cte"].calls == 1


def test_cache_is_scoped_by_snapshot() -> None:
    call = tool_response("prereq_cte", {"person_id": "P08", "mission_id": "missionB-1"})
    harness, _, handlers = build_harness([call, call])
    harness.call(KNOWLEDGE, snapshot_id="snap_a")
    harness.call(KNOWLEDGE, snapshot_id="snap_b")
    assert handlers["prereq_cte"].calls == 2


def test_tool_failure_is_reported_not_raised() -> None:
    harness, _, _ = build_harness(
        [tool_response("prereq_cte", {"person_id": "P08", "mission_id": "missionB-1"})]
    )

    def boom(_a: dict[str, object]) -> object:
        raise RuntimeError("库连不上")

    harness.registry.register("prereq_cte", boom)
    out = harness.call(KNOWLEDGE)
    assert out.results[0].ok is False
    assert "库连不上" in out.results[0].error


# ─── 受约束 JSON 模式 ────────────────────────────────────────────────


def test_constrained_mode_sends_format_schema_and_parses_json() -> None:
    harness, _, _ = build_harness(
        [text_response(json.dumps({"tool": "resolve_person", "arguments": {"surface": "何超"}}))]
    )
    harness.modes.reset()
    for _ in range(5):
        harness.modes.report_failure("route")
    assert harness.modes.pick("route") == "constrained_json"

    out = harness.call(ROUTE)
    request = harness.recorder.events[0].request  # type: ignore[union-attr]
    assert request.tools == ()
    assert request.format_schema == constrained_schema(ROUTE.tools)
    assert out.calls[0].name == "resolve_person"
    assert out.mode == "constrained_json"


def test_constrained_mode_reports_malformed_json() -> None:
    harness, _, _ = build_harness([text_response("{不是 JSON")] * 3)
    for _ in range(5):
        harness.modes.report_failure("route")
    out = harness.call(ROUTE)
    assert out.attempts[0].failures[0].mode is FailureMode.JSON_MALFORMED


def test_text_only_component_never_switches_to_constrained_json() -> None:
    """纯生成型组件永远 native：把「写一段解释」约束成 {"tool": …} 没有意义，
    工具名的 enum 还会是空数组。"""
    harness, _, _ = build_harness([text_response("本周共 14 个架次。")])
    for _ in range(5):
        harness.modes.report_failure("explain")
    assert harness.modes.pick("explain") == "constrained_json"

    out = harness.call(AgentSpec(name="explain", tools=(), requires_tool_call=False))
    assert out.mode == "native"
    assert out.degraded is False
    request = harness.recorder.events[0].request  # type: ignore[union-attr]
    assert request.format_schema is None


def test_extract_tool_calls_accepts_a_list() -> None:
    payload = json.dumps(
        [
            {"tool": "resolve_person", "arguments": {"surface": "何超"}},
            {"tool": "resolve_week", "arguments": {"surface": "本周"}},
        ]
    )
    calls, failure = extract_tool_calls(text_response(payload), "constrained_json")
    assert failure is None
    assert [c.name for c in calls] == ["resolve_person", "resolve_week"]


def test_constrained_schema_pins_tool_names_to_an_enum() -> None:
    schema = constrained_schema(("resolve_person", "ask_user"))
    assert schema["properties"]["tool"]["enum"] == ["resolve_person", "ask_user"]
    assert schema["required"] == ["tool", "arguments"]


# ─── 上下文与统计 ────────────────────────────────────────────────────


def test_context_trim_is_recorded_as_a_note() -> None:
    cfg = harness_settings(LLM_NUM_CTX=200, HARNESS_RESERVE_OUTPUT_TOKENS=50)
    harness, _, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})], settings=cfg
    )
    harness.call(
        ROUTE,
        [ContextBlock(kind="evidence", content="很长的检索片段" * 60, label="e1")],
    )
    notes = [e for e in harness.recorder.events if getattr(e, "topic", "") == "context_trim"]
    assert notes and notes[0].detail["dropped"] == ["evidence:e1"]  # type: ignore[union-attr]


def test_stats_track_first_pass_and_failure_modes() -> None:
    harness, _, _ = build_harness(
        [
            tool_response("resolve_person", {}),
            tool_response("resolve_person", {"surface": "何超"}),
            tool_response("resolve_person", {"surface": "张勇"}),
        ]
    )
    harness.call(ROUTE)
    harness.call(ROUTE)
    assert harness.stats.llm_requests == 3
    assert harness.stats.first_pass == 1
    assert harness.stats.failure_modes == {"missing_field": 1}
    assert harness.stats.failure_mode_counter["missing_field"] == 1


def test_tokens_are_marked_estimated_under_mock() -> None:
    """mock 没有实测 token 计数——账本必须如实标记，不许当实测数上报。"""
    harness, _, _ = build_harness([tool_response("resolve_person", {"surface": "何超"})])
    harness.call(ROUTE)
    assert harness.usage().tokens_estimated is True
    assert harness.usage().tokens > 0


def test_trace_is_written_to_disk_when_root_given(tmp_path: Path) -> None:
    harness, _, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})], trace_root=tmp_path
    )
    harness.call(ROUTE)
    meta = harness.recorder.finish({"resolved": "P08"})
    lines = (tmp_path / "trace_test" / "events.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 2  # 一条 llm + 一条 tool
    assert json.loads(lines[0])["kind"] == "llm"
    assert meta.final_state == {"resolved": "P08"}
    assert (tmp_path / "trace_test" / "meta.json").is_file()
