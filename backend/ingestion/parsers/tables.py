"""表格定位与取值的公共工具。

**跨页表格必须合并**：`personnel.pdf` 的课目级资质明细横跨第 1、2 页，
pdfplumber 会返回两张表、两个表头。按表头签名归并是唯一靠谱的做法 ——
按「页码相邻」猜会在文档改版时错得很难看。
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.core.errors import IngestionError
from backend.ingestion.adapters import ExtractedDocument, ExtractedTable
from backend.ingestion.repair import aggregate_rows


def header_matches(header: Sequence[str], signature: Sequence[str]) -> bool:
    """表头是否包含签名里的全部列名（顺序无关、允许表头有额外列）。"""
    cells = {c.strip() for c in header if c.strip()}
    return all(col in cells for col in signature)


def collect_tables(
    doc: ExtractedDocument, signature: Sequence[str], *, key_index: int = 0
) -> list[list[str]]:
    """收集全文档中表头匹配 `signature` 的表，合并表体并做跨行聚合。

    合并时**逐表剥掉表头**（跨页续表会重复表头），再对合并后的表体统一做
    「主键列非空」聚合（v6 §1.5）。
    """
    matched: list[ExtractedTable] = [
        t for t in doc.tables if t.rows and header_matches(t.header, signature)
    ]
    if not matched:
        raise IngestionError(
            f"文档 {doc.path.name} 中未找到表头含 {list(signature)} 的表",
            details={
                "path": str(doc.path),
                "signature": list(signature),
                "found_headers": [list(t.header) for t in doc.tables],
            },
            suggestions=["确认文档版式未变；若列名变更需同步更新 parser 的表头签名"],
        )

    body: list[list[str]] = []
    for table in matched:
        body.extend(list(row) for row in table.body)
    return aggregate_rows(body, key_index=key_index)


def row_to_mapping(header: Sequence[str], row: Sequence[str]) -> dict[str, str]:
    """按表头把一行映射成字典，短行补空串。"""
    return {
        col.strip(): (row[i] if i < len(row) else "") for i, col in enumerate(header) if col.strip()
    }


def require_header(doc: ExtractedDocument, signature: Sequence[str]) -> tuple[str, ...]:
    """取回匹配签名的第一张表的表头（用于 `row_to_mapping`）。"""
    for table in doc.tables:
        if table.rows and header_matches(table.header, signature):
            return table.header
    raise IngestionError(
        f"文档 {doc.path.name} 中未找到表头含 {list(signature)} 的表",
        details={"path": str(doc.path), "signature": list(signature)},
    )


__all__ = ["collect_tables", "header_matches", "require_header", "row_to_mapping"]
