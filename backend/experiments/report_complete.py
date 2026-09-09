"""实验一/五的完整结果、Wilson 区间、失败明细与配对对比。

本模块只消费 runner 已落盘的逐条 JSONL，不调用模型，也不改实验观测。
比例指标统一给 Wilson 95% 区间；F1、均值等非二项比例保留原统计量，避免
给不适用的量强套二项区间。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from backend.experiments.nl_eval import SLOT_KINDS, action_at_threshold, iter_jsonl, slot_counts
from backend.experiments.report_nl import (
    completion,
    fit_calibrator,
    intent_accuracy,
    misexecution,
    summarize,
    threshold_sweep,
)
from backend.experiments.stats import wilson_interval


def _interval(hits: int, n: int) -> dict[str, Any] | None:
    return wilson_interval(hits, n).__dict__ if n else None


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _slots_exact(row: Mapping[str, Any]) -> bool:
    return all((counts := slot_counts(row, kind)).fp == 0 and counts.fn == 0 for kind in SLOT_KINDS)


def _paired(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    *,
    key: Callable[[Mapping[str, Any]], tuple[Any, ...]],
    checks: Mapping[str, Callable[[Mapping[str, Any]], bool]],
) -> dict[str, Any]:
    old = {key(row): row for row in before}
    new = {key(row): row for row in after}
    shared = sorted(old.keys() & new.keys())
    result: dict[str, Any] = {
        "n_before": len(old),
        "n_after": len(new),
        "n_paired": len(shared),
        "n_before_only": len(old.keys() - new.keys()),
        "n_after_only": len(new.keys() - old.keys()),
    }
    for name, check in checks.items():
        improved: list[str] = []
        regressed: list[str] = []
        both_pass = 0
        both_fail = 0
        for item_key in shared:
            was = check(old[item_key])
            now = check(new[item_key])
            label = "/".join(map(str, item_key))
            if not was and now:
                improved.append(label)
            elif was and not now:
                regressed.append(label)
            elif now:
                both_pass += 1
            else:
                both_fail += 1
        result[name] = {
            "improved": len(improved),
            "regressed": len(regressed),
            "both_pass": both_pass,
            "both_fail": both_fail,
            "improved_keys": improved,
            "regressed_keys": regressed,
        }
    return result


def report_nl(
    current: Path,
    baseline: Path,
    output: Path,
    *,
    threshold: float,
) -> dict[str, Any]:
    rows = [r for r in iter_jsonl(current) if r.get("variant", "main") == "main"]
    before = [r for r in iter_jsonl(baseline) if r.get("variant", "main") == "main"]
    summary = summarize(current, "main", threshold)
    baseline_summary = summarize(baseline, "main", threshold)
    by_round: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_round[int(row["round_index"])].append(row)
    summary["per_round_wilson"] = {
        str(round_index): {
            "completion": completion(round_rows, threshold).__dict__,
            "intent_accuracy": intent_accuracy(round_rows).__dict__,
            "misexecution_over_all": misexecution(round_rows, threshold)[0].__dict__,
        }
        for round_index, round_rows in sorted(by_round.items())
    }
    by_layer: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_layer[str(row["layer"])].append(row)
    summary["per_layer"] = {
        layer: {
            "n": len(layer_rows),
            "completion": completion(layer_rows, threshold).__dict__,
            "intent_accuracy": intent_accuracy(layer_rows).__dict__,
            "slots_exact_diagnostic": _interval(
                sum(_slots_exact(row) for row in layer_rows), len(layer_rows)
            ),
        }
        for layer, layer_rows in sorted(by_layer.items())
    }
    summary["threshold_sweep"] = threshold_sweep(rows)
    try:
        calibrator, ece, bins = fit_calibrator(rows)
        summary["calibration"] = {
            "ece": ece,
            "n_fit_samples": calibrator.n_samples,
            "reliability_bins": [b.__dict__ for b in bins],
        }
    except ValueError as exc:
        summary["calibration"] = {"error": str(exc)}

    failures: list[dict[str, Any]] = []
    for row in rows:
        actual_action = action_at_threshold(row, threshold)
        action_ok = actual_action == row["expected_action"]
        intent_ok = row["observed_intent"] == row["expected_intent"]
        slots_ok = _slots_exact(row)
        if not (action_ok and intent_ok and slots_ok) or row.get("error"):
            failures.append(
                {
                    "round_index": row["round_index"],
                    "item_id": row["item_id"],
                    "layer": row["layer"],
                    "completion_ok": action_ok,
                    "expected_action": row["expected_action"],
                    "observed_action": actual_action,
                    "intent_ok": intent_ok,
                    "expected_intent": row["expected_intent"],
                    "observed_intent": row["observed_intent"],
                    "slots_exact": slots_ok,
                    "expected_slots": row["expected_slots"],
                    "observed_slots": row["observed_slots"],
                    "error": row.get("error", ""),
                }
            )

    checks: dict[str, Callable[[Mapping[str, Any]], bool]] = {
        "completion": lambda r: action_at_threshold(r, threshold) == r["expected_action"],
        "intent": lambda r: r["observed_intent"] == r["expected_intent"],
        "slots_exact": _slots_exact,
    }
    stopline: list[dict[str, Any]] = []
    nl_targets = {
        "completion": (float(summary["completion"]["point"]), 0.80, "min"),
        "intent_accuracy": (float(summary["intent_accuracy"]["point"]), 0.89, "min"),
        "slot_f1_micro": (float(summary["slot_f1_micro"]["f1"]), 0.85, "min"),
    }
    calibration = summary["calibration"]
    if "ece" in calibration:
        nl_targets["ece"] = (float(calibration["ece"]), 0.18, "max")
    for name, (actual, target, direction) in nl_targets.items():
        gap = target - actual if direction == "min" else actual - target
        if gap > 0.05 + 1e-12:
            stopline.append({"metric": name, "actual": actual, "target": target, "gap": gap})

    result = {
        "experiment": "experiment_1",
        "current_file": str(current),
        "baseline_file": str(baseline),
        "wilson_note": "Wilson 95% 只用于二项比例；槽位 micro-F1 不强套二项区间。",
        "summary": summary,
        "baseline_summary": baseline_summary,
        "aggregate_changes": {
            "completion_points": float(summary["completion"]["point"])
            - float(baseline_summary["completion"]["point"]),
            "intent_accuracy_points": float(summary["intent_accuracy"]["point"])
            - float(baseline_summary["intent_accuracy"]["point"]),
            "slot_f1_micro_points": float(summary["slot_f1_micro"]["f1"])
            - float(baseline_summary["slot_f1_micro"]["f1"]),
        },
        "stopline_breaches_over_5pt": stopline,
        "failures": failures,
        "paired_before_after": _paired(
            before,
            rows,
            key=lambda r: (r["round_index"], r["item_id"]),
            checks=checks,
        ),
    }
    _write(output, result)
    return result


def _trajectory_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if not r.get("error")]
    required = sum(int(r["steps"]["required_steps"]) for r in ok)
    tool_hits = sum(int(r["steps"]["tool_hits"]) for r in ok)
    param_n = sum(int(r["steps"]["param_denominator"]) for r in ok)
    param_hits = sum(int(r["steps"]["param_hits"]) for r in ok)
    calls = sum(int(r["steps"]["observed_calls"]) for r in ok)
    redundant = sum(int(r["steps"]["redundant"]) for r in ok)
    eligible = [r for r in ok if int(r["steps"]["required_steps"]) > 0]
    missing = sum(1 for r in eligible if int(r["steps"]["observed_calls"]) == 0)
    translations = [r for r in ok if r.get("revision_translation_ok") is not None]
    rollbacks = [r for r in ok if r.get("revision_rollback_ok") is not None]
    fidelity = [r for r in ok if r.get("answer_fidelity_ok") is not None]

    return {
        "n_scored": len(ok),
        "n_errored": len(rows) - len(ok),
        "tool_selection": _interval(tool_hits, required),
        "param_accuracy_diagnostic": _interval(param_hits, param_n),
        "redundant_call_rate": _interval(redundant, calls),
        "missing_call_rate": _interval(missing, len(eligible)),
        "path_correct": _interval(sum(bool(r["path_ok"]) for r in ok), len(ok)),
        "invalid_loop_rate": _interval(sum(bool(r["invalid_loop"]) for r in ok), len(ok)),
        "revision_translation": _interval(
            sum(bool(r["revision_translation_ok"]) for r in translations), len(translations)
        ),
        "revision_rollback": _interval(
            sum(bool(r["revision_rollback_ok"]) for r in rollbacks), len(rollbacks)
        ),
        "answer_fidelity_diagnostic": _interval(
            sum(bool(r["answer_fidelity_ok"]) for r in fidelity), len(fidelity)
        ),
        "mean_path_similarity": (
            sum(float(r["path_similarity"]) for r in ok) / len(ok) if ok else None
        ),
    }


def _trajectory_failure(row: Mapping[str, Any]) -> dict[str, Any] | None:
    reasons: list[str] = []
    steps = row["steps"]
    if not row.get("path_ok"):
        reasons.append("path")
    if int(steps["tool_hits"]) < int(steps["required_steps"]):
        reasons.append("tool_selection")
    if int(steps["param_hits"]) < int(steps["param_denominator"]):
        reasons.append("params")
    if int(steps["redundant"]) > 0:
        reasons.append("redundant")
    if int(steps["required_steps"]) > 0 and int(steps["observed_calls"]) == 0:
        reasons.append("missing_all_tools")
    if row.get("invalid_loop"):
        reasons.append("invalid_loop")
    if row.get("revision_translation_ok") is False:
        reasons.append("revision_translation")
    if row.get("revision_rollback_ok") is False:
        reasons.append("revision_rollback")
    if row.get("answer_fidelity_ok") is False:
        reasons.append("answer_fidelity")
    if row.get("error"):
        reasons.append("runner_error")
    if not reasons:
        return None
    return {
        "item_id": row["item_id"],
        "flow": row["flow"],
        "reasons": reasons,
        "path_reason": row.get("path_reason", ""),
        "expected_path": row["expected_path"],
        "scored_path": row.get("scored_path", row["observed_path"]),
        "observed_path": row["observed_path"],
        "score_components": row.get("score_components", []),
        "raw_observed_calls": row.get(
            "raw_observed_calls", row.get("steps", {}).get("observed_calls", 0)
        ),
        "steps": steps,
        "answer_text": row.get("answer_text", ""),
        "error": row.get("error", ""),
    }


def report_trajectory(
    current: Path,
    baseline: Path,
    record: Path,
    output: Path,
) -> dict[str, Any]:
    rows = list(iter_jsonl(current))
    before = list(iter_jsonl(baseline))
    recorded = list(iter_jsonl(record))
    checks: dict[str, Callable[[Mapping[str, Any]], bool]] = {
        "path": lambda r: bool(r["path_ok"]),
        "tools_present": lambda r: (
            not (int(r["steps"]["required_steps"]) > 0 and int(r["steps"]["observed_calls"]) == 0)
        ),
        "tool_steps": lambda r: int(r["steps"]["tool_hits"]) == int(r["steps"]["required_steps"]),
    }
    replay_by_id = {r["item_id"]: r for r in rows}
    record_by_id = {r["item_id"]: r for r in recorded}
    shared = sorted(replay_by_id.keys() & record_by_id.keys())
    mismatches = [item_id for item_id in shared if replay_by_id[item_id] != record_by_id[item_id]]
    replay_metrics = _trajectory_metrics(rows)
    metrics = _trajectory_metrics(recorded)
    baseline_metrics = _trajectory_metrics(before)
    trajectory_targets = {
        "tool_selection": (metrics["tool_selection"], 0.88, "min"),
        "path_correct": (metrics["path_correct"], 0.85, "min"),
        "revision_translation": (metrics["revision_translation"], 0.82, "min"),
        "revision_rollback": (metrics["revision_rollback"], 1.0, "min"),
    }
    stopline: list[dict[str, Any]] = []
    for name, (interval, target, direction) in trajectory_targets.items():
        if interval is None:
            continue
        actual = float(interval["point"])
        gap = target - actual if direction == "min" else actual - target
        if gap > 0.05 + 1e-12:
            stopline.append({"metric": name, "actual": actual, "target": target, "gap": gap})

    result = {
        "experiment": "experiment_5",
        "current_replay_file": str(current),
        "current_record_file": str(record),
        "baseline_file": str(baseline),
        "wilson_note": "八项中的二项比例均给 Wilson 95%；路径相似度均值不适用。",
        "metrics": metrics,
        "replay_metrics": replay_metrics,
        "metrics_basis": "record（真机完整运行）；replay 仅用于一致性门禁",
        "diagnostic_metrics_without_threshold": [
            "param_accuracy_diagnostic",
            "redundant_call_rate",
            "missing_call_rate",
            "invalid_loop_rate",
            "answer_fidelity_diagnostic",
            "mean_path_similarity",
        ],
        "baseline_metrics": baseline_metrics,
        "aggregate_point_changes": {
            name: (
                float(interval["point"]) - float(baseline_metrics[name]["point"])
                if interval is not None and baseline_metrics.get(name) is not None
                else None
            )
            for name, interval in metrics.items()
            if isinstance(interval, dict) and "point" in interval
        },
        "metrics_by_flow": {
            flow: _trajectory_metrics([row for row in recorded if row["flow"] == flow])
            for flow in sorted({str(row["flow"]) for row in recorded})
        },
        "stopline_breaches_over_5pt": stopline,
        "failures": [failure for row in recorded if (failure := _trajectory_failure(row))],
        "replay_failures": [failure for row in rows if (failure := _trajectory_failure(row))],
        "paired_before_after": _paired(
            before,
            recorded,
            key=lambda r: (r["item_id"],),
            checks=checks,
        ),
        "record_replay": {
            "n_record": len(recorded),
            "n_replay": len(rows),
            "n_paired": len(shared),
            "identical": len(mismatches) == 0 and len(recorded) == len(rows),
            "gate_passed": len(mismatches) == 0 and len(recorded) == len(rows),
            "mismatch_item_ids": mismatches,
        },
    }
    _write(output, result)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="实验一/五完整结果报告")
    sub = parser.add_subparsers(dest="experiment", required=True)
    nl = sub.add_parser("nl")
    nl.add_argument("--current", required=True)
    nl.add_argument("--baseline", required=True)
    nl.add_argument("--out", required=True)
    nl.add_argument("--threshold", type=float, default=0.75)
    trajectory = sub.add_parser("trajectory")
    trajectory.add_argument("--current", required=True)
    trajectory.add_argument("--record", required=True)
    trajectory.add_argument("--baseline", required=True)
    trajectory.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    if args.experiment == "nl":
        result = report_nl(
            Path(args.current), Path(args.baseline), Path(args.out), threshold=args.threshold
        )
    else:
        result = report_trajectory(
            Path(args.current), Path(args.baseline), Path(args.record), Path(args.out)
        )
    print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())
