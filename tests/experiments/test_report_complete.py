"""完整实验报告器的回归测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.experiments.report_complete import report_nl, report_trajectory


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _nl_row(*, action: str = "solve", observed: str = "schedule") -> dict[str, Any]:
    return {
        "item_id": "NL-1",
        "layer": "standard_schedule",
        "round_index": 1,
        "expected_intent": "schedule",
        "expected_action": action,
        "expected_slots": {
            "persons": ["P01"],
            "aircraft": [],
            "missions": [],
            "week": "2026W02",
            "constraint_modifiers": [],
        },
        "observed_intent": observed,
        "confidence": 1.0,
        "source": "rule",
        "agreement": 1.0,
        "has_ambiguity": False,
        "llm_calls": 0,
        "calibration_features": {},
        "planner_ran": True,
        "planner_asked": False,
        "planner_questions": [],
        "observed_slots": {
            "persons": ["P01"],
            "aircraft": [],
            "missions": [],
            "week": "2026W02",
            "constraint_modifiers": [],
        },
        "wall_s": 0.1,
        "error": "",
        "variant": "main",
    }


def _trajectory_row(*, path_ok: bool, observed_calls: int) -> dict[str, Any]:
    return {
        "item_id": "TRJ-1",
        "flow": "query",
        "observed_path": ["route", "END"],
        "scored_path": ["route", "END"],
        "score_components": ["knowledge"],
        "raw_observed_calls": observed_calls,
        "expected_path": ["route", "knowledge", "tool:sql_query", "END"],
        "path_ok": path_ok,
        "path_reason": "test",
        "path_similarity": 0.5,
        "steps": {
            "expected_steps": 1,
            "required_steps": 1,
            "tool_hits": int(observed_calls > 0),
            "param_hits": int(observed_calls > 0),
            "param_denominator": int(observed_calls > 0),
            "unmatched_required": int(observed_calls == 0),
            "redundant": 0,
            "observed_calls": observed_calls,
        },
        "invalid_loop": False,
        "revision_translation_ok": None,
        "revision_rollback_ok": None,
        "answer_text": "",
        "answer_fidelity_ok": None,
        "error": "",
    }


def test_report_nl_includes_failures_and_paired_changes(tmp_path: Path) -> None:
    before, current, output = (tmp_path / name for name in ("before.jsonl", "now.jsonl", "r.json"))
    _jsonl(before, [_nl_row(observed="query")])
    _jsonl(current, [_nl_row()])

    result = report_nl(current, before, output, threshold=0.75)

    assert result["summary"]["completion"]["point"] == 1.0
    assert result["paired_before_after"]["intent"]["improved"] == 1
    assert result["failures"] == []
    assert json.loads(output.read_text(encoding="utf-8"))["experiment"] == "experiment_1"


def test_report_trajectory_has_wilson_failures_and_replay_check(tmp_path: Path) -> None:
    before, record, replay, output = (
        tmp_path / name for name in ("before.jsonl", "record.jsonl", "replay.jsonl", "report.json")
    )
    old = _trajectory_row(path_ok=False, observed_calls=0)
    new = _trajectory_row(path_ok=True, observed_calls=1)
    _jsonl(before, [old])
    _jsonl(record, [new])
    _jsonl(replay, [new])

    result = report_trajectory(replay, before, record, output)

    assert result["metrics"]["path_correct"]["point"] == 1.0
    assert result["metrics"]["missing_call_rate"]["point"] == 0.0
    assert "missing_call_rate" in result["diagnostic_metrics_without_threshold"]
    assert all(
        breach["metric"] not in {"missing_call_rate", "redundant_call_rate", "invalid_loop_rate"}
        for breach in result["stopline_breaches_over_5pt"]
    )
    assert result["paired_before_after"]["path"]["improved"] == 1
    assert result["record_replay"]["identical"] is True
    assert result["record_replay"]["gate_passed"] is True
    assert result["failures"] == []
