"""闸门3：写出即回读（v6 §4.3）。

```
render → 回读反解为 SchedulePlan → deep_diff(源, 回读) 必须为空
                                    ↓ 非空
                       不交付文件 + 保留中间 JSON + 抛 FTS-5001
```

## 比对逻辑复用校验器那一份，不另写

`backend/validator/workbook.py::verify_workbook` 已经把「工作表名与顺序 /
表头逐字 / 单元格类型（时间列必须是 HH:MM 文本）/ 分组结构 / 深度相等 /
三表交叉一致 / S-11 授权改写声明在场」全部实现了。本模块只负责**编排**：
写出 → 调它 → 处理失败。写出侧再实现一份比对等于自己给自己判卷。

## 为什么要写两遍

区块2 末行有一格叫「产物回读层」。第一遍写出时闸门3 还没跑，此时若填 ✅ 就是
报告一个未实际计算的结果（铁律 6），所以第一遍填「待回读」。第一遍回读通过后，
用同一个 bundle 重渲一遍、把那一格填成 ✅，**再回读一次**确认最终文件本身也通过。

两遍写出的差异只在区块2 的那一格 —— 它不参与反解（回读只解区块1/3/4/7 与
Sheet 1~3），所以第二遍的深度相等结论与第一遍必然一致；第二次回读是为了让
「交付出去的这个文件」自己也被验过，而不是验了它的前身。

失败时**两遍都不交付**：xlsx 只写在同目录的临时文件上，通过后才 `replace`
到正式路径，中途抛异常连半个文件都不会落到交付位置。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend.core.errors import ExportVerifyError
from backend.report.bundle import ReportBundle
from backend.report.excel import render_workbook
from backend.schemas.validation import SchemaCheckReport
from backend.validator.workbook import verify_workbook

#: 回读失败时保留的中间产物后缀（v6 §4.3：不交付文件，但证据要留）
FAILED_JSON_SUFFIX = ".failed.json"
#: 写出中的临时文件后缀
STAGING_SUFFIX = ".staging.xlsx"


@dataclass(frozen=True)
class ExportResult:
    """一次成功导出的结果。`report.passed` 恒为 True —— 不通过根本不返回。"""

    path: Path
    report: SchemaCheckReport
    passes: int


def _quarantine(path: Path, bundle: ReportBundle, report: SchemaCheckReport, *, phase: str) -> Path:
    """保留中间 JSON：源方案 + 差异清单，供排查。"""
    payload = {
        "phase": phase,
        "plan_id": bundle.plan.plan_id,
        "iso_week": bundle.plan.iso_week,
        "content_sha256": bundle.plan.content_sha256,
        "recorded_at": datetime.now().astimezone().isoformat(),
        "diff": list(report.diff),
        "sheet_names": list(report.sheet_names),
        "plan": bundle.plan.model_dump(mode="json"),
    }
    target = path.with_name(path.stem + FAILED_JSON_SUFFIX)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def _fail(
    path: Path, bundle: ReportBundle, report: SchemaCheckReport, phase: str, staging: Path
) -> None:
    """不交付文件：临时 xlsx 删掉，只留中间 JSON 作为排查证据。"""
    kept = _quarantine(path, bundle, report, phase=phase)
    staging.unlink(missing_ok=True)
    raise ExportVerifyError(
        f"Excel 回读校验未通过（{phase}），共 {len(report.diff)} 处差异，文件不予交付。"
        f"中间 JSON 已保留：{kept}",
        details={
            "plan_id": bundle.plan.plan_id,
            "phase": phase,
            "diff": list(report.diff[:20]),
            "diff_total": len(report.diff),
            "intermediate_json": str(kept),
        },
        suggestions=[
            "对照 diff 里的字段路径检查渲染逻辑与 validator/workbook.py 的回读契约",
            "确认 Sheet 4 区块7 是否每个架次都有一行（runway_id / is_recurrent 只在那里）",
            f"中间 JSON：{kept}",
        ],
    )


def export_workbook(path: Path, bundle: ReportBundle) -> ExportResult:
    """渲染 → 回读 → 通过才交付。不通过抛 :class:`ExportVerifyError`（FTS-5001）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(path.stem + STAGING_SUFFIX)

    render_workbook(staging, bundle, readback_passed=None)
    first = verify_workbook(staging, bundle.plan, ctx=bundle.ctx)
    if not first.passed:
        _fail(path, bundle, first, "第一遍回读", staging)

    render_workbook(staging, bundle, readback_passed=True)
    final = verify_workbook(staging, bundle.plan, ctx=bundle.ctx)
    if not final.passed:
        _fail(path, bundle, final, "第二遍回读（已填入回读结论的最终文件）", staging)

    staging.replace(path)
    return ExportResult(
        path=path,
        report=SchemaCheckReport(
            passed=True, diff=[], workbook_path=str(path), sheet_names=list(final.sheet_names)
        ),
        passes=2,
    )


def verify_existing(path: Path, bundle: ReportBundle) -> SchemaCheckReport:
    """对一个**已存在**的产物再跑一次闸门3（归档后的复核、回归比对用）。"""
    return verify_workbook(path, bundle.plan, ctx=bundle.ctx)


__all__ = [
    "FAILED_JSON_SUFFIX",
    "STAGING_SUFFIX",
    "ExportResult",
    "export_workbook",
    "verify_existing",
]
