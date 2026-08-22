"""验收报告要用的统计量：Wilson 区间、Cohen's Kappa、加权召回。

**为什么自己写而不是拉 scipy/statsmodels**：与
`planner/calibration.py` 的理由相同 —— 这些数要进验收报告并参与可复现性，
第三方求解器在不同 BLAS 后端上会有末位差异。这里的实现全部是闭式或
固定轮数的确定性算术，同一份输入逐位可复现（铁律 9）。

`mcnemar_exact` 不在这里 —— 它已由 M9-A 落在
`backend/datasets/ood_judge.py`，本模块不重复实现。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

#: 95% 双侧正态分位数。验收报告统一用它，不逐处写魔数。
Z95: float = 1.959963984540054


@dataclass(frozen=True)
class Interval:
    """一个比例的点估计与置信区间。"""

    point: float
    low: float
    high: float
    n: int

    def pct(self, digits: int = 2) -> str:
        """报告里统一的呈现形态：`点估计% [下界%, 上界%]`。"""
        return (
            f"{self.point * 100:.{digits}f}% "
            f"[{self.low * 100:.{digits}f}%, {self.high * 100:.{digits}f}%]"
        )


def wilson_interval(successes: int, n: int, *, z: float = Z95) -> Interval:
    """Wilson score 区间（v6 §12.2 点名要的那个）。

    **为什么不是 Wald（正态近似）区间**：本项目好几个指标会贴到 100%
    （越权拦截、格式校验、无解判定）。Wald 在 p=1 时给出宽度为 0 的区间
    ——「100% ± 0%」是个假的确定性陈述，样本只有 200 条时尤其误导。
    Wilson 在边界处仍给出非退化区间，这正是验收报告需要的诚实形态。
    """
    if n <= 0:
        raise ValueError("样本量必须为正 —— 空样本不产出区间，也不假装产出")
    if not 0 <= successes <= n:
        raise ValueError(f"成功数 {successes} 不在 [0, {n}] 内")

    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return Interval(point=p, low=max(0.0, center - margin), high=min(1.0, center + margin), n=n)


def agreement_rate(a: Sequence[str], b: Sequence[str]) -> Interval:
    """两个标注序列的逐格一致率，带 Wilson 区间。"""
    if len(a) != len(b):
        raise ValueError(f"两侧长度不等（{len(a)} vs {len(b)}），无法逐格比对")
    hits = sum(1 for x, y in zip(a, b, strict=True) if x == y)
    return wilson_interval(hits, len(a))


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float:
    """Cohen's Kappa。

    ⚠️ **在偏斜分布下 Kappa 远严于一致率**，这不是缺陷而是它的定义。
    `judge_calib_50` 的标签 88.4% 集中在 `SUPPORTED`，偶然一致率
    `p_e ≈ 0.792`，于是「Kappa ≥0.70」实际要求一致率 ≥93.8%
    （M9-A §3.9.4 已把这笔账算过一遍）。报数时要三个数一起给：
    一致率、Kappa、以及少数类各自的召回率 —— 只有第三个数说得清
    judge 是「整体不准」还是「只是抓不住少数类」。
    """
    if len(a) != len(b):
        raise ValueError(f"两侧长度不等（{len(a)} vs {len(b)}），无法算 Kappa")
    n = len(a)
    if n == 0:
        raise ValueError("空样本不产出 Kappa")

    p_o = sum(1 for x, y in zip(a, b, strict=True) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    p_e = sum(ca[k] / n * cb[k] / n for k in set(ca) | set(cb))
    if math.isclose(p_e, 1.0):
        # 两侧都只用了同一个标签：Kappa 无定义。返回 0.0 并不掩盖这件事 ——
        # 调用方必须同时报一致率，读者据此看得出发生了什么。
        return 0.0
    return (p_o - p_e) / (1 - p_e)


def kappa_bootstrap_ci(
    a: Sequence[str],
    b: Sequence[str],
    *,
    resamples: int = 2000,
    seed: int = 42,
    z: float = Z95,
) -> Interval:
    """Kappa 的自助百分位区间。

    用固定 seed 的线性同余发生器而不是 `random` 模块：后者的实现细节
    跨 Python 版本不保证一致，而这个区间要进验收报告（铁律 9）。
    """
    del z  # 百分位法不用正态分位数，保留形参是为了与其余入口同形
    n = len(a)
    if n == 0:
        raise ValueError("空样本不产出 Kappa 区间")
    point = cohen_kappa(a, b)

    state = seed & 0xFFFFFFFF
    draws: list[float] = []
    for _ in range(resamples):
        sa: list[str] = []
        sb: list[str] = []
        for _ in range(n):
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            idx = state % n
            sa.append(a[idx])
            sb.append(b[idx])
        try:
            draws.append(cohen_kappa(sa, sb))
        except ValueError:  # pragma: no cover —— n>0 时不会发生
            continue
    draws.sort()
    lo = draws[int(0.025 * len(draws))]
    hi = draws[min(len(draws) - 1, int(0.975 * len(draws)))]
    return Interval(point=point, low=lo, high=hi, n=n)


def class_recall(truth: Sequence[str], pred: Sequence[str], label: str) -> Interval:
    """某一类的召回率（该类真值里被判对的比例），带 Wilson 区间。

    M9-A §3.9.4 点名要报的「少数类各自的召回率」就是它。
    """
    idx = [i for i, t in enumerate(truth) if t == label]
    if not idx:
        return Interval(point=float("nan"), low=float("nan"), high=float("nan"), n=0)
    hits = sum(1 for i in idx if pred[i] == label)
    return wilson_interval(hits, len(idx))


def weighted_rate(parts: Sequence[tuple[int, float]]) -> float:
    """按条数加权的总体率：`Σ(nᵢ×rᵢ) / Σnᵢ`。

    v6 §12.4 那笔「(120×语义 + 120×情景 + 80×程序) / 320」的账。
    """
    total = sum(n for n, _ in parts)
    if total == 0:
        raise ValueError("加权项条数合计为 0")
    return sum(n * r for n, r in parts) / total


__all__ = [
    "Z95",
    "Interval",
    "agreement_rate",
    "class_recall",
    "cohen_kappa",
    "kappa_bootstrap_ci",
    "weighted_rate",
    "wilson_interval",
]
