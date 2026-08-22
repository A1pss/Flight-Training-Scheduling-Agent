"""实验一的指标聚合：`python -m backend.experiments.report_nl`。

**这里一个 LLM 调用都不发。** 全部指标由 `exp1_nl360.jsonl` 的原始观测算出来，
包括阈值扫描与两条消融 —— 这正是 `nl_eval` 那套「先记观测、后判动作」的收益。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from backend.experiments.nl_eval import (
    EXECUTING_ACTIONS,
    SLOT_KINDS,
    SlotCounts,
    action_at_threshold,
    iter_jsonl,
    slot_counts,
)
from backend.experiments.stats import Interval, wilson_interval
from backend.planner.calibration import (
    CalibrationFeatures,
    ConfidenceCalibrator,
    expected_calibration_error,
    reliability_bins,
)


def _rows(path: Path, variant: str) -> list[dict[str, Any]]:
    return [r for r in iter_jsonl(path) if r.get("variant", "main") == variant]


def completion(rows: Sequence[Mapping[str, Any]], threshold: float) -> Interval:
    """端到端任务完成率 —— **「正确地反问澄清」计为成功**。

    实现上不需要为反问开小灶：期望动作就是 `ask_clarify` 的样本，
    系统也给出 `ask_clarify` 时自然相等。这条规则的全部含义是
    「槽位抽错但系统识别出自己不确定并转而追问」在主指标上记对，
    而那正是 `action_at_threshold` 会给出的结果。
    """
    hits = sum(1 for r in rows if action_at_threshold(r, threshold) == r["expected_action"])
    return wilson_interval(hits, len(rows))


def intent_accuracy(rows: Sequence[Mapping[str, Any]]) -> Interval:
    hits = sum(1 for r in rows if r["observed_intent"] == r["expected_intent"])
    return wilson_interval(hits, len(rows))


def misexecution(
    rows: Sequence[Mapping[str, Any]], threshold: float
) -> tuple[Interval, Interval, int]:
    """误执行率 —— **两个分母都报，因为 v6 没有把话说死**。

    §12.2 的原文是「意图不明却直接执行的比例」。「比例」的分母有两种读法：

    | 口径 | 分母 | 读法 |
    |---|---|---|
    | **A 全量** | 360 | 「每 100 个请求里有几个被误执行」 |
    | **B 该反问的那些** | 期望 `ask_clarify` 的条数 | 「该反问的场合里有几成没反问」 |

    两者差着 `360 / 62 ≈ 5.8` 倍，**选哪个会改变达标与否**，所以两个都算、
    都写进报告，由业务方裁定哪个是验收口径（`CLAUDE.md` §7 第 2 条）。
    分子是同一个：**期望反问、实际却执行了**。
    """
    unclear = [r for r in rows if r["expected_action"] == "ask_clarify"]
    bad = sum(1 for r in unclear if action_at_threshold(r, threshold) in EXECUTING_ACTIONS)
    # 分层子集里可能一条「该反问」的样本都没有（跑到一半时常见）。
    # 那时口径 B 无定义 —— 给 NaN，**不给 0**：0 会被读成「一条都没误执行」。
    over_unclear = (
        wilson_interval(bad, len(unclear))
        if unclear
        else Interval(point=float("nan"), low=float("nan"), high=float("nan"), n=0)
    )
    return wilson_interval(bad, len(rows)), over_unclear, bad


def slot_f1(rows: Sequence[Mapping[str, Any]]) -> tuple[SlotCounts, dict[str, SlotCounts]]:
    """五类槽位的微平均 F1，以及逐类明细。"""
    per: dict[str, SlotCounts] = {}
    total = SlotCounts()
    for kind in SLOT_KINDS:
        acc = SlotCounts()
        for r in rows:
            acc = acc.merged(slot_counts(r, kind))
        per[kind] = acc
        total = total.merged(acc)
    return total, per


def threshold_sweep(
    rows: Sequence[Mapping[str, Any]], *, target_misexec: float = 0.04
) -> list[dict[str, Any]]:
    """扫阈值，给出反推那一步的全过程。

    §12.2：**反问阈值由「误执行率 ≤4%」反推确定**。所以这张表是要写进报告的
    ——只报一个最终阈值，读者无从判断它是怎么来的。
    """
    out: list[dict[str, Any]] = []
    for i in range(0, 101, 5):
        t = i / 100.0
        over_all, over_unclear, bad = misexecution(rows, t)
        comp = completion(rows, t)
        out.append(
            {
                "threshold": t,
                "completion": comp.point,
                "misexec_over_all": over_all.point,
                "misexec_over_unclear": over_unclear.point,
                "misexec_count": bad,
                "meets_over_all": over_all.point <= target_misexec,
                "meets_over_unclear": over_unclear.point <= target_misexec,
            }
        )
    return out


def fit_calibrator(rows: Sequence[Mapping[str, Any]]) -> tuple[ConfidenceCalibrator, float, Any]:
    """在这 360 条上拟合 §7.3.5 的校准器，并算 ECE 与可靠性图。

    **标签取「意图分类对不对」**：校准器预测的就是这一次分类的可信程度，
    拿端到端动作当标签会把 Planner 的行为混进来。

    ⚠️ **只用走了 LLM 的那部分拟合**：规则命中路径没有校准特征
    （`confidence=1.0` 是确定性事实，不归阈值管辖，见 `below_threshold`），
    把它们按 1.0 塞进去会人为拉高一致率并压低 ECE。
    """
    samples: list[tuple[CalibrationFeatures, bool]] = []
    for r in rows:
        feats = r.get("calibration_features") or {}
        if not feats:
            continue
        samples.append(
            (
                CalibrationFeatures(
                    agreement=float(feats.get("agreement", 1.0)),
                    first_pass=bool(feats.get("first_pass", True)),
                    retries=int(feats.get("retries", 0)),
                    worst_failure_mode=str(feats.get("worst_failure_mode", "")),
                ),
                bool(r["observed_intent"] == r["expected_intent"]),
            )
        )
    if not samples:
        raise ValueError("没有任何带校准特征的样本 —— 是不是跑的是旧格式的观测？")

    calibrator = ConfidenceCalibrator.fit(samples, dataset="nl_360")
    preds = [(calibrator.predict(f), label) for f, label in samples]
    return calibrator, expected_calibration_error(preds), reliability_bins(preds)


def summarize(path: Path, variant: str, threshold: float) -> dict[str, Any]:
    rows = _rows(path, variant)
    if not rows:
        raise ValueError(f"{path} 里没有 variant={variant} 的观测")

    by_round: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_round[int(r["round_index"])].append(r)

    per_round = {
        rnd: {
            "n": len(rs),
            "completion": completion(rs, threshold).point,
            "intent": intent_accuracy(rs).point,
            "misexec_over_all": misexecution(rs, threshold)[0].point,
        }
        for rnd, rs in sorted(by_round.items())
    }
    total_slots, per_slot = slot_f1(rows)
    over_all, over_unclear, bad = misexecution(rows, threshold)
    errors = [r for r in rows if r.get("error")]

    return {
        "variant": variant,
        "threshold": threshold,
        "n_observations": len(rows),
        "n_rounds": len(by_round),
        "per_round": per_round,
        "completion": completion(rows, threshold).__dict__,
        "intent_accuracy": intent_accuracy(rows).__dict__,
        "slot_f1_micro": {
            "f1": total_slots.f1,
            "precision": total_slots.precision,
            "recall": total_slots.recall,
            "tp": total_slots.tp,
            "fp": total_slots.fp,
            "fn": total_slots.fn,
        },
        "slot_per_kind": {
            k: {"f1": v.f1, "tp": v.tp, "fp": v.fp, "fn": v.fn} for k, v in per_slot.items()
        },
        "misexecution": {
            "count": bad,
            "over_all": over_all.__dict__,
            "over_unclear": over_unclear.__dict__,
        },
        "llm_calls_total": sum(int(r.get("llm_calls", 0)) for r in rows),
        "rule_hit_share": sum(1 for r in rows if r["source"] == "rule") / len(rows),
        "errors": len(errors),
        "error_samples": [r["item_id"] for r in errors[:10]],
        "confusion_action": dict(
            Counter(
                f"{r['expected_action']}→{action_at_threshold(r, threshold)}"
                for r in rows
                if action_at_threshold(r, threshold) != r["expected_action"]
            ).most_common(12)
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="实验一指标聚合")
    parser.add_argument("--path", default="reports/m9b/exp1_nl360.jsonl")
    parser.add_argument("--variant", default="main")
    parser.add_argument("--threshold", type=float, default=0.75)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    path = Path(args.path)
    summary = summarize(path, args.variant, args.threshold)
    rows = _rows(path, args.variant)
    summary["threshold_sweep"] = threshold_sweep(rows)
    try:
        calibrator, ece, bins = fit_calibrator(rows)
        summary["calibration"] = {
            "ece": ece,
            "n_fit_samples": calibrator.n_samples,
            "coefficients": dict(
                zip(calibrator.feature_names, calibrator.coefficients, strict=True)
            ),
            "intercept": calibrator.intercept,
            "reliability_bins": [
                {
                    "lower": b.lower,
                    "upper": b.upper,
                    "n": b.count,
                    "mean_confidence": b.mean_confidence,
                    "accuracy": b.accuracy,
                }
                for b in bins
            ],
        }
    except ValueError as exc:
        summary["calibration"] = {"error": str(exc)}

    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"已写入 {args.out}")
    print(text[:3000])
    return 0


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    sys.exit(main())
