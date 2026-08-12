"""四表 Excel 输出、回读校验、命名与归档（v6 §10）。

```
ReportBundle ──render_workbook──→ xlsx ──verify_workbook──→ SchedulePlan
     │                                                          │
     │                        deep_diff 必须为空（§4.3 闸门3）───┘
     └──archive_plan──→ data/plans/YYYY/Www/{xlsx,json,manifest.yaml,
                                             validation_report,solver_log}
```

版式契约（工作表名与顺序、表头、区块标题、拼接格式）全部来自
`backend.validator.workbook`，本包不另存一份。
"""

from backend.report.archive import ArchiveResult, archive_plan, code_version_from_git
from backend.report.bundle import (
    ApprovalInfo,
    ProvenanceInfo,
    RelaxationRecord,
    ReportBundle,
)
from backend.report.excel import render_workbook, sheet_titles
from backend.report.manifest import (
    build_manifest,
    dump_manifest,
    load_manifest,
    missing_reproducibility_fields,
    write_manifest,
)
from backend.report.naming import PlanName, allocate_name, next_version, parse_name, week_dir
from backend.report.verify import ExportResult, export_workbook, verify_existing

__all__ = [
    "ApprovalInfo",
    "ArchiveResult",
    "ExportResult",
    "PlanName",
    "ProvenanceInfo",
    "RelaxationRecord",
    "ReportBundle",
    "allocate_name",
    "archive_plan",
    "build_manifest",
    "code_version_from_git",
    "dump_manifest",
    "export_workbook",
    "load_manifest",
    "missing_reproducibility_fields",
    "next_version",
    "parse_name",
    "render_workbook",
    "sheet_titles",
    "verify_existing",
    "week_dir",
    "write_manifest",
]
