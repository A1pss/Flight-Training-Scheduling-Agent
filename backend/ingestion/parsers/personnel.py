"""`personnel.pdf` → :class:`IngestedPerson`。

两张表：

- **一、人员资质总表** —— 编号 / 姓名 / 身份 / 机型资质 / 已完成课目 /
  复训到期 / 不可用日期。「已完成课目」列横跨 5~6 个物理行且课目编号被硬换行
  截断（`mis` / `sionB-1` / `missio` / `nH-1`），全靠修复层。
- **二、课目级资质明细**（跨第 1、2 页）—— 形如
  `A类/单飞;B类/带飞;C类/带飞;F类/带飞`，刘斌的 C 类多一段 `/至2026-02-07`。

⚠️ **X1 就在这两张表之间**：总表「复训到期」写 2026-01-07，明细表写
2026-02-07。本 parser **两边都如实记下来**，绝不在这里挑一个 ——
检出与裁定由 :mod:`backend.ingestion.conflicts` 负责（§5.5 强制要求）。
"""

from __future__ import annotations

import re
from datetime import date

from backend.core.errors import IngestionError
from backend.ingestion.adapters import ExtractedDocument
from backend.ingestion.parsers.tables import collect_tables, require_header, row_to_mapping
from backend.ingestion.repair import extract_mission_tokens, is_null_token, split_list
from backend.ingestion.schema import IngestedPerson, IngestedQualification

#: 总表表头签名
SUMMARY_SIGNATURE = ("编号", "姓名", "身份", "机型资质", "已完成课目", "复训到期", "不可用日期")
#: 课目级资质明细表头签名
DETAIL_SIGNATURE = ("编号", "姓名", "资质(课目类/等级/到期日)")

#: 明细表里的一条资质：`C类/单飞/至2026-02-07` 或 `A类/教员`
_QUAL_RE = re.compile(r"([A-H])类\s*/\s*(教员|单飞|带飞)(?:\s*/\s*至\s*(\d{4}-\d{2}-\d{2}))?")
#: 总表「复训到期」列：`仪表等级(C类):2026-01-07`
_RECURRENT_DUE_RE = re.compile(r"[（(]([A-H])类[）)]\s*[:：]\s*(\d{4}-\d{2}-\d{2})")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_recurrent_due(cell: str) -> tuple[str, date] | None:
    """解析总表「复训到期」列，返回 (课目类别, 到期日)。无值返回 ``None``。"""
    if is_null_token(cell):
        return None
    match = _RECURRENT_DUE_RE.search(cell)
    if not match:
        raise IngestionError(
            f"「复训到期」列无法解析：{cell!r}",
            details={"cell": cell, "expected": _RECURRENT_DUE_RE.pattern},
        )
    return match.group(1), date.fromisoformat(match.group(2))


def parse_qualifications(person_id: str, cell: str) -> tuple[IngestedQualification, ...]:
    """解析课目级资质明细单元格。"""
    if is_null_token(cell):
        return ()
    matches = list(_QUAL_RE.finditer(cell))
    if not matches:
        raise IngestionError(
            f"{person_id} 的资质明细无法解析：{cell!r}",
            details={"person_id": person_id, "cell": cell},
        )
    # 把未被任何 match 覆盖的残留文本挑出来——静默丢弃残留等于「部分入库」（铁律 7）
    residue = _QUAL_RE.sub("", cell)
    residue = re.sub(r"[;；\s]", "", residue)
    if residue:
        raise IngestionError(
            f"{person_id} 的资质明细有无法解析的残留片段：{residue!r}",
            details={"person_id": person_id, "cell": cell, "residue": residue},
        )

    return tuple(
        IngestedQualification(
            person_id=person_id,
            mission_class=m.group(1),  # type: ignore[arg-type]
            level=m.group(2),  # type: ignore[arg-type]
            expiry_date=date.fromisoformat(m.group(3)) if m.group(3) else None,
        )
        for m in matches
    )


def parse_personnel_document(doc: ExtractedDocument) -> tuple[IngestedPerson, ...]:
    """`personnel.pdf` 主入口。"""
    summary_header = require_header(doc, SUMMARY_SIGNATURE)
    detail_header = require_header(doc, DETAIL_SIGNATURE)

    summary_rows = collect_tables(doc, SUMMARY_SIGNATURE)
    detail_rows = collect_tables(doc, DETAIL_SIGNATURE)

    details: dict[str, str] = {}
    for row in detail_rows:
        rec = row_to_mapping(detail_header, row)
        person_id = rec["编号"].strip()
        if not person_id or person_id == "编号":
            continue
        details[person_id] = rec["资质(课目类/等级/到期日)"]

    persons: list[IngestedPerson] = []
    for row in summary_rows:
        rec = row_to_mapping(summary_header, row)
        person_id = rec["编号"].strip()
        if not person_id or person_id == "编号":
            continue

        if person_id not in details:
            raise IngestionError(
                f"{person_id} 在总表出现但课目级资质明细里没有对应记录",
                details={"person_id": person_id, "detail_ids": sorted(details)},
            )

        unavailable = tuple(
            date.fromisoformat(d) for d in _DATE_RE.findall(rec["不可用日期"] or "")
        )
        persons.append(
            IngestedPerson(
                person_id=person_id,
                name=rec["姓名"].strip(),
                identity=rec["身份"].strip(),  # type: ignore[arg-type]
                aircraft_types=tuple(split_list(rec["机型资质"])),  # type: ignore[arg-type]
                completed_missions=tuple(extract_mission_tokens(rec["已完成课目"])),
                unavailable_dates=unavailable,
                qualifications=parse_qualifications(person_id, details[person_id]),
                recurrent_due_raw=rec["复训到期"].strip()
                if not is_null_token(rec["复训到期"])
                else "",
            )
        )

    if not persons:
        raise IngestionError("人员总表未抽出任何记录", details={"path": str(doc.path)})
    return tuple(persons)


__all__ = [
    "DETAIL_SIGNATURE",
    "SUMMARY_SIGNATURE",
    "parse_personnel_document",
    "parse_qualifications",
    "parse_recurrent_due",
]
