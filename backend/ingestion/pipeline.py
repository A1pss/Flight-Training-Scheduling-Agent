"""摄取管线编排（v6 §5.1 的那张流程图，逐段落地）。

```
上传文件
   ├─ 安全闸           safety.screen_file
   ├─ 文档分类器       classify.classify_document
   ├─ 格式适配层       adapters.extract_document
   ├─ 【修复层】       repair.*（在适配层内逐格执行）
   ├─ 抽取层           parsers.*
   ├─ 校验层           validate.validate_facts（含 X1 检出 + X3 断言 + 后置断言）
   ├─ Diff 层          diff.build_changeset
   ├─ 【人工确认】     gate.review
   └─ 落库             loader.persist_facts → chroma.upsert_chunks → 新 snapshot_id
```

分两个阶段，中间**必须**经过人工确认：

- :func:`prepare` —— 安全闸到 Diff，只读，不碰数据库的事实表
- :func:`commit` —— 拿到 `GateDecision` 后才落库

这个切分不是为了好看：v6 §5.1 的「人工确认」是**硬性门禁**，把它做成一个必须
显式传入的参数，就没法「先落库再说」。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.core.errors import IngestionError
from backend.core.logging import get_logger
from backend.ingestion.adapters import ExtractedDocument, extract_document
from backend.ingestion.chunkers import Chunk, chunk_entities, chunk_rules
from backend.ingestion.classify import classify_document
from backend.ingestion.conflicts import apply_x1_resolution
from backend.ingestion.diff import ChangeSet, build_changeset, content_sha256, normalize_facts
from backend.ingestion.gate import GateDecision, resolved_expiry_dates
from backend.ingestion.loader import (
    DEFAULT_CYCLE_START,
    activate_snapshot,
    active_snapshot_id,
    create_snapshot,
    load_snapshot_normalized,
    make_snapshot_id,
    persist_facts,
    record_audit,
    source_files_digest,
)
from backend.ingestion.parsers import (
    parse_aircraft_document,
    parse_missions_document,
    parse_personnel_document,
    parse_rules_document,
    parse_runways_from_semantics,
)
from backend.ingestion.safety import screen_file
from backend.ingestion.schema import IngestedFacts, SourceFile
from backend.ingestion.validate import ValidationOutcome, validate_facts
from backend.llm.provider import LLMProvider
from backend.memory.chroma import upsert_chunks
from backend.memory.embeddings import Embedder

logger = get_logger(__name__)


@dataclass
class PreparedIngestion:
    """`prepare` 的产物：抽好、校验过、diff 过，等人工确认。"""

    facts: IngestedFacts
    changeset: ChangeSet
    validation: ValidationOutcome
    documents: list[ExtractedDocument] = field(default_factory=list)
    snapshot_id: str = ""
    base_snapshot_id: str | None = None

    @property
    def content_sha256(self) -> str:
        return content_sha256(normalize_facts(self.facts))


@dataclass
class CommitResult:
    """`commit` 的产物。"""

    snapshot_id: str
    table_counts: dict[str, int]
    vector_counts: dict[str, int]
    applied_resolutions: dict[str, str] = field(default_factory=dict)


def _parse_one(
    doc: ExtractedDocument, doc_class: str, provider: LLMProvider | None
) -> IngestedFacts:
    """按分类结果分派到对应 parser。"""
    if doc_class == "人员档案":
        return IngestedFacts(persons=parse_personnel_document(doc))
    if doc_class == "飞机资源":
        fleet, airspaces = parse_aircraft_document(doc)
        return IngestedFacts(aircraft=fleet, airspaces=airspaces)
    if doc_class == "课目标准":
        return IngestedFacts(missions=parse_missions_document(doc))
    if doc_class == "规则条文":
        return IngestedFacts(rules=parse_rules_document(doc))
    if doc_class == "情况文件":
        if provider is None:
            raise IngestionError(
                f"情况文件 {doc.path.name} 需要 LLM 受约束解码，但未提供 Provider",
                details={"path": str(doc.path)},
            )
        # 情况文件产出的是事件而非实体，由调用方（W9 的 UI）转成扰动录入；
        # 这里先确保它能被解析、schema 合法，解析失败即阻断。
        from backend.ingestion.parsers.freetext import parse_situation_document

        parse_situation_document(doc, provider)
        return IngestedFacts()
    raise IngestionError(
        f"文档 {doc.path.name} 分类为「{doc_class}」，没有对应的抽取器",
        details={"path": str(doc.path), "doc_class": doc_class},
        suggestions=["若确为新类型文档，需先在 classify.RULE_SIGNATURES 里登记"],
    )


def prepare(
    paths: Sequence[Path],
    *,
    session: Session | None = None,
    provider: LLMProvider | None = None,
    include_runways: bool = True,
    check_counts: bool = True,
) -> PreparedIngestion:
    """安全闸 → 分类 → 适配 → 修复 → 抽取 → 校验 → Diff。**不落库。**"""
    facts = IngestedFacts()
    documents: list[ExtractedDocument] = []
    sources: list[SourceFile] = []

    for path in paths:
        safe = screen_file(path)
        doc = extract_document(safe.path, safe.media_type)
        documents.append(doc)
        classification = classify_document(doc.text, provider)
        logger.info(
            "文档分类完成",
            filename=path.name,
            doc_class=classification.doc_class,
            by=classification.by,
        )
        sources.append(
            SourceFile(
                path=str(path),
                filename=path.name,
                sha256=safe.sha256,
                size_bytes=safe.size_bytes,
                media_type=safe.media_type,
                doc_class=classification.doc_class,
                classifier=classification.by,  # type: ignore[arg-type]
                pages=doc.page_count,
            )
        )
        facts = facts.merged_with(_parse_one(doc, classification.doc_class, provider))

    if include_runways:
        facts = facts.merged_with(IngestedFacts(runways=parse_runways_from_semantics()))
    facts = facts.model_copy(update={"sources": tuple(sources)})

    doc_texts = [(d.path.name, d.text) for d in documents]
    validation = validate_facts(facts, doc_texts, check_counts=check_counts)

    base_id = active_snapshot_id(session) if session is not None else None
    current = (
        load_snapshot_normalized(session, base_id)
        if session is not None and base_id is not None
        else None
    )
    changeset = build_changeset(
        facts, current, conflicts=validation.conflicts, base_snapshot_id=base_id
    )

    logger.info("摄取准备完成", snapshot_id=make_snapshot_id(facts), **changeset.summary())
    return PreparedIngestion(
        facts=facts,
        changeset=changeset,
        validation=validation,
        documents=documents,
        snapshot_id=make_snapshot_id(facts),
        base_snapshot_id=base_id,
    )


def build_chunks(facts: IngestedFacts, *, snapshot_id: str, ruleset_version: str) -> list[Chunk]:
    """把事实切成待向量化的 chunk（规则原文 + 实体摘要句）。"""
    return [
        *chunk_rules(facts.rules, ruleset_version=ruleset_version),
        *chunk_entities(facts, snapshot_id=snapshot_id),
    ]


def commit(
    prepared: PreparedIngestion,
    decision: GateDecision,
    session: Session,
    *,
    ruleset_version: str,
    cycle_start: date = DEFAULT_CYCLE_START,
    embedder: Embedder | None = None,
    write_vectors: bool = True,
) -> CommitResult:
    """人工确认通过后落库：PG → Chroma → 新 snapshot_id。

    **顺序不能反**：Chroma 的 `field_map` 回指 PG 主键，先写向量后写事实会留下
    指向不存在记录的向量。
    """
    if not decision.approved:
        raise IngestionError(
            "人工确认未通过，拒绝落库：" + "；".join(decision.reasons),
            details={"outcome": decision.outcome, "reasons": decision.reasons},
        )

    # ① 应用 X1 类裁决 —— 到这一步才允许改值，parser 里绝不允许（§5.5）
    facts = prepared.facts
    applied: dict[str, str] = {}
    expiry_resolutions = resolved_expiry_dates(decision, prepared.changeset)
    if expiry_resolutions:
        persons = []
        for person in facts.persons:
            updated = person
            for (person_id, mission_class), value in expiry_resolutions.items():
                if person.person_id == person_id:
                    updated = apply_x1_resolution(updated, mission_class, value)
                    applied[f"{person_id}:{mission_class}:expiry"] = value.isoformat()
            persons.append(updated)
        facts = facts.model_copy(update={"persons": tuple(persons)})

    # 裁决改了内容 → snapshot_id 必须跟着变（它由内容哈希决定，铁律 9）
    snapshot_id = make_snapshot_id(facts)

    # ② PG
    create_snapshot(session, facts, snapshot_id=snapshot_id, status="PENDING")
    table_counts = persist_facts(session, snapshot_id, facts)
    from backend.ingestion.loader import materialize_training_progress

    if cycle_start != DEFAULT_CYCLE_START:
        table_counts["training_progress"] = materialize_training_progress(
            session, snapshot_id, facts, cycle_start=cycle_start
        )

    record_audit(
        session,
        actor=decision.approved_by,
        action="ingest.commit",
        resource_type="data_snapshot",
        resource_id=snapshot_id,
        before={"base_snapshot_id": prepared.base_snapshot_id},
        after={
            "changeset": prepared.changeset.summary(),
            "resolutions": applied,
            "sources_digest": source_files_digest(facts.sources),
        },
    )
    activate_snapshot(
        session,
        snapshot_id,
        confirmed_by=decision.approved_by,
        note=f"由 {decision.approved_by} 于人工确认门禁批准",
    )

    # ③ Chroma
    vector_counts: dict[str, int] = {}
    if write_vectors:
        vector_counts = upsert_chunks(
            build_chunks(facts, snapshot_id=snapshot_id, ruleset_version=ruleset_version),
            embedder=embedder,
        )

    logger.info(
        "摄取落库完成",
        snapshot_id=snapshot_id,
        tables=table_counts,
        vectors=vector_counts,
    )
    return CommitResult(
        snapshot_id=snapshot_id,
        table_counts=table_counts,
        vector_counts=vector_counts,
        applied_resolutions=applied,
    )


def snapshot_manifest(prepared: PreparedIngestion) -> dict[str, Any]:
    """给报告/UI 用的摘要。"""
    return {
        "snapshot_id": prepared.snapshot_id,
        "base_snapshot_id": prepared.base_snapshot_id,
        "content_sha256": prepared.content_sha256,
        "changeset": prepared.changeset.summary(),
        "conflicts": [
            {
                "conflict_id": c.conflict_id,
                "kind": c.kind,
                "severity": c.severity,
                "value_a": c.value_a,
                "value_b": c.value_b,
                "adjudicated_value": c.adjudicated_value,
            }
            for c in prepared.changeset.conflicts
        ],
        "sources": [
            {"filename": s.filename, "sha256": s.sha256, "doc_class": s.doc_class}
            for s in prepared.facts.sources
        ],
    }


__all__ = [
    "CommitResult",
    "PreparedIngestion",
    "build_chunks",
    "commit",
    "prepare",
    "snapshot_manifest",
]
