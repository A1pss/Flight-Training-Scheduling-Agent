"""实验三的指标聚合：`python -m backend.experiments.report_memory`。

零 LLM 调用 —— 全部由 `exp3_memory320.jsonl` 的观测算出来。

## 三处口径，报数时必须跟着写清楚

1. **`absent` 探针是负例，不进 Recall 的分母**（它们的正确行为是一条都不召回），
   单独报误召回率。
2. **程序类的 Recall@5 量的不是排序质量**：程序记忆不走 `retrieve()`，
   由 `memory.search` 取 `list_preferences(at=…)` 的前 top_k 条，**没有按查询排序**。
   所以那个数反映的是「有效期过滤对不对 + 条数够不够挤进前 5」。
3. **「去 SQL 精确路」的失效形态是可答性、不是召回率**（`Z-22`，M5 预演实测）：
   召回其实没怎么降，能答的问题少了一大半。**只报 Recall@5 的降幅会低估这条消融
   的影响** —— 报告里要把这句话带上。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from backend.experiments.memory_eval import ProbeOutcome
from backend.experiments.stats import weighted_rate, wilson_interval

#: v6 §12.4 的分层目标。
TARGETS: dict[str, float] = {"semantic": 0.98, "episodic": 0.91, "procedural": 0.88}
OVERALL_TARGET = 0.92
MRR_TARGET = 0.70
TIMELINESS_TARGET = 0.94


def timeline_weeks() -> dict[str, Any]:
    """item_id → 写入周。

    `ProbeOutcome` 不记这个字段（它是**数据集的属性**不是观测结果），
    衰减测试要按写入周分层，就回数据集查一次。比重跑一遍 320 条便宜得多。
    """
    path = Path("datasets/memory_320/v1/items.jsonl")
    out: dict[str, Any] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            out[str(item["item_id"])] = item.get("timeline_week")
    return out


def load(path: Path) -> list[ProbeOutcome]:
    rows: list[ProbeOutcome] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(ProbeOutcome(**json.loads(line)))
    return rows


def _recall(outcomes: Sequence[ProbeOutcome]) -> dict[str, Any] | None:
    scored = [o for o in outcomes if not o.is_absent_probe and not o.error]
    if not scored:
        return None
    hits = sum(1 for o in scored if o.hit_at_5)
    return wilson_interval(hits, len(scored)).__dict__


def summarize(rows: Sequence[ProbeOutcome], variant: str) -> dict[str, Any]:
    sel = [o for o in rows if o.variant == variant]
    if not sel:
        raise ValueError(f"没有 variant={variant} 的观测")

    by_type: dict[str, list[ProbeOutcome]] = defaultdict(list)
    for o in sel:
        by_type[o.memory_type].append(o)

    scored = [o for o in sel if not o.is_absent_probe and not o.error]
    absent = [o for o in sel if o.is_absent_probe and not o.error]
    mrr = (
        sum(1.0 / o.first_hit_rank_at10 if o.first_hit_rank_at10 else 0.0 for o in scored)
        / len(scored)
        if scored
        else 0.0
    )

    # 时效正确率：时效类探针（temporal_validity）召回到**当前有效版本**的比例。
    temporal = [o for o in sel if o.probe_kind == "temporal_validity" and not o.error]
    timeliness = (
        wilson_interval(sum(1 for o in temporal if o.hit_at_5), len(temporal)).__dict__
        if temporal
        else None
    )

    # 时间衰减：按写入周分层（第 1/4/8/12/16/20 周），在第 20 周提问。
    decay: dict[str, Any] = {}
    weeks = timeline_weeks()
    by_week: dict[Any, list[ProbeOutcome]] = defaultdict(list)
    for o in sel:
        if o.probe_kind == "decay" and not o.error:
            by_week[weeks.get(o.item_id)].append(o)
    for week, group in sorted(by_week.items(), key=lambda kv: (kv[0] is None, kv[0])):
        decay[str(week)] = wilson_interval(sum(1 for o in group if o.hit_at_5), len(group)).__dict__

    per_type: dict[str, dict[str, Any]] = {
        t: {
            "n_scored": len([o for o in group if not o.is_absent_probe and not o.error]),
            "recall_at_5": _recall(group),
            "target": TARGETS.get(t),
        }
        for t, group in sorted(by_type.items())
    }

    # v6 §12.4 那笔加权账：(120×语义 + 120×情景 + 80×程序) / 320
    parts: list[tuple[int, float]] = []
    for block in per_type.values():
        n = int(block["n_scored"])
        recall = block["recall_at_5"]
        if n and isinstance(recall, dict):
            parts.append((n, float(recall["point"])))

    return {
        "variant": variant,
        "n_total": len(sel),
        "n_scored": len(scored),
        "n_absent": len(absent),
        "n_errors": sum(1 for o in sel if o.error),
        "recall_at_5_overall": _recall(sel),
        "recall_weighted": weighted_rate(parts) if parts else None,
        "per_type": per_type,
        "mrr_at_10": mrr,
        "timeliness": timeliness,
        "false_recall": {
            "hits": sum(1 for o in absent if o.retrieved_doc_ids),
            "n": len(absent),
        },
        "decay_by_write_week": decay,
        "targets": {
            "overall": OVERALL_TARGET,
            "mrr": MRR_TARGET,
            "timeliness": TIMELINESS_TARGET,
            **TARGETS,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="实验三指标聚合")
    parser.add_argument("--path", default="reports/m9b/exp3_memory320.jsonl")
    parser.add_argument("--out", default="reports/m9b/exp3_summary.json")
    args = parser.parse_args(argv)

    rows = load(Path(args.path))
    variants = sorted({o.variant for o in rows})
    summary: dict[str, Any] = {"variants": {}}
    for v in variants:
        try:
            summary["variants"][v] = summarize(rows, v)
        except ValueError as exc:  # pragma: no cover
            summary["variants"][v] = {"error": str(exc)}

    main_recall = (summary["variants"].get("main") or {}).get("recall_at_5_overall")
    for v, data in summary["variants"].items():
        if v == "main" or not isinstance(data, dict) or "recall_at_5_overall" not in data:
            continue
        other = data["recall_at_5_overall"]
        if main_recall and other:
            data["delta_vs_main"] = other["point"] - main_recall["point"]

    Path(args.out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2)[:2500])
    return 0


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    sys.exit(main())
