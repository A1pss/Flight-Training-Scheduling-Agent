"""实验轨迹同时冻结 LLM 与工具返回。"""

import json
from pathlib import Path

import pytest
from langgraph.types import Command

from backend.core.errors import LLMUnavailableError
from backend.experiments.recorder import (
    RecordingCheckpointStream,
    RecordingToolStream,
    load_checkpoint_replayer,
    load_tool_replayer,
)
from backend.experiments.run_trajectory import (
    _assert_non_feasible_solve_replay,
    _restore_solve_command,
    _serialize_solve_command,
)
from backend.harness.types import ToolResult
from backend.llm.replay import ReplayProvider
from backend.llm.types import LLMRequest, LLMResponse
from backend.schemas.solver import SolverStats
from tests.fixtures.graph_fixtures import plan


class _Inner:
    def chat(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=request.messages[0]["content"])


def test_llm_record_keeps_request_for_fingerprint_diagnosis(tmp_path: Path) -> None:
    from backend.experiments.recorder import RecordingProvider

    path = tmp_path / "trace.jsonl"
    provider = RecordingProvider(_Inner(), path)
    request = LLMRequest(messages=[{"role": "user", "content": "诊断请求"}])
    provider.chat(request)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["request"] == request.model_dump(mode="json")
    assert ReplayProvider._load(path)[0].response.text == "诊断请求"


def test_tool_stream_round_trip_and_preserves_failures(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    stream = RecordingToolStream(path)
    stream.write(
        "diagnosis",
        "probe_solve",
        {"iso_week": "2026W02", "relaxations": ["TIER_1"]},
        ToolResult(tool="probe_solve", ok=True, value={"status": "OPTIMAL", "sorties": 1}),
    )
    stream.write(
        "knowledge",
        "sql_query",
        {"sql": "SELECT broken"},
        ToolResult(tool="sql_query", ok=False, error="UndefinedColumn"),
    )

    replay = load_tool_replayer(path)
    first = replay.next_result("probe_solve", {"iso_week": "2026W02", "relaxations": ["TIER_1"]})
    second = replay.next_result("sql_query", {"sql": "SELECT broken"})

    assert first.value == {"status": "OPTIMAL", "sorties": 1}
    assert second.ok is False
    assert second.error == "UndefinedColumn"
    assert replay.remaining == 0


def test_checkpoint_stream_round_trip_in_mixed_trace(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text('{"kind":"llm","request_key":"ignored"}\n', encoding="utf-8")
    stream = RecordingCheckpointStream(path)
    stream.write("solve", {"status": "FEASIBLE", "sorties": [1, 2]})
    stream.write("diagnosis_base", ["C3", "C9"])

    replay = load_checkpoint_replayer(path)

    assert replay.remaining == 2
    assert replay.next_payload("solve") == {"status": "FEASIBLE", "sorties": [1, 2]}
    second = replay.next_checkpoint("diagnosis_base")
    assert second.seq == 1
    assert second.payload == ["C3", "C9"]
    assert replay.remaining == 0


def test_checkpoint_stream_rejects_non_json_payload_without_advancing(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    stream = RecordingCheckpointStream(path)

    with pytest.raises(TypeError, match="不能 JSON 序列化"):
        stream.write("solve", object())

    event = stream.write("solve", {"status": "OPTIMAL"})
    assert event.seq == 0
    assert load_checkpoint_replayer(path).next_payload("solve") == {"status": "OPTIMAL"}


def test_checkpoint_replayer_rejects_name_mismatch_without_consuming(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    RecordingCheckpointStream(path).write("solve", {"status": "FEASIBLE"})
    replay = load_checkpoint_replayer(path)

    with pytest.raises(LLMUnavailableError, match="checkpoint 与录制不符") as exc_info:
        replay.next_payload("diagnosis_base")

    assert exc_info.value.details["expected_name"] == "solve"
    assert exc_info.value.details["actual_name"] == "diagnosis_base"
    assert replay.remaining == 1


def test_checkpoint_replayer_rejects_sequence_gap(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text(
        json.dumps(
            {"kind": "checkpoint", "seq": 1, "name": "solve", "payload": {}},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(LLMUnavailableError, match="checkpoint 录制序号不连续") as exc_info:
        load_checkpoint_replayer(path)

    assert exc_info.value.details["expected_seq"] == 0
    assert exc_info.value.details["actual_seq"] == 1


def test_checkpoint_replayer_reports_exhaustion(tmp_path: Path) -> None:
    replay = load_checkpoint_replayer(tmp_path / "missing.jsonl")

    with pytest.raises(LLMUnavailableError, match="没有对应录制") as exc_info:
        replay.next_payload("solve")

    assert exc_info.value.details["recorded_checkpoints"] == 0


def test_feasible_solve_command_round_trips_with_typed_models() -> None:
    schedule = plan([])
    command = Command(
        goto="validate",
        update={
            "solution": schedule,
            "solver_stats": SolverStats(
                status="FEASIBLE",
                num_candidates=1,
                num_variables=2,
                num_constraints=3,
                objective_value=4.0,
                wall_time_ms=5.0,
            ),
            "blocked_items": [],
            "solve_attempts": 1,
        },
    )

    restored = _restore_solve_command(_serialize_solve_command(command))

    assert restored.goto == "validate"
    assert restored.update["solution"] == schedule  # type: ignore[index]
    assert restored.update["solver_stats"].status == "FEASIBLE"  # type: ignore[index,union-attr]


def test_optimal_solve_replay_rejects_content_fingerprint_drift() -> None:
    first = plan([])
    second = first.model_copy(update={"content_sha256": "f" * 64})

    def command(schedule: object) -> Command[str]:
        return Command(
            goto="validate",
            update={
                "solution": schedule,
                "solver_stats": SolverStats(
                    status="OPTIMAL",
                    num_candidates=1,
                    num_variables=2,
                    num_constraints=3,
                    objective_value=4.0,
                    wall_time_ms=5.0,
                ),
            },
        )

    with pytest.raises(RuntimeError, match="OPTIMAL solve 重放方案指纹漂移"):
        _assert_non_feasible_solve_replay(command(first), command(second))
