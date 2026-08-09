"""文档摄取管线（v6 §5）。

两条铁律在本包里是硬编码的行为，不是注释：

- **铁律 7 —— 抽取失败绝不静默降级**：宁可抛 `IngestionError`（FTS-1003）阻断，
  也不让 `sionB-1` 这类脏 token 进库；不做「尽力而为的部分入库」。
- **§5.1 人工确认是硬性门禁**：:func:`~backend.ingestion.pipeline.commit` 必须
  显式收到一个通过的 `GateDecision` 才落库，没有旁路。

模块地图：

| 模块 | 职责 | v6 落点 |
|---|---|---|
| :mod:`~backend.ingestion.safety` | 扩展名白名单 / MIME 嗅探 / 50MB / 压缩炸弹 | §5.1 |
| :mod:`~backend.ingestion.classify` | 六类文档分类（规则优先 + LLM 兜底） | §5.1 |
| :mod:`~backend.ingestion.adapters` | pdfplumber / pandas / docx / PaddleOCR | §5.1 |
| :mod:`~backend.ingestion.repair` | 断词修复 / 跨行聚合 / 全半角归一 | §1.5、§5.2 |
| :mod:`~backend.ingestion.parsers` | 结构化表格 → Pydantic（不过 LLM） | §5.1 |
| :mod:`~backend.ingestion.conflicts` | X1~X4 检出与裁定映射 | §5.5 |
| :mod:`~backend.ingestion.validate` | 引用完整性 / 值域 / 时间逻辑 / 后置断言 | §5.1、§3.1.1 |
| :mod:`~backend.ingestion.chunkers` | 五种自适应 chunk 策略 | §5.3 |
| :mod:`~backend.ingestion.prompts` | `<untrusted_document>` 隔离 + 受约束解码 | §5.4 |
| :mod:`~backend.ingestion.diff` | ChangeSet（新增/修改/删除/冲突） | §5.1 |
| :mod:`~backend.ingestion.gate` | 人工确认门禁 | §5.1 |
| :mod:`~backend.ingestion.loader` | PG → Chroma → 新 snapshot_id | §5.1、§6.3 |
| :mod:`~backend.ingestion.pipeline` | 两阶段编排（prepare / commit） | §5.1 |
"""

from backend.ingestion.conflicts import ADJUDICATIONS, Conflict, detect_all
from backend.ingestion.diff import ChangeSet, build_changeset, content_sha256, normalize_facts
from backend.ingestion.gate import ConflictResolution, GateDecision, baseline_resolutions, review
from backend.ingestion.pipeline import CommitResult, PreparedIngestion, commit, prepare
from backend.ingestion.repair import (
    TOKEN_PATTERNS,
    assert_no_orphan_tokens,
    repair_cell,
    repair_linebreaks,
    repair_text,
)
from backend.ingestion.safety import screen_file
from backend.ingestion.schema import IngestedFacts
from backend.ingestion.validate import validate_facts

__all__ = [
    "ADJUDICATIONS",
    "TOKEN_PATTERNS",
    "ChangeSet",
    "CommitResult",
    "Conflict",
    "ConflictResolution",
    "GateDecision",
    "IngestedFacts",
    "PreparedIngestion",
    "assert_no_orphan_tokens",
    "baseline_resolutions",
    "build_changeset",
    "commit",
    "content_sha256",
    "detect_all",
    "normalize_facts",
    "prepare",
    "repair_cell",
    "repair_linebreaks",
    "repair_text",
    "review",
    "screen_file",
    "validate_facts",
]
