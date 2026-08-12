"""200 场景的**格式校验通过率**（v6 §0.3 不可调指标之二）。

```bash
conda run -n schedule python -m tests.scenarios.format_suite
```

## 方案从哪来：读 M2-C 落盘的结果，不重解一遍

`reports/M2C_200场景运行结果.json` 里存着 200 个场景的判定与 **97 个出解场景的
方案原样**（`plans` 段，`runner.run_case` 的 `plan_sink` 写的）。M3 新增的是
**报告层** —— 渲染、回读、归档；求解层的 200 场景实测 M2-C 已经做完且结论固化。
重解一遍要 69 分钟、且按铁律 9 会得到同一批方案，对本窗口的结论没有任何增量。

所以这里的口径是明确的：**「200 场景的每一个可交付方案都渲染成 xlsx 并回读，
逐个断言深度相等」**。分母是 97（出解场景），不是 200 —— 另外 103 个是
INFEASIBLE，**没有方案可导**，把它们算进分母等于给自己送 103 个免费通过。

## 校验上下文取基准快照

与 `runner._validate` 同一口径：`load_context` 读基准快照，不按场景改写。
闸门3 的比对只用到 `name_map`（姓名 → 编号）与 S-11 开关，两者与场景扰动无关。
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.core.config import PROJECT_ROOT
from backend.core.db import session_scope
from backend.report.bundle import ProvenanceInfo, ReportBundle
from backend.report.excel import render_workbook
from backend.schemas.plan import SchedulePlan
from backend.schemas.solver import SolverStats
from backend.validator import load_context, run_all_checks, verify_format
from backend.validator.context import ValidationContext
from backend.validator.workbook import verify_workbook

#: M2-C 的 200 场景运行结果（含 `plans` 段）
SOURCE_JSON = PROJECT_ROOT / "reports" / "M2C_200场景运行结果.json"
#: 本窗口的产物
OUTPUT_JSON = PROJECT_ROOT / "reports" / "M3_200场景格式校验.json"

DEFAULT_SNAPSHOT = "snap_9724982865ee"
DEFAULT_WEEK = date(2026, 1, 5)
GENERATED_AT = datetime(2026, 8, 13, 9, 0, 0, tzinfo=timezone(timedelta(hours=8)))


@dataclass
class FormatResult:
    scenario_id: str
    sorties: int
    rendered: bool = False
    readback_passed: bool = False
    diff: tuple[str, ...] = ()
    error: str | None = None


@dataclass
class FormatSummary:
    scenarios_total: int = 0
    plans_total: int = 0
    rendered: int = 0
    readback_passed: int = 0
    format_pass_rate: float = 0.0
    failures: tuple[str, ...] = field(default_factory=tuple)


def _stats_for(plan: SchedulePlan) -> SolverStats:
    """回放用的求解统计。

    M2-C 的结果 JSON 只存了方案，没存逐场景的 `SolverStats`，所以这里给一份
    **标注清楚的占位统计**：它只影响区块1 的展示，不参与回读反解，也不会被
    当成任何实测指标写进报告（铁律 6 —— 真实求解统计在基准周集成测试里）。
    """
    return SolverStats(
        status="FEASIBLE",
        num_candidates=0,
        num_variables=0,
        num_constraints=0,
        objective_value=0.0,
        wall_time_ms=0.0,
        relaxation_tier=plan.relaxation_tier,
    )


def check_plan(plan: SchedulePlan, ctx: ValidationContext, scenario_id: str) -> FormatResult:
    result = FormatResult(scenario_id=scenario_id, sorties=len(plan.sorties))
    try:
        bundle = ReportBundle(
            plan=plan,
            ctx=ctx,
            validation=run_all_checks(plan, ctx),
            stats=_stats_for(plan),
            generated_at=GENERATED_AT,
            format_report=verify_format(plan, ctx),
            provenance=ProvenanceInfo(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{scenario_id}.xlsx"
            render_workbook(path, bundle, readback_passed=True)
            result.rendered = True
            report = verify_workbook(path, plan, ctx=ctx)
        result.readback_passed = report.passed
        result.diff = tuple(report.diff)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def run_suite(
    *,
    source: Path = SOURCE_JSON,
    snapshot_id: str = DEFAULT_SNAPSHOT,
    week_start: date = DEFAULT_WEEK,
) -> tuple[FormatSummary, list[FormatResult]]:
    payload: Mapping[str, Any] = json.loads(source.read_text(encoding="utf-8"))
    plans: Mapping[str, Any] = payload["plans"]
    with session_scope() as session:
        ctx = load_context(session, snapshot_id=snapshot_id, week_start=week_start)

    results = [
        check_plan(SchedulePlan.model_validate(plans[sid]), ctx, sid) for sid in sorted(plans)
    ]
    passed = sum(1 for r in results if r.readback_passed)
    summary = FormatSummary(
        scenarios_total=len(payload["results"]),
        plans_total=len(results),
        rendered=sum(1 for r in results if r.rendered),
        readback_passed=passed,
        format_pass_rate=(passed / len(results)) if results else 0.0,
        failures=tuple(r.scenario_id for r in results if not r.readback_passed),
    )
    return summary, results


def main() -> None:  # pragma: no cover - CLI 入口
    summary, results = run_suite()
    OUTPUT_JSON.write_text(
        json.dumps(
            {"summary": asdict(summary), "results": [asdict(r) for r in results]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"场景 {summary.scenarios_total} 个，其中出解 {summary.plans_total} 个；"
        f"渲染 {summary.rendered}，回读通过 {summary.readback_passed}，"
        f"格式校验通过率 {summary.format_pass_rate:.1%}"
    )
    if summary.failures:
        print("未通过：", ", ".join(summary.failures))
    print(f"结果已写入 {OUTPUT_JSON}")


if __name__ == "__main__":  # pragma: no cover
    main()
