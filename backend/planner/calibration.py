"""置信度校准（v6 §7.3.5，业务方 2026-08-13 裁定 `Z-11`）。

小模型的自评置信度基本未经校准。v6 改用两类信号：

| # | 信号 | 来源 | 代价 |
|---|---|---|---|
| 一 | **self-consistency** —— 采样 n 次看结构化意图是否一致 | 本模块 :func:`consistency_ratio` | +2 次调用，只在首轮意图解析这类高风险低频节点启用 |
| 二~四 | **Harness 侧行为特征** —— `first_pass` / `retries` / `worst_failure_mode` | `AgentOutput.calibration_features()`（M4-A 已现成） | **0**，Harness 本来就在记 |

> 原方案的信号一是「序列 logprob」。M4-A 实测本机 Ollama v0.6.8 **不返回任何
> logprob 字段**，而升级推理端会踩 M0 记过的 CUDA 版本坑（v0.30+ 的运行时是
> 12.8、本机驱动是 12.2，会静默退化成 CPU 推理）。业务方裁定不为一个特征动
> 驱动或换推理端。

## 本窗口交付的是框架，不是拟合好的曲线

**校准曲线要在 v6 §12.2 的 360 条标注数据上拟合，那是 W11 才有的东西。**
所以这里给的是：特征定义、逻辑回归的训练与推理路径、序列化格式、以及一个
**未拟合期的保守回退**。三件事必须分清楚：

- `fitted=False` 的校准器**不假装自己拟合过**——`predict()` 走的是一条明确
  标记为「启发式」的回退公式，`ConfidenceCalibrator.fitted` 与序列化里的
  `n_samples=0` 都如实写着（铁律 6）。
- 阈值 `CONFIDENCE_THRESHOLD` 是**配置项**（`FTS_CONFIDENCE_THRESHOLD`，默认
  0.75），不是常量。v6 §7.3.5 要求它由「误执行率 ≤4%」在 360 条数据上反推，
  W11/W13 拿到数据后替换默认值即可，代码一行不用改。
- 训练是**确定性**的：零初始化 + 全批梯度下降 + 固定轮数，没有随机打散、没有
  随机初始化。同一份数据两次拟合出的系数逐位相等（铁律 9 的同一条要求）。

## 特征向量的形状

`[agreement, first_pass, retries_norm, is_hallucination, is_malformed]`

`is_hallucination` 单独占一维，是因为它是 v6 §12.5.1 的**硬地板**：模型把
「何超」当成 `person_id` 填进去时，它对自己填的东西一无所知，这类失败重试多少
轮都救不回来，**恰恰是最该压低置信度的那一类**。把它并进 `retries` 会让「重试
两次后靠回灌纠正过来」与「两次都在编实体」得到同一个分数。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from backend.harness.types import FailureMode

#: 特征名。**顺序即向量顺序**，改了要同步改序列化格式的 `feature_names`。
FEATURE_NAMES: Final[tuple[str, ...]] = (
    "agreement",
    "first_pass",
    "retries_norm",
    "is_hallucination",
    "is_malformed",
)

#: `retries` 的归一化分母（Harness 的重试上限是 2，v6 §7.7.1）。
_MAX_RETRIES: Final[float] = 2.0

#: 序列化格式版本。改了字段含义就要 +1，否则旧文件会被按新语义读。
CALIBRATOR_FORMAT_VERSION: Final[int] = 1


@dataclass(frozen=True)
class CalibrationFeatures:
    """一次调用的校准特征。四项全部**免费**——Harness 本来就在记。"""

    agreement: float = 1.0
    first_pass: bool = True
    retries: int = 0
    worst_failure_mode: str = ""

    @classmethod
    def from_output(
        cls, features: dict[str, Any], *, agreement: float = 1.0
    ) -> CalibrationFeatures:
        """从 `AgentOutput.calibration_features()` 的返回构造。"""
        return cls(
            agreement=float(agreement),
            first_pass=bool(features.get("first_pass", False)),
            retries=int(features.get("retries", 0)),
            worst_failure_mode=str(features.get("worst_failure_mode", "")),
        )

    def vector(self) -> list[float]:
        return [
            _clamp(self.agreement),
            1.0 if self.first_pass else 0.0,
            min(self.retries / _MAX_RETRIES, 1.0),
            1.0 if self.worst_failure_mode == FailureMode.ENTITY_HALLUCINATION.value else 0.0,
            1.0 if self.worst_failure_mode == FailureMode.JSON_MALFORMED.value else 0.0,
        ]


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _sigmoid(z: float) -> float:
    # 溢出防护：exp(710) 就 inf 了，而 z 在训练早期完全可能跑到那个量级
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 60.0)))
    return math.exp(max(z, -60.0)) / (1.0 + math.exp(max(z, -60.0)))


def consistency_ratio(values: Sequence[str]) -> float:
    """self-consistency 一致率：众数占比。

    空序列返回 0.0 —— 一次都没采到样，**不是「完全一致」**。这个默认值写反过
    很危险：采样全部失败时会得到 1.0，于是「模型什么都没吐」被记成「模型非常
    确定」。
    """
    if not values:
        return 0.0
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return max(counts.values()) / len(values)


@dataclass(frozen=True)
class ConfidenceCalibrator:
    """逻辑回归校准器（v6 §7.3.5 的 `CALIBRATOR`）。"""

    coefficients: tuple[float, ...] = (0.0,) * len(FEATURE_NAMES)
    intercept: float = 0.0
    n_samples: int = 0
    fitted: bool = False
    feature_names: tuple[str, ...] = FEATURE_NAMES
    #: 拟合所用数据集的标识，写进 manifest 便于回溯（未拟合时为空）
    dataset: str = ""

    # ── 推理 ─────────────────────────────────────────────────────────
    def predict(self, features: CalibrationFeatures) -> float:
        """给出校准后的置信度。"""
        if not self.fitted:
            return heuristic_confidence(features)
        z = self.intercept + sum(
            c * x for c, x in zip(self.coefficients, features.vector(), strict=True)
        )
        return _clamp(_sigmoid(z))

    # ── 训练 ─────────────────────────────────────────────────────────
    @classmethod
    def fit(
        cls,
        samples: Sequence[tuple[CalibrationFeatures, bool]],
        *,
        learning_rate: float = 0.5,
        iterations: int = 2000,
        l2: float = 1e-3,
        dataset: str = "",
    ) -> ConfidenceCalibrator:
        """全批梯度下降，零初始化，固定轮数 —— **确定性**。

        不用 sklearn 有两个具体理由：① `LogisticRegression` 的 lbfgs 解在不同
        BLAS 后端上会有末位差异，而校准器系数要进 manifest 参与可复现性；
        ② 序列化格式要自己定死（跨版本能读），pickle 一个 sklearn 对象做不到。
        """
        if not samples:
            raise ValueError("拟合校准器需要至少一条样本 —— 空数据集不产出校准器，也不假装产出")
        labels = {label for _, label in samples}
        if len(labels) < 2:
            raise ValueError(
                f"样本标签只有一类（{labels}），逻辑回归无从区分。"
                "校准数据必须同时含正确与错误的例子（v6 §12.2 的 360 条即为此）"
            )

        xs = [f.vector() for f, _ in samples]
        ys = [1.0 if label else 0.0 for _, label in samples]
        dim = len(FEATURE_NAMES)
        weights = [0.0] * dim
        bias = 0.0
        n = float(len(samples))

        for _ in range(iterations):
            grad_w = [0.0] * dim
            grad_b = 0.0
            for x, y in zip(xs, ys, strict=True):
                error = _sigmoid(bias + sum(w * xi for w, xi in zip(weights, x, strict=True))) - y
                for j in range(dim):
                    grad_w[j] += error * x[j]
                grad_b += error
            for j in range(dim):
                weights[j] -= learning_rate * (grad_w[j] / n + l2 * weights[j])
            bias -= learning_rate * (grad_b / n)

        return cls(
            coefficients=tuple(weights),
            intercept=bias,
            n_samples=len(samples),
            fitted=True,
            dataset=dataset,
        )

    # ── 序列化 ───────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": CALIBRATOR_FORMAT_VERSION,
            "fitted": self.fitted,
            "n_samples": self.n_samples,
            "dataset": self.dataset,
            "feature_names": list(self.feature_names),
            "coefficients": list(self.coefficients),
            "intercept": self.intercept,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ConfidenceCalibrator:
        version = int(payload.get("format_version", 0))
        if version != CALIBRATOR_FORMAT_VERSION:
            raise ValueError(
                f"校准器格式版本不匹配：文件是 v{version}，本代码认 "
                f"v{CALIBRATOR_FORMAT_VERSION}。不做静默兼容 —— 按旧语义读新文件"
                "会得到一条看起来正常、实际错位的校准曲线"
            )
        names = tuple(payload["feature_names"])
        if names != FEATURE_NAMES:
            raise ValueError(f"特征集不匹配：文件是 {names}，本代码是 {FEATURE_NAMES}")
        return cls(
            coefficients=tuple(float(c) for c in payload["coefficients"]),
            intercept=float(payload["intercept"]),
            n_samples=int(payload.get("n_samples", 0)),
            fitted=bool(payload.get("fitted", False)),
            feature_names=names,
            dataset=str(payload.get("dataset", "")),
        )

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path) -> ConfidenceCalibrator:
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def heuristic_confidence(features: CalibrationFeatures) -> float:
    """**未拟合期**的保守回退，明确标记为启发式而非校准结果。

    公式：以一致率打底，按「这次输出有多顺」逐项扣分。三档扣分的相对大小取自
    v6 §12.5.1 对失败模式的严重度判断（`entity_hallucination` 是硬地板），
    **绝对值没有数据支撑，所以它不叫校准，叫回退**。W11 拿到 360 条后
    `ConfidenceCalibrator.fit()` 一跑，这个函数就退出主路径。
    """
    score = _clamp(features.agreement)
    if not features.first_pass:
        score -= 0.10
    score -= 0.05 * min(features.retries, int(_MAX_RETRIES))
    if features.worst_failure_mode == FailureMode.ENTITY_HALLUCINATION.value:
        score -= 0.35
    elif features.worst_failure_mode == FailureMode.JSON_MALFORMED.value:
        score -= 0.15
    elif features.worst_failure_mode:
        score -= 0.05
    return _clamp(score)


@dataclass(frozen=True)
class ReliabilityBin:
    """可靠性图的一个分箱（v6 §7.3.5「输出可靠性图与 ECE」）。"""

    lower: float
    upper: float
    count: int
    mean_confidence: float
    accuracy: float


def reliability_bins(
    predictions: Sequence[tuple[float, bool]], *, bins: int = 10
) -> list[ReliabilityBin]:
    """把 (置信度, 是否正确) 分箱。空箱**不返回**——画出来是条假的零线。"""
    if bins <= 0:
        raise ValueError("分箱数必须为正")
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for confidence, correct in predictions:
        index = min(int(_clamp(confidence) * bins), bins - 1)
        buckets[index].append((confidence, correct))
    out: list[ReliabilityBin] = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        out.append(
            ReliabilityBin(
                lower=index / bins,
                upper=(index + 1) / bins,
                count=len(bucket),
                mean_confidence=sum(c for c, _ in bucket) / len(bucket),
                accuracy=sum(1 for _, ok in bucket if ok) / len(bucket),
            )
        )
    return out


def expected_calibration_error(
    predictions: Sequence[tuple[float, bool]], *, bins: int = 10
) -> float:
    """ECE（期望校准误差）。样本为空时抛，不返回 0.0。

    返回 0.0 的话，「一条数据都没有」会显示成「校准完美」——这正是铁律 6 说的
    「不报告未实际计算的指标」。
    """
    if not predictions:
        raise ValueError("没有预测样本，ECE 无从计算 —— 不返回 0.0 冒充完美校准")
    total = len(predictions)
    return sum(
        b.count / total * abs(b.accuracy - b.mean_confidence)
        for b in reliability_bins(predictions, bins=bins)
    )


def brier_score(predictions: Sequence[tuple[float, bool]]) -> float:
    """Brier 分数（越低越好）。同样拒绝空输入。"""
    if not predictions:
        raise ValueError("没有预测样本，Brier 分数无从计算")
    return sum((c - (1.0 if ok else 0.0)) ** 2 for c, ok in predictions) / len(predictions)


#: 进程级默认校准器：**未拟合**。W11 拿到 360 条后由装配层 load 一个拟合过的替换。
DEFAULT_CALIBRATOR: Final[ConfidenceCalibrator] = ConfidenceCalibrator()


__all__ = [
    "CALIBRATOR_FORMAT_VERSION",
    "DEFAULT_CALIBRATOR",
    "FEATURE_NAMES",
    "CalibrationFeatures",
    "ConfidenceCalibrator",
    "ReliabilityBin",
    "brier_score",
    "consistency_ratio",
    "expected_calibration_error",
    "heuristic_confidence",
    "reliability_bins",
]
