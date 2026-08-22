"""`run_judge.py` 的单测：一致率 / Kappa / 门槛判定的算法本身。

**这段逻辑不该只能在一块 24G 显卡前面才验证得了** —— judge 是否被采信全靠它，
而采信错了会让两个不可信的数进验收报告。所以 Provider 可注入，用假 judge 跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.experiments.run_judge import (
    AGREEMENT_FLOOR,
    KAPPA_FLOOR,
    run_calibration,
)
from backend.llm.types import LLMRequest, LLMResponse


class _EchoJudge:
    """一个「照着人工答案抄」的假 judge —— 用来验「完全一致」那一端。"""

    def __init__(self, verdicts: list[str], used: list[bool]) -> None:
        self._v = list(verdicts)
        self._u = list(used)

    def chat(self, request: LLMRequest) -> LLMResponse:
        schema = request.format_schema or {}
        if "verdict" in (schema.get("properties") or {}):
            return LLMResponse(text=json.dumps({"verdict": self._v.pop(0)}))
        return LLMResponse(text=json.dumps({"used": self._u.pop(0)}))


def _gold() -> tuple[list[str], list[bool]]:
    """按 `judge_calib_50` 的真实顺序取出人工标签。"""
    from backend.experiments.run_judge import CALIB

    verdicts: list[str] = []
    used: list[bool] = []
    for line in CALIB.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        verdicts += [c["verdict"] for c in item["claims"] if c.get("is_assertive")]
        used += [bool(u["used"]) for u in (item.get("context_usage") or [])]
    return verdicts, used


def test_thresholds_are_the_ones_the_spec_names() -> None:
    """§12.4.1 的采信门槛，**不许放宽**。"""
    assert (AGREEMENT_FLOOR, KAPPA_FLOOR) == (0.85, 0.70)


def test_perfect_judge_passes_with_kappa_one(tmp_path: Path) -> None:
    """judge 与人工完全一致 → 一致率 1.0、Kappa 1.0、通过。"""
    verdicts, used = _gold()
    summary = run_calibration(tmp_path / "calib.jsonl", _EchoJudge(verdicts, used))
    assert summary["agreement"]["point"] == pytest.approx(1.0)
    assert summary["kappa"] == pytest.approx(1.0)
    assert summary["passed"] is True
    assert summary["n_claims_scored"] == len(verdicts)


def test_high_agreement_can_still_fail_kappa(tmp_path: Path) -> None:
    """★ M9-A §3.9.4 那笔算术的可执行版本。

    标签 88.4% 压在 SUPPORTED（`p_e ≈ 0.792`），judge 只要把少数类全判成
    SUPPORTED，**一致率仍有 ~88%（过 85% 的闸），Kappa 却是 0**。

    这条钉住的是「未过门槛可能是分布偏斜的结果，而不是 judge 太差」——
    也钉住「一致率 ≥85%」这道闸本身拦不住一个只会说 SUPPORTED 的 judge。
    """
    verdicts, used = _gold()
    lazy = ["SUPPORTED"] * len(verdicts)
    summary = run_calibration(tmp_path / "calib2.jsonl", _EchoJudge(lazy, used))
    assert summary["agreement"]["point"] > AGREEMENT_FLOOR, "一致率过得了闸"
    assert summary["kappa"] == pytest.approx(0.0, abs=1e-9), "Kappa 却是 0"
    assert summary["passed"] is False, "两条闸是且的关系，Kappa 才是真正起作用的那道"


def test_minority_recall_exposes_the_lazy_judge(tmp_path: Path) -> None:
    """三个数一起报，第三个才说得清 judge 是「整体不准」还是「只抓不住少数类」。"""
    verdicts, used = _gold()
    summary = run_calibration(
        tmp_path / "calib3.jsonl", _EchoJudge(["SUPPORTED"] * len(verdicts), used)
    )
    recall = summary["minority_recall"]
    assert recall["SUPPORTED"]["point"] == pytest.approx(1.0)
    assert recall["PARTIAL"]["point"] == pytest.approx(0.0)
    assert recall["NOT_SUPPORTED"]["point"] == pytest.approx(0.0)


def test_unparseable_verdicts_are_excluded_not_counted_as_wrong(tmp_path: Path) -> None:
    """解析失败的条目排除出分母 —— 记成分歧会让一致率不再是「judge 与人的一致率」。"""

    class _Broken:
        def chat(self, request: LLMRequest) -> LLMResponse:
            schema = request.format_schema or {}
            if "verdict" in (schema.get("properties") or {}):
                return LLMResponse(text="不是 JSON")
            return LLMResponse(text=json.dumps({"used": True}))

    summary = run_calibration(tmp_path / "calib4.jsonl", _Broken())
    assert summary["n_claims_scored"] == 0
    assert summary["n_claims_unparsed"] > 0


def test_calibration_writes_every_judgement_to_disk(tmp_path: Path) -> None:
    """逐条落盘 —— 报告里若要解释某个分歧，得能翻回原始判定。"""
    verdicts, used = _gold()
    out = tmp_path / "calib5.jsonl"
    run_calibration(out, _EchoJudge(verdicts, used))
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == len(verdicts) + len(used)
    assert {r["kind"] for r in rows} == {"claim", "usage"}
