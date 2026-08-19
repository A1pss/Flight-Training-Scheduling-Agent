"""把 200 场景的运行结果汇成验收报告用的表格（v6 §12.3 / §12.7）。

```bash
conda run -n schedule python -m tests.scenarios.report
```

只读 `reports/M2C_200场景运行结果.json`，不重跑任何求解 —— 报告里的每个数都来自
那一次真实运行（`CLAUDE.md` 铁律 6）。

产出四块：

1. **总览** —— v6 §0.3 的四条断言 + 分类状态分布
2. **不可行族** —— I1~I5 逐族的 INFEASIBLE 判定率、冲突源召回/精确率、升级人工
3. **边界对** —— 20 组「恰好够 / 恰好差 1」逐对核对
4. **人工抽检清单** —— 每类固定种子抽 5 个（v6 §12.3 度量方式第 3 条）
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from backend.core.config import PROJECT_ROOT
from tests.scenarios.catalog import INFEASIBLE_FAMILIES, load_eval_dataset

RESULT_PATH: Path = PROJECT_ROOT / "reports" / "M2C_200场景运行结果.json"

#: 人工抽检的抽样种子（可复现 —— 业务方要能按同一份清单复核）
SAMPLE_SEED: int = 20260812
SAMPLES_PER_CATEGORY: int = 5

SOLVED = ("OPTIMAL", "FEASIBLE")


def load_results() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    return payload["summary"], payload["results"]


def overview(summary: Mapping[str, Any], results: Sequence[Mapping[str, Any]]) -> str:
    solved = [r for r in results if r["status"] in SOLVED]
    lines = [
        "### 总览",
        "",
        "| v6 §0.3 断言 | 口径 | 实测 |",
        "|---|---|---|",
        f"| 输出方案 100% 合规 | 出解场景中主校验器 0 条 HARD 违规的比例 | "
        f"**{summary['hard_pass_rate'] * 100:.1f}%**（{sum(1 for r in solved if r['main_passed'])}/{len(solved)}） |",
        f"| 格式校验 100% 通过 | 出解场景通过闸门2 的比例 | "
        f"**{summary['format_pass_rate'] * 100:.1f}%** |",
        f"| 阻塞项 100% 披露 | 出解场景无披露缺口的比例 | "
        f"**{summary['disclosure_rate'] * 100:.1f}%** |",
        f"| 主校验器 vs naive checker 逐条一致 | 判定集合相等的比例 | "
        f"**{summary['crosscheck_agreement'] * 100:.1f}%**（{len(results)} 个场景） |",
        "",
        f"运行错误 **{summary['errors']}** 个 · `UNKNOWN` **{summary['unknown']}** 个"
        f"（铁律 8：UNKNOWN 不得与 INFEASIBLE 混为一谈）",
        "",
        "| 类别 | 数量 | OPTIMAL | FEASIBLE | INFEASIBLE | UNKNOWN |",
        "|---|---|---|---|---|---|",
    ]
    for category in ("baseline", "single", "combo", "boundary", "infeasible", "reschedule"):
        rows = [r for r in results if r["category"] == category]
        if not rows:
            continue
        counts = {
            s: sum(1 for r in rows if r["status"] == s)
            for s in ("OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN")
        }
        lines.append(
            f"| {category} | {len(rows)} | {counts['OPTIMAL']} | {counts['FEASIBLE']} | "
            f"{counts['INFEASIBLE']} | {counts['UNKNOWN']} |"
        )
    if summary["disagreements"]:
        lines += ["", "**★ 分歧（必须为空）**：", ""]
        lines += [f"- {d}" for d in summary["disagreements"]]
    else:
        lines += ["", "**分歧：无。** 两条独立实现在全部场景上判定逐条一致。"]
    return "\n".join(lines)


def infeasible_section(summary: Mapping[str, Any], results: Sequence[Mapping[str, Any]]) -> str:
    cases = {c.scenario_id: c for c in load_eval_dataset(PROJECT_ROOT)}
    lines = [
        "### 不可行族 I1~I5",
        "",
        "| 场景 | 状态 | 标注冲突源 | 归因规则 | `sat_core_ids` | `structural_ids` | 召回 | 精确 | 升级人工 | 有效提案 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        if result["category"] != "infeasible":
            continue
        case = cases[result["scenario_id"]]
        annotated = set(case.annotated_conflict_rules)
        reported = set(result["conflict_rules"])
        hit = annotated & reported
        recall = len(hit) / len(annotated) if annotated else 0.0
        precision = len(hit) / len(reported) if reported else 0.0
        lines.append(
            f"| {result['scenario_id']} | {result['status']} | {sorted(annotated)} | "
            f"{sorted(reported)} | {list(result['sat_core_ids'])} | "
            f"{list(result['structural_ids'])} | {recall * 100:.0f}% | {precision * 100:.0f}% | "
            f"{result['escalate']} | {result['useful_proposals']} |"
        )
    lines += [
        "",
        f"**判定率**：{summary['infeasible_family_correct']}/{summary['infeasible_family_total']} "
        f"判为 `INFEASIBLE`，`UNKNOWN` **{summary['infeasible_family_unknown']}** 个",
        f"**冲突源召回率**：micro **{summary['conflict_recall_micro'] * 100:.1f}%** · "
        f"macro **{summary['conflict_recall_macro'] * 100:.1f}%**（目标 100%）",
        f"**冲突源精确率**：micro **{summary['conflict_precision_micro'] * 100:.1f}%** · "
        f"macro **{summary['conflict_precision_macro'] * 100:.1f}%**（目标 ≥60%）",
        "",
        "> 标注来源：v6 §12.3 表格的「预期最小冲突集」列，**不是本窗口自己编的**。",
        "> 「归因规则」= `Diagnosis.conflicts` 各项 `rule_ids` 的并集，它同时包含",
        "> `sat_core_ids`（CP-SAT 的极小 core）与 `structural_ids`（结构性不可满足组），",
        "> 外加归因阶段从 `DropReason` 补回来的根因规则（v6 §3.9，M2-A §3.10）。",
    ]
    for family in INFEASIBLE_FAMILIES:
        rows = [r for r in results if r["family"] == family.family_id]
        with_rule = {rid for r in rows for rid in r["conflict_rules"]}
        lines.append(
            f"- **{family.family_id}**（{family.title}）：{len(rows)} 个变体，"
            f"状态 {sorted({r['status'] for r in rows})}，归因规则并集 {sorted(with_rule)}"
        )
    return "\n".join(lines)


def boundary_section(results: Sequence[Mapping[str, Any]]) -> str:
    cases = {c.scenario_id: c for c in load_eval_dataset(PROJECT_ROOT)}
    pairs: dict[str, dict[str, Mapping[str, Any]]] = {}
    for result in results:
        case = cases[result["scenario_id"]]
        if case.pair_id and case.pair_role:
            pairs.setdefault(case.pair_id, {})[case.pair_role] = result
    lines = [
        "### 边界对（「恰好」的成对证明）",
        "",
        "| 对 | 旋钮 | 恰好够 | 恰好差 1 | 「恰好」成立 |",
        "|---|---|---|---|---|",
    ]
    ok = 0
    for pair_id in sorted(pairs):
        members = pairs[pair_id]
        enough = members.get("enough")
        short = members.get("short")
        if enough is None or short is None:
            lines.append(f"| {pair_id} | — | 缺成员 | 缺成员 | ❌ |")
            continue
        good = enough["status"] in SOLVED and short["status"] == "INFEASIBLE"
        ok += int(good)
        lines.append(
            f"| {pair_id} | `{enough['family']}` | {enough['status']}（{enough['num_sorties']} 架次） | "
            f"{short['status']} | {'✅' if good else '❌'} |"
        )
    lines += ["", f"**{ok}/{len(pairs)} 组成立。**"]
    return "\n".join(lines)


def sampling_section(results: Sequence[Mapping[str, Any]]) -> str:
    rng = random.Random(SAMPLE_SEED)
    lines = [
        "### 人工抽检清单（v6 §12.3 度量方式第 3 条）",
        "",
        f"每类固定种子（`SAMPLE_SEED={SAMPLE_SEED}`）抽 {SAMPLES_PER_CATEGORY} 个，"
        "业务方按同一份清单复核即可复现这批抽样。",
        "",
        "| 场景 | 类别 | 状态 | 架次 | 阻塞 | 标题 |",
        "|---|---|---|---|---|---|",
    ]
    for category in ("baseline", "single", "combo", "boundary", "infeasible", "reschedule"):
        rows = [r for r in results if r["category"] == category]
        picked = (
            rows if len(rows) <= SAMPLES_PER_CATEGORY else rng.sample(rows, SAMPLES_PER_CATEGORY)
        )
        for result in sorted(picked, key=lambda r: r["scenario_id"]):
            lines.append(
                f"| {result['scenario_id']} | {category} | {result['status']} | "
                f"{result['num_sorties']} | {result['num_blocked']} | {result['title'][:56]} |"
            )
    return "\n".join(lines)


def render() -> str:
    summary, results = load_results()
    return "\n\n".join(
        [
            overview(summary, results),
            infeasible_section(summary, results),
            boundary_section(results),
            sampling_section(results),
        ]
    )


def main() -> int:  # pragma: no cover —— CLI 入口
    print(render())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "RESULT_PATH",
    "SAMPLES_PER_CATEGORY",
    "SAMPLE_SEED",
    "boundary_section",
    "infeasible_section",
    "load_results",
    "overview",
    "render",
    "sampling_section",
]
