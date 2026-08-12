"""200 场景测试集的 CLI（v6 §12.3）。

```bash
# ① 标定边界档 + 生成清单 → datasets/plan_scenarios/v1/
conda run -n schedule python -m tests.scenarios.run_suite generate

# ② 只看清单（不跑求解），发业务方审核
conda run -n schedule python -m tests.scenarios.run_suite show

# ③ 全量跑（约一小时）→ reports/M2C_200场景运行结果.{json,md}
conda run -n schedule python -m tests.scenarios.run_suite run
conda run -n schedule python -m tests.scenarios.run_suite run --only infeasible,boundary
```

**为什么分成两步**：清单要先经业务方审核再跑全量（CC_PROMPTS W4 的硬性要求）。
`generate` 只做标定与落盘，`run` 才真正求解。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path

from backend.core.config import PROJECT_ROOT
from backend.core.db import session_scope
from backend.solver.solve import solve_week
from tests.scenarios.calibrate import calibrate_boundary
from tests.scenarios.catalog import (
    ScenarioCase,
    build_catalog,
    catalog_counts,
    dataset_dir,
    load_dataset,
    write_dataset,
)
from tests.scenarios.runner import (
    ScenarioResult,
    c_class_airspaces,
    load_entities,
    run_case,
    summarize,
)

DEFAULT_SNAPSHOT = "snap_9724982865ee"
DEFAULT_WEEK = date(2026, 1, 5)


def generate(snapshot_id: str, week_start: date, root: Path) -> list[ScenarioCase]:
    """标定边界档 → 拼清单 → 落盘。"""
    with session_scope() as session:
        ents = load_entities(session, snapshot_id=snapshot_id, week_start=week_start)
        c_airspaces = c_class_airspaces(session, snapshot_id=snapshot_id, ents=ents)
        print(
            f"实体：{len(ents.persons)} 人 / {len(ents.aircraft)} 机 / "
            f"{len(ents.airspaces)} 空域 / {len(ents.runways)} 跑道 / {len(ents.missions)} 课目"
        )
        print(f"I3 关闭的空域（算出来的，不是写死）：{c_airspaces}")
        print("标定边界档（可行性求解，约每次 2 秒）…")
        boundary, calibrations = calibrate_boundary(session, ents, wanted=20)
        for cal in calibrations:
            mark = f"临界档 {cal.critical_level}" if cal.found else "无临界档（拧到底仍可解）"
            print(f"  {cal.knob_id:26s} {mark:22s} 探针 {cal.probes}")
        cases = build_catalog(ents, c_airspaces=c_airspaces, boundary=boundary)
        target = write_dataset(root, ents, cases)
        (target / "calibration.json").write_text(
            json.dumps([asdict(c) for c in calibrations], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(f"\n共 {len(cases)} 个场景 → {target}")
    print(json.dumps(catalog_counts(cases), ensure_ascii=False))
    return cases


def show(root: Path) -> None:
    cases = load_dataset(root)
    counts = catalog_counts(cases)
    print(f"共 {len(cases)} 个场景：{json.dumps(counts, ensure_ascii=False)}\n")
    current = ""
    for case in cases:
        if case.category != current:
            current = case.category
            print(f"\n── {current} ──")
        pair = f" [{case.pair_id}/{case.pair_role}]" if case.pair_id else ""
        print(f"  {case.scenario_id:10s} 期望={case.expected_status:10s}{pair} {case.title}")
        if case.annotated_conflict_rules:
            print(f"             标注冲突源 {list(case.annotated_conflict_rules)}")


def run(root: Path, snapshot_id: str, week_start: date, only: Sequence[str] | None) -> int:
    cases = load_dataset(root)
    if only:
        cases = [c for c in cases if c.category in set(only)]
    results: list[ScenarioResult] = []
    with session_scope() as session:
        ents = load_entities(session, snapshot_id=snapshot_id, week_start=week_start)
        print("先解一版基准周计划（局部重排场景要用它当「已批准计划」）…")
        baseline = solve_week(session, snapshot_id=snapshot_id, week_start=week_start)
        print(f"  基准周 {baseline.status}，{len(baseline.sorties)} 架次")
        for i, case in enumerate(cases, start=1):
            result = run_case(session, case, ents, baseline_plan=baseline.plan)
            results.append(result)
            flag = "✅" if (result.status_ok and result.agrees and not result.error) else "❌"
            print(
                f"[{i:3d}/{len(cases)}] {flag} {result.scenario_id:10s} {result.status:11s} "
                f"架次={result.num_sorties:2d} 阻塞={result.num_blocked:2d} "
                f"main={'过' if result.main_passed else ('—' if result.main_passed is None else '违规')} "
                f"naive={'过' if result.naive_passed else ('—' if result.naive_passed is None else '违规')} "
                f"{result.wall_time_s:5.1f}s",
                flush=True,
            )
            if result.error:
                print(f"          错误：{result.error}", flush=True)
    summary = summarize(cases, results)
    out_json = PROJECT_ROOT / "reports" / "M2C_200场景运行结果.json"
    out_json.write_text(
        json.dumps(
            {"summary": asdict(summary), "results": [r.to_json() for r in results]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n结果已写入 {out_json}")
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    return 0 if (summary.crosscheck_agreement == 1.0 and summary.errors == 0) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v6 §12.3 的 200 场景测试集")
    parser.add_argument("command", choices=("generate", "show", "run"))
    parser.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    parser.add_argument("--week-start", default=DEFAULT_WEEK.isoformat())
    parser.add_argument("--only", default=None, help="只跑某几类，逗号分隔")
    args = parser.parse_args(argv)
    root = PROJECT_ROOT
    week_start = date.fromisoformat(args.week_start)
    if args.command == "generate":
        generate(args.snapshot, week_start, root)
        return 0
    if args.command == "show":
        show(root)
        return 0
    only = args.only.split(",") if args.only else None
    return run(root, args.snapshot, week_start, only)


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    sys.exit(main())


__all__ = ["DEFAULT_SNAPSHOT", "DEFAULT_WEEK", "dataset_dir", "generate", "main", "run", "show"]
