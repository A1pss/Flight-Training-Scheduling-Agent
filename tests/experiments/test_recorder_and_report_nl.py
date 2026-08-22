"""`recorder.py` 与 `report_nl.py` 的单测。

recorder 这边钉住的是**录制格式必须能被 `ReplayProvider` 直接吃下去** ——
两侧对不上的话 `traces/` 里存的东西一文不值，而 §12.5.2 与 §12.6 都靠它。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.core.config import Settings
from backend.experiments.recorder import RecordingProvider
from backend.experiments.report_nl import (
    completion,
    fit_calibrator,
    intent_accuracy,
    misexecution,
    slot_f1,
    threshold_sweep,
)
from backend.llm.replay import ReplayProvider
from backend.llm.types import LLMRequest, LLMResponse


class _FakeProvider:
    """按顺序吐固定回答的假 Provider。"""

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.calls = 0

    def chat(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text=self._answers.pop(0))


def test_recorded_trace_replays_byte_identically(tmp_path: Path) -> None:
    """录制 → 重放，结果逐字相同，且重放**零次**打到真 Provider。"""
    trace = tmp_path / "run.jsonl"
    inner = _FakeProvider(["答案甲", "答案乙"])
    rec = RecordingProvider(inner, trace)
    reqs = [LLMRequest(messages=[{"role": "user", "content": q}]) for q in ("甲", "乙")]
    original = [rec.chat(r).text for r in reqs]
    assert rec.call_count == 2

    cfg = Settings(_env_file=None, LLM_PROVIDER="replay", REPLAY_TRACE_DIR=tmp_path)
    replay = ReplayProvider(cfg)
    assert replay.size == 2
    assert [replay.chat(r).text for r in reqs] == original
    assert inner.calls == 2, "重放不许再打真 Provider"


def test_replay_can_run_the_same_trace_twice(tmp_path: Path) -> None:
    """`rewind()` 之后再跑一遍要一模一样 —— 重放一致性就是比这两遍。"""
    trace = tmp_path / "run.jsonl"
    rec = RecordingProvider(_FakeProvider(["X"]), trace)
    req = LLMRequest(messages=[{"role": "user", "content": "q"}])
    rec.chat(req)
    cfg = Settings(_env_file=None, LLM_PROVIDER="replay", REPLAY_TRACE_DIR=tmp_path)
    replay = ReplayProvider(cfg)
    first = replay.chat(req).text
    replay.rewind()
    assert replay.chat(req).text == first


def test_recorded_line_is_tagged_as_llm(tmp_path: Path) -> None:
    """轨迹里混着 Harness 的工具事件，Provider 只认 `kind == "llm"` 的行。"""
    trace = tmp_path / "run.jsonl"
    rec = RecordingProvider(_FakeProvider(["X"]), trace)
    rec.chat(LLMRequest(messages=[{"role": "user", "content": "q"}]))
    record = json.loads(trace.read_text(encoding="utf-8").strip())
    assert record["kind"] == "llm"
    assert "request_key" in record and "response" in record


# ── report_nl ────────────────────────────────────────────────────────
def _row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "item_id": "NL-X-001",
        "layer": "standard_schedule",
        "round_index": 1,
        "expected_intent": "schedule",
        "expected_action": "solve",
        "expected_slots": {},
        "observed_intent": "schedule",
        "observed_slots": {},
        "confidence": 1.0,
        "source": "rule",
        "agreement": 1.0,
        "has_ambiguity": False,
        "llm_calls": 0,
        "planner_asked": False,
        "calibration_features": {},
        "error": "",
    }
    base.update(kw)
    return base


def test_correct_clarification_counts_as_success() -> None:
    """§12.2 的主指标口径：**「正确地反问澄清」计为成功**。"""
    rows = [_row(expected_action="ask_clarify", has_ambiguity=True)]
    assert completion(rows, 0.75).point == 1.0


def test_misexecution_reports_both_denominators() -> None:
    """v6 没把「比例」的分母说死，两个都要算（差 5.8 倍，会改变达标与否）。"""
    rows = [
        _row(expected_action="ask_clarify", has_ambiguity=False),  # 该反问却执行了
        _row(expected_action="solve"),
        _row(expected_action="solve"),
        _row(expected_action="solve"),
    ]
    over_all, over_unclear, bad = misexecution(rows, 0.75)
    assert bad == 1
    assert over_all.point == pytest.approx(0.25), "全量分母 1/4"
    assert over_unclear.point == pytest.approx(1.0), "该反问分母 1/1"


def test_threshold_sweep_is_monotone_in_asking() -> None:
    """阈值越高越倾向反问 → 误执行只减不增。反推那一步靠的就是这个单调性。"""
    rows = [
        _row(expected_action="ask_clarify", source="llm", confidence=c) for c in (0.3, 0.6, 0.9)
    ]
    sweep = threshold_sweep(rows)
    counts = [s["misexec_count"] for s in sweep]
    assert counts == sorted(counts, reverse=True)
    assert sweep[-1]["misexec_count"] == 0, "阈值 1.0 时 LLM 路径全部反问"


def test_intent_accuracy_is_independent_of_threshold() -> None:
    rows = [_row(observed_intent="query", expected_intent="schedule")]
    assert intent_accuracy(rows).point == 0.0


def test_slot_f1_skips_kinds_with_no_samples() -> None:
    total, per = slot_f1([_row()])
    assert per["aircraft"].tp == per["aircraft"].fp == per["aircraft"].fn == 0
    assert total.f1 == 0.0


def test_calibrator_refuses_to_fit_on_old_format_rows() -> None:
    """没有校准特征就明确报错 —— 静默跳过会让报告里出现一个没人知道怎么来的 ECE。"""
    with pytest.raises(ValueError, match="旧格式"):
        fit_calibrator([_row()])


def test_calibrator_fits_when_features_are_present() -> None:
    rows = [
        _row(
            observed_intent="schedule" if ok else "query",
            calibration_features={
                "agreement": 1.0 if ok else 0.34,
                "first_pass": ok,
                "retries": 0 if ok else 2,
                "worst_failure_mode": "" if ok else "entity_hallucination",
            },
        )
        for ok in (True, True, False, True, False, True)
    ]
    calibrator, ece, bins = fit_calibrator(rows)
    assert calibrator.fitted
    assert 0.0 <= ece <= 1.0
    assert sum(b.count for b in bins) == len(rows)
