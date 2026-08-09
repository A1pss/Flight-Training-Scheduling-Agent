"""抽取层：结构化表格 → 直接映射 Pydantic（**不过 LLM**，v6 §5.1）。

四份原始 PDF 全是规整表格，所以主路径一个 LLM 调用都没有 —— 这既是精度问题
也是可复现性问题（铁律 9）。LLM 只在「情况文件」这类自由文本上出场，走
:mod:`backend.ingestion.parsers.freetext` 的受约束解码。

每个 parser 只负责**如实抽取**：抽出什么就是什么，不做「顺手修一下」的容错。
值域、引用完整性、源内冲突分别由 :mod:`backend.ingestion.validate` 与
:mod:`backend.ingestion.conflicts` 负责 —— 让 parser 兼职裁决，冲突就永远
浮不上来（§5.5 X1 明确要求「不要在 parser 里悄悄选一个」）。
"""

from backend.ingestion.parsers.aircraft import parse_aircraft_document
from backend.ingestion.parsers.freetext import FREETEXT_SCHEMA, parse_situation_document
from backend.ingestion.parsers.missions import parse_missions_document
from backend.ingestion.parsers.personnel import parse_personnel_document
from backend.ingestion.parsers.rules_doc import parse_rules_document
from backend.ingestion.parsers.runways import parse_runways_from_semantics
from backend.ingestion.parsers.tables import (
    collect_tables,
    header_matches,
    row_to_mapping,
)

__all__ = [
    "FREETEXT_SCHEMA",
    "collect_tables",
    "header_matches",
    "parse_aircraft_document",
    "parse_missions_document",
    "parse_personnel_document",
    "parse_rules_document",
    "parse_runways_from_semantics",
    "parse_situation_document",
    "row_to_mapping",
]
