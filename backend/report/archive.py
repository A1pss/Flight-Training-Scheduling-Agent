"""归档：`data/plans/YYYY/Www/` 下的四件套（v6 §10.6）。

```
data/plans/2026/W02/
  FTP_NAU_WEEKLY_2026W02_20260105-20260111_v3_APPROVED_7f3a9c21.xlsx
  FTP_..._v3_APPROVED_7f3a9c21.json            # 机器可读，**权威源**
  FTP_..._v3_APPROVED_7f3a9c21.manifest.yaml   # 可复现性清单
  validation_report_v3.json
  solver_log_v3.txt
  versions.json                                # 版本台账（naming.py，只增不改）
```

## 为什么 json 是「权威源」而 xlsx 不是

xlsx 是给人看的，它经过版式加工（拼接、分组、姓名而非编号）。机器要消费的是
`SchedulePlan` 的原样序列化 —— 它与 xlsx 的等价性由闸门3 的深度相等断言保证，
所以两者不会漂。真要拿一个当准，取 json。

## `solver_log.txt` 没采到就如实写没采到

CP-SAT 的日志要在求解时开 `capture_log` 才有。M2-A 的实测经验是：开着它跑
基准周会因为 coverage 插桩把墙钟拖慢约 50%，所以不是每次求解都开。
**没有日志时这个文件写一行说明，不伪造日志**（铁律 6）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.core.config import PROJECT_ROOT
from backend.report.bundle import ReportBundle
from backend.report.manifest import build_manifest, write_manifest
from backend.report.naming import PlanName, allocate_name, week_dir
from backend.report.verify import ExportResult, export_workbook
from backend.schemas.validation import SchemaCheckReport

#: 未采集到求解日志时写入 `solver_log.txt` 的内容（不伪造日志）
NO_SOLVER_LOG = "（本次求解未采集 CP-SAT 日志：求解时未开启 capture_log）\n"


@dataclass(frozen=True)
class ArchiveResult:
    """一次归档落盘的全部路径。"""

    directory: Path
    name: PlanName
    xlsx: Path
    plan_json: Path
    manifest: Path
    validation_report: Path
    solver_log: Path
    export: ExportResult

    def all_paths(self) -> tuple[Path, ...]:
        return (self.xlsx, self.plan_json, self.manifest, self.validation_report, self.solver_log)


def code_version_from_git(root: Path | None = None) -> str | None:
    """`git:8a3f21c` —— 直接读 `.git`，不起子进程。

    读文件而不是 `git rev-parse` 有两个好处：离线容器里没有 git 也能跑，
    以及不引入 `subprocess` 这个安全扫描的常客。
    """
    base = (root or PROJECT_ROOT) / ".git"
    head = base / "HEAD"
    if not head.exists():
        return None
    content = head.read_text(encoding="utf-8").strip()
    if content.startswith("ref:"):
        ref = base / content.removeprefix("ref:").strip()
        if not ref.exists():
            packed = base / "packed-refs"
            if not packed.exists():
                return None
            target = content.removeprefix("ref:").strip()
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(f" {target}"):
                    return f"git:{line.split()[0][:7]}"
            return None
        content = ref.read_text(encoding="utf-8").strip()
    return f"git:{content[:7]}" if content else None


def _validation_payload(bundle: ReportBundle, export: SchemaCheckReport) -> dict[str, Any]:
    fmt = bundle.format_report
    return {
        "plan_id": bundle.plan.plan_id,
        "generated_at": bundle.generated_at.isoformat(),
        "gate1_rules": bundle.validation.model_dump(mode="json"),
        "gate2_format": None
        if fmt is None
        else {
            "passed": fmt.passed,
            "schema_errors": list(fmt.schema_errors),
            "integrity_errors": list(fmt.integrity_errors),
            "cross_table_errors": list(fmt.cross_table_errors),
        },
        "gate3_workbook": export.model_dump(mode="json"),
    }


def archive_plan(
    bundle: ReportBundle,
    *,
    root: Path | None = None,
    now: datetime | None = None,
) -> ArchiveResult:
    """分配版本号 → 写 xlsx（含闸门3）→ 写 json / manifest / 校验报告 / 求解日志。

    闸门3 不通过时 :func:`export_workbook` 会抛 FTS-5001，**此时四件套一个都不落**
    —— 版本号已经分配掉了（台账里留有记录），这是刻意的：那个号从此作废，
    绝不复用（§10.6）。
    """
    stamp = now or bundle.generated_at
    name = allocate_name(bundle, root=root, now=stamp)
    directory = week_dir(bundle.plan.iso_week, root=root)

    export = export_workbook(directory / name.xlsx, bundle)

    plan_json = directory / name.json
    plan_json.write_text(
        json.dumps(bundle.plan.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest_path = write_manifest(directory / name.manifest, build_manifest(bundle, name))

    report_path = directory / name.validation_report
    report_path.write_text(
        json.dumps(_validation_payload(bundle, export.report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    log_path = directory / name.solver_log
    log_path.write_text(bundle.solver_log or NO_SOLVER_LOG, encoding="utf-8")

    return ArchiveResult(
        directory=directory,
        name=name,
        xlsx=export.path,
        plan_json=plan_json,
        manifest=manifest_path,
        validation_report=report_path,
        solver_log=log_path,
        export=export,
    )


__all__ = [
    "NO_SOLVER_LOG",
    "ArchiveResult",
    "archive_plan",
    "code_version_from_git",
]
