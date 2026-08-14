"""置信度校准框架（v6 §7.3.5，`Z-11`）。

**本窗口交付的是框架，不是拟合好的曲线。** 所以这里验的是：接口能跑通、训练
确定、序列化能往返、以及**未拟合期不假装拟合过**。真正的曲线要在 W11 的 360 条
标注数据上拟合。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.harness.types import FailureMode
from backend.planner.calibration import (
    CALIBRATOR_FORMAT_VERSION,
    DEFAULT_CALIBRATOR,
    FEATURE_NAMES,
    CalibrationFeatures,
    ConfidenceCalibrator,
    brier_score,
    consistency_ratio,
    expected_calibration_error,
    heuristic_confidence,
    reliability_bins,
)


def test_consistency_ratio_is_the_mode_share() -> None:
    assert consistency_ratio(["a", "a", "a"]) == 1.0
    assert consistency_ratio(["a", "a", "b"]) == pytest.approx(2 / 3)
    assert consistency_ratio(["a", "b", "c"]) == pytest.approx(1 / 3)


def test_empty_samples_are_zero_not_one() -> None:
    """采样全失败时返回 1.0 会把「什么都没吐」记成「非常确定」。"""
    assert consistency_ratio([]) == 0.0


def test_feature_vector_shape_and_order() -> None:
    features = CalibrationFeatures(
        agreement=1.0,
        first_pass=True,
        retries=0,
        worst_failure_mode="",
    )
    assert len(features.vector()) == len(FEATURE_NAMES)
    assert features.vector() == [1.0, 1.0, 0.0, 0.0, 0.0]


def test_hallucination_gets_its_own_dimension() -> None:
    """`entity_hallucination` 是 §12.5.1 的硬地板，不能并进 `retries`。"""
    hallucinated = CalibrationFeatures(
        agreement=1.0,
        first_pass=False,
        retries=2,
        worst_failure_mode=FailureMode.ENTITY_HALLUCINATION.value,
    )
    corrected = CalibrationFeatures(
        agreement=1.0,
        first_pass=False,
        retries=2,
        worst_failure_mode=FailureMode.MISSING_FIELD.value,
    )
    assert hallucinated.vector()[3] == 1.0
    assert corrected.vector()[3] == 0.0
    assert heuristic_confidence(hallucinated) < heuristic_confidence(corrected)


def test_features_from_harness_output_dict() -> None:
    payload = {
        "first_pass": False,
        "retries": 1,
        "worst_failure_mode": "type_error",
        "degraded": False,
        "llm_calls": 2,
    }
    features = CalibrationFeatures.from_output(payload, agreement=0.67)
    assert features.retries == 1
    assert features.agreement == pytest.approx(0.67)


# ─────────────────────────────────────────────────────────────────────
# 未拟合期
# ─────────────────────────────────────────────────────────────────────
def test_default_calibrator_is_honest_about_not_being_fitted() -> None:
    assert DEFAULT_CALIBRATOR.fitted is False
    assert DEFAULT_CALIBRATOR.n_samples == 0
    assert DEFAULT_CALIBRATOR.to_dict()["fitted"] is False


def test_unfitted_predict_uses_the_heuristic_fallback() -> None:
    features = CalibrationFeatures(agreement=0.8, first_pass=True, retries=0)
    assert DEFAULT_CALIBRATOR.predict(features) == pytest.approx(heuristic_confidence(features))


def test_heuristic_is_monotone_in_agreement() -> None:
    low = heuristic_confidence(CalibrationFeatures(agreement=0.34))
    high = heuristic_confidence(CalibrationFeatures(agreement=1.0))
    assert low < high


def test_heuristic_stays_in_range() -> None:
    worst = CalibrationFeatures(
        agreement=0.0,
        first_pass=False,
        retries=2,
        worst_failure_mode=FailureMode.ENTITY_HALLUCINATION.value,
    )
    assert 0.0 <= heuristic_confidence(worst) <= 1.0


# ─────────────────────────────────────────────────────────────────────
# 训练（小样本跑通训练与推理路径）
# ─────────────────────────────────────────────────────────────────────
def sample_set() -> list[tuple[CalibrationFeatures, bool]]:
    """一个刻意做得很小的训练集：高一致率 → 对，低一致率 + 幻觉 → 错。"""
    good = [
        (CalibrationFeatures(agreement=1.0, first_pass=True, retries=0), True) for _ in range(8)
    ]
    bad = [
        (
            CalibrationFeatures(
                agreement=0.34,
                first_pass=False,
                retries=2,
                worst_failure_mode=FailureMode.ENTITY_HALLUCINATION.value,
            ),
            False,
        )
        for _ in range(8)
    ]
    return good + bad


def test_fit_separates_the_two_groups() -> None:
    calibrator = ConfidenceCalibrator.fit(sample_set(), dataset="unit-smoke")
    assert calibrator.fitted and calibrator.n_samples == 16
    good = calibrator.predict(CalibrationFeatures(agreement=1.0, first_pass=True))
    bad = calibrator.predict(
        CalibrationFeatures(
            agreement=0.34,
            first_pass=False,
            retries=2,
            worst_failure_mode=FailureMode.ENTITY_HALLUCINATION.value,
        )
    )
    assert good > 0.5 > bad


def test_fit_is_deterministic() -> None:
    """同一份数据两次拟合，系数逐位相等（铁律 9 的同一条要求）。"""
    a = ConfidenceCalibrator.fit(sample_set())
    b = ConfidenceCalibrator.fit(sample_set())
    assert a.coefficients == b.coefficients
    assert a.intercept == b.intercept


def test_fit_refuses_empty_dataset() -> None:
    with pytest.raises(ValueError, match="至少一条样本"):
        ConfidenceCalibrator.fit([])


def test_fit_refuses_single_class_dataset() -> None:
    """全是正例的数据集拟合不出任何区分度 —— 抛，不产出一个假校准器。"""
    with pytest.raises(ValueError, match="只有一类"):
        ConfidenceCalibrator.fit([(CalibrationFeatures(), True)] * 5)


# ─────────────────────────────────────────────────────────────────────
# 序列化
# ─────────────────────────────────────────────────────────────────────
def test_round_trip(tmp_path: Path) -> None:
    calibrator = ConfidenceCalibrator.fit(sample_set(), dataset="unit-smoke")
    path = calibrator.save(tmp_path / "calibrator.json")
    restored = ConfidenceCalibrator.load(path)
    assert restored.coefficients == calibrator.coefficients
    assert restored.dataset == "unit-smoke"
    features = CalibrationFeatures(agreement=0.8)
    assert restored.predict(features) == pytest.approx(calibrator.predict(features))


def test_format_version_mismatch_is_rejected(tmp_path: Path) -> None:
    """不做静默兼容 —— 按旧语义读新文件会给出一条看似正常的错曲线。"""
    payload = ConfidenceCalibrator.fit(sample_set()).to_dict()
    payload["format_version"] = CALIBRATOR_FORMAT_VERSION + 1
    path = tmp_path / "c.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="格式版本不匹配"):
        ConfidenceCalibrator.load(path)


def test_feature_set_mismatch_is_rejected(tmp_path: Path) -> None:
    payload = ConfidenceCalibrator.fit(sample_set()).to_dict()
    payload["feature_names"] = ["agreement"]
    path = tmp_path / "c.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="特征集不匹配"):
        ConfidenceCalibrator.load(path)


# ─────────────────────────────────────────────────────────────────────
# 指标（v6 §7.3.5「输出可靠性图与 ECE」）
# ─────────────────────────────────────────────────────────────────────
def test_reliability_bins_skip_empty_buckets() -> None:
    bins = reliability_bins([(0.95, True), (0.92, True), (0.1, False)], bins=10)
    assert len(bins) == 2
    assert all(b.count > 0 for b in bins)


def test_ece_of_a_perfectly_calibrated_set_is_small() -> None:
    predictions = [(1.0, True)] * 10 + [(0.0, False)] * 10
    assert expected_calibration_error(predictions) == pytest.approx(0.0)


def test_ece_refuses_empty_input() -> None:
    """空输入返回 0.0 等于把「没数据」显示成「校准完美」（铁律 6）。"""
    with pytest.raises(ValueError, match=r"不返回 0\.0"):
        expected_calibration_error([])


def test_brier_refuses_empty_input() -> None:
    with pytest.raises(ValueError, match="无从计算"):
        brier_score([])


def test_brier_rewards_confident_correctness() -> None:
    assert brier_score([(1.0, True)]) == 0.0
    assert brier_score([(1.0, False)]) == 1.0
