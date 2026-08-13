"""录制与重放（v6 §7.7.1 第 7 行 / §12.5.2）。

出口标准两条，这里各占一半：

- **重放一致率 100%**：`replay(trace_id)` 复现的最终状态与原始运行逐字段相等；
- **零 LLM 调用**：重放期间对 Ollama 的实际请求数为 0（socket 级证据见
  `tests/guardrail/test_replay_zero_llm.py`，这里验的是逻辑层）。

重放刻意做得「脆」：调用次序变了、请求内容变了、工具参数变了，一律抛。
重放要抓的就是这些变化——一个「找不到就将就一下」的重放没有任何断言价值。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend.core.errors import LLMUnavailableError
from backend.harness.context import ContextBlock
from backend.harness.harness import Harness
from backend.harness.recorder import (
    ToolReplayer,
    TraceRecorder,
    diff_states,
    load_trace,
    replay,
)
from backend.harness.types import AgentSpec
from backend.llm.mock import tool_response
from tests.fixtures.harness_fixtures import (
    build_harness,
    harness_settings,
    registry_with_test_handlers,
)

ROUTE = AgentSpec(name="route", tools=("resolve_person", "resolve_week"))
KNOWLEDGE = AgentSpec(name="knowledge", tools=("prereq_cte", "memory.search"))


def _record_a_run(trace_root: Path) -> dict[str, Any]:
    """录一条两步的轨迹：先解析人，再查先修。"""
    harness, _, _ = build_harness(
        [
            tool_response("resolve_person", {"surface": "何超"}),
            tool_response("prereq_cte", {"person_id": "P08", "mission_id": "missionB-1"}),
        ],
        trace_root=trace_root,
    )
    state = _run_flow(harness)
    harness.recorder.finish(state)
    return state


def _run_flow(harness: Harness) -> dict[str, Any]:
    """被录制 / 被重放的那段「图」。两边必须是同一段代码。"""
    first = harness.call(ROUTE, [ContextBlock(kind="summary", content="快照：8 人 8 机")])
    person = first.results[0].value["person_id"]
    second = harness.call(KNOWLEDGE, [ContextBlock(kind="summary", content="快照：8 人 8 机")])
    return {
        "person_id": person,
        "eligible": second.results[0].value["eligible"],
        "tools_used": [c.name for c in first.calls] + [c.name for c in second.calls],
    }


# ─── 录制 ────────────────────────────────────────────────────────────


def test_events_and_meta_are_written(tmp_path: Path) -> None:
    state = _record_a_run(tmp_path)
    trace = load_trace("trace_test", root=tmp_path)
    assert len(trace.llm_events) == 2
    assert len(trace.tool_events) == 2
    assert trace.meta.final_state == state
    assert trace.meta.finished is True
    assert trace.meta.provider == "mock"


def test_events_carry_request_fingerprints(tmp_path: Path) -> None:
    _record_a_run(tmp_path)
    trace = load_trace("trace_test", root=tmp_path)
    assert all(len(e.request_key) == 64 for e in trace.llm_events)
    assert trace.llm_events[0].request_key != trace.llm_events[1].request_key


def test_notes_are_recorded_for_offline_stats(tmp_path: Path) -> None:
    """失败模式分布表是从 note 事件离线统计出来的（§12.5.1）。"""
    harness, _, _ = build_harness(
        [
            tool_response("resolve_person", {}),
            tool_response("resolve_person", {"surface": "何超"}),
        ],
        trace_root=tmp_path,
    )
    harness.call(ROUTE)
    harness.recorder.finish({})
    notes = load_trace("trace_test", root=tmp_path).note_events
    assert notes[0].topic == "contract_failure"
    assert notes[0].detail["modes"] == ["missing_field"]


def test_in_memory_recorder_writes_nothing(tmp_path: Path) -> None:
    recorder = TraceRecorder("t")
    recorder.note("route", "budget", "x")
    assert len(recorder.events) == 1
    assert not list(tmp_path.iterdir())


# ─── 重放一致性 ──────────────────────────────────────────────────────


def test_replay_reproduces_the_final_state_field_by_field(tmp_path: Path) -> None:
    original = _record_a_run(tmp_path)
    result = replay("trace_test", _run_flow, root=tmp_path, settings=harness_settings())

    assert result.consistent is True
    assert result.diff == ()
    assert result.final_state == original
    assert result.llm_calls == 2
    assert result.tool_calls == 2


def test_replay_makes_zero_calls_to_the_real_provider(tmp_path: Path) -> None:
    """重放期间 provider 是 ReplayProvider，MockProvider 一次都不会被碰。"""
    _record_a_run(tmp_path)
    calls: list[str] = []

    def counting_flow(harness: Harness) -> dict[str, Any]:
        calls.append(type(harness._provider).__name__)
        return _run_flow(harness)

    replay("trace_test", counting_flow, root=tmp_path, settings=harness_settings())
    assert calls == ["ReplayProvider"]


def test_replay_detects_a_changed_flow(tmp_path: Path) -> None:
    """图改了 → 重放抛，而不是「凑合出一个结果」。"""
    _record_a_run(tmp_path)

    def changed_flow(harness: Harness) -> dict[str, Any]:
        # 少了 ContextBlock → 请求内容变了 → 指纹对不上
        harness.call(ROUTE)
        return {}

    with pytest.raises(LLMUnavailableError, match="请求指纹不匹配"):
        replay("trace_test", changed_flow, root=tmp_path, settings=harness_settings())


def test_replay_detects_extra_calls(tmp_path: Path) -> None:
    _record_a_run(tmp_path)

    def longer_flow(harness: Harness) -> dict[str, Any]:
        state = _run_flow(harness)
        harness.call(ROUTE, [ContextBlock(kind="summary", content="多出来的一次")])
        return state

    with pytest.raises(LLMUnavailableError, match="已耗尽"):
        replay("trace_test", longer_flow, root=tmp_path, settings=harness_settings())


def test_replay_reports_state_diff_instead_of_crashing(tmp_path: Path) -> None:
    """最终状态对不上时给出**逐字段**的差异，而不是一句「不一致」。"""
    _record_a_run(tmp_path)

    def flow_with_different_state(harness: Harness) -> dict[str, Any]:
        state = _run_flow(harness)
        state["person_id"] = "P07"
        state.pop("eligible")
        state["extra"] = 1
        return state

    result = replay(
        "trace_test", flow_with_different_state, root=tmp_path, settings=harness_settings()
    )
    assert result.consistent is False
    assert any("person_id" in d for d in result.diff)
    assert any("eligible" in d for d in result.diff)
    assert any("extra" in d for d in result.diff)


def test_missing_trace_raises(tmp_path: Path) -> None:
    with pytest.raises(LLMUnavailableError, match="不完整或不存在"):
        load_trace("no_such_trace", root=tmp_path)


# ─── 工具重放 ────────────────────────────────────────────────────────


def test_tool_replayer_checks_order_and_arguments(tmp_path: Path) -> None:
    _record_a_run(tmp_path)
    events = load_trace("trace_test", root=tmp_path).tool_events
    replayer = ToolReplayer(events)

    assert replayer.next_result("resolve_person", {"surface": "何超"}).value["person_id"] == "P08"
    with pytest.raises(LLMUnavailableError, match="与录制不符"):
        replayer.next_result("prereq_cte", {"person_id": "P05", "mission_id": "missionB-1"})


def test_tool_replayer_runs_out(tmp_path: Path) -> None:
    _record_a_run(tmp_path)
    events = load_trace("trace_test", root=tmp_path).tool_events
    replayer = ToolReplayer(events)
    for event in events:
        replayer.next_result(event.tool, event.arguments)
    assert replayer.remaining == 0
    with pytest.raises(LLMUnavailableError, match="没有对应录制"):
        replayer.next_result("resolve_person", {"surface": "何超"})


def test_replay_without_tool_replay_executes_handlers(tmp_path: Path) -> None:
    """`replay_tools=False` 时工具真跑——只有在工具确实无副作用时才该这么用。"""
    _record_a_run(tmp_path)
    result = replay(
        "trace_test",
        _run_flow,
        root=tmp_path,
        settings=harness_settings(),
        registry=registry_with_test_handlers()[0],
        replay_tools=False,
    )
    assert result.consistent is True


# ─── 状态比对 ────────────────────────────────────────────────────────


def test_diff_states_is_empty_for_equal_states() -> None:
    assert diff_states({"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1}) == ()


def test_diff_states_lists_every_difference() -> None:
    diff = diff_states({"a": 1, "b": 2}, {"a": 9, "c": 3})
    assert len(diff) == 3
