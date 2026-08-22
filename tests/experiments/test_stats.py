"""`backend/experiments/stats.py` 的单测。

重点盯三件事：**边界处不退化**（p=1 时 Wilson 仍给非退化区间）、
**偏斜分布下 Kappa 远严于一致率**（这是 `judge_calib_50` 会不会过门槛的关键，
M9-A §3.9.4 把这笔账算过）、以及**空样本一律拒绝而不是返回 0**。
"""

from __future__ import annotations

import math

import pytest

from backend.experiments.stats import (
    agreement_rate,
    class_recall,
    cohen_kappa,
    kappa_bootstrap_ci,
    weighted_rate,
    wilson_interval,
)


def test_wilson_at_one_hundred_percent_is_not_degenerate() -> None:
    """p=1 时区间不能是「100% ± 0%」。

    本项目好几个指标会贴到 100%（越权拦截、格式校验、无解判定）。
    Wald 在那里给出宽度 0 的区间，是个假的确定性陈述 —— 换 Wilson 正为此。
    """
    interval = wilson_interval(200, 200)
    assert interval.point == 1.0
    assert interval.low < 1.0, "上界可以是 1，下界不能也是 1"
    assert interval.high == pytest.approx(1.0)


def test_wilson_reproduces_m7_first_pass_rate() -> None:
    """M7 实测 597/600 = 99.50%，区间要盖住它。"""
    interval = wilson_interval(597, 600)
    assert interval.point == pytest.approx(0.995, abs=1e-6)
    assert interval.low < 0.995 < interval.high


def test_wilson_rejects_empty_sample() -> None:
    """空样本不产出区间，也不假装产出（铁律 6）。"""
    with pytest.raises(ValueError, match="样本量必须为正"):
        wilson_interval(0, 0)


def test_wilson_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError, match="不在"):
        wilson_interval(5, 3)


def test_kappa_is_far_stricter_than_agreement_on_skewed_labels() -> None:
    """M9-A §3.9.4 的算术：标签 88.4% 压在 SUPPORTED 时，
    一致率 ~94% 才换得到 Kappa 0.70 左右。

    这条钉住的是**报告口径**：judge 若未过门槛，很可能是这条算术的结果，
    而不是 judge 太差。
    """
    truth = ["S"] * 137 + ["P"] * 16 + ["N"] * 2
    pred = list(truth)
    for i in range(137, 146):  # 错 9 条少数类
        pred[i] = "S"
    agree = agreement_rate(truth, pred).point
    kappa = cohen_kappa(truth, pred)
    assert agree > 0.94, f"一致率 {agree}"
    assert kappa < 0.80, "偏斜分布下高一致率也换不到很高的 Kappa"
    assert kappa > 0.0


def test_kappa_is_one_when_identical() -> None:
    a = ["S", "P", "N", "S"]
    assert cohen_kappa(a, list(a)) == pytest.approx(1.0)


def test_kappa_is_zero_when_both_sides_use_one_label() -> None:
    """两侧都只用一个标签时 Kappa 无定义 —— 返回 0.0 而不是 1.0。

    调用方必须同时报一致率，读者据此看得出发生了什么。
    """
    assert cohen_kappa(["S"] * 10, ["S"] * 10) == 0.0


def test_kappa_bootstrap_ci_is_deterministic() -> None:
    """同一份输入两次调用逐位相同 —— 这个区间要进验收报告（铁律 9）。"""
    a = ["S"] * 30 + ["P"] * 12 + ["N"] * 8
    b = list(a)
    b[0], b[35] = "P", "S"
    first = kappa_bootstrap_ci(a, b, resamples=200)
    second = kappa_bootstrap_ci(a, b, resamples=200)
    assert first == second
    assert first.low <= first.point <= first.high


def test_class_recall_reports_nan_for_absent_class() -> None:
    """某一类一条真值都没有时给 NaN —— 给 0 会被读成「一条都没召回」。"""
    out = class_recall(["S", "S"], ["S", "S"], "NOT_SUPPORTED")
    assert math.isnan(out.point)
    assert out.n == 0


def test_class_recall_counts_only_that_class() -> None:
    truth = ["S", "P", "P", "N"]
    pred = ["S", "P", "S", "N"]
    assert class_recall(truth, pred, "P").point == pytest.approx(0.5)


def test_weighted_rate_reproduces_v6_worked_example() -> None:
    """v6 §12.4 那笔账：(120×98 + 120×91 + 80×88) / 320 = 92.9%。"""
    assert weighted_rate([(120, 0.98), (120, 0.91), (80, 0.88)]) == pytest.approx(0.9288, abs=1e-4)


def test_weighted_rate_rejects_zero_total() -> None:
    with pytest.raises(ValueError, match="条数合计为 0"):
        weighted_rate([(0, 0.9)])


def test_agreement_rate_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError, match="长度不等"):
        agreement_rate(["S"], ["S", "P"])
