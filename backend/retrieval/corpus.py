"""可检索语料：路 B（BM25）与路 C（向量）看的**同一批文档**。

## 为什么两路必须共用一个语料

RRF 融合的是**排名**（§6.5.4）。两路排的若不是同一批文档，融合出来的名次就
没有可比性 —— 一个文档在 BM25 里排第 1、在向量里根本不存在，`1/(k+rank)`
的加和会悄悄偏向文档多的那一路。所以语料在这里构建一次，两路各自建索引。

## 语料从哪来

| 来源 | collection | 内容 |
|---|---|---|
| PG 事实表 | `entity_summaries` | 人员 / 飞机 / 课目 / 空域 / 跑道的摘要句 |
| `rules/ruleset_v1.3.yaml` | `rule_texts` | 14 条规则的条文，**一条一个文档、不拆分** |
| `episodic_memories` | `episodic_summaries` | 历次会话、用户修改与驳回、松弛档选择 |

**摘要句是索引，不是事实。** 权威值一律回 PG 取（路 A），这与 §6.1 里
「PG 是事实唯一真源，Chroma 只是索引」是同一条原则。所以本模块的措辞与
`ingestion/chunkers.py` 的摘要句不必逐字相同 —— 两者都是同一份 PG 事实的
索引文本，字面差异不影响任何答案的正确性（答案的内容来自路 A）。

## 规则条文为什么整条进、不切

`ingestion/chunkers.py::chunk_rules` 已经写过一次理由，这里同样适用：
「连续飞行 2 架次后」和「第 3 架次前休息不少于 30 分钟」分开召回，
会让解释报告说出完全错误的话。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.ruleset import Ruleset, get_ruleset
from backend.memory import semantic
from backend.memory.collections import (
    COLLECTION_ENTITIES,
    COLLECTION_EPISODIC,
    COLLECTION_RULES,
)
from backend.models.memory import EpisodicMemory

#: 语料条目的 id 前缀，按来源分。**前缀即命名空间**，两路索引都靠它去重。
ID_PREFIXES: Final[dict[str, str]] = {
    COLLECTION_ENTITIES: "ent",
    COLLECTION_RULES: "rule",
    COLLECTION_EPISODIC: "epi",
}


@dataclass(frozen=True)
class CorpusDoc:
    """语料里的一条文档。"""

    doc_id: str
    text: str
    collection: str
    metadata: dict[str, Any] = field(default_factory=dict)
    #: 时效（§6.4）。规则原文与实体摘要不带时效，情景记忆带
    valid_from: str | None = None
    valid_to: str | None = None
    archived: bool = False


@dataclass(frozen=True)
class Corpus:
    """一批文档 + 按 id 的索引。**顺序固定**（按 doc_id 排序），铁律 9。"""

    docs: tuple[CorpusDoc, ...]

    def __len__(self) -> int:
        return len(self.docs)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(d.doc_id for d in self.docs)

    def get(self, doc_id: str) -> CorpusDoc | None:
        return self._index.get(doc_id)

    @property
    def _index(self) -> dict[str, CorpusDoc]:
        return {d.doc_id: d for d in self.docs}

    def filter(self, *, collections: Sequence[str] = (), include_archived: bool = False) -> Corpus:
        """按 collection 与归档位筛一份子语料。

        `include_archived=False` 是**默认召回**的形态（§6.4 遗忘策略：
        归档条目仍可检索，但不参与默认召回）。
        """
        wanted = frozenset(collections) if collections else None
        return Corpus(
            docs=tuple(
                d
                for d in self.docs
                if (wanted is None or d.collection in wanted)
                and (include_archived or not d.archived)
            )
        )


def entity_docs(session: Session, snapshot_id: str) -> list[CorpusDoc]:
    """PG 事实 → 实体摘要句。"""
    docs: list[CorpusDoc] = []
    prefix = ID_PREFIXES[COLLECTION_ENTITIES]
    for person in semantic.all_persons(session, snapshot_id):
        docs.append(
            CorpusDoc(
                doc_id=f"{prefix}:person:{person.person_id}",
                text=person.sentence(),
                collection=COLLECTION_ENTITIES,
                metadata={
                    "entity_type": "person",
                    "entity_id": person.person_id,
                    "snapshot_id": snapshot_id,
                    "field_map": {"table": "persons", "pk": {"person_id": person.person_id}},
                },
            )
        )
    for plane in semantic.all_aircraft(session, snapshot_id):
        docs.append(
            CorpusDoc(
                doc_id=f"{prefix}:aircraft:{plane.aircraft_id}",
                text=plane.sentence(),
                collection=COLLECTION_ENTITIES,
                metadata={
                    "entity_type": "aircraft",
                    "entity_id": plane.aircraft_id,
                    "snapshot_id": snapshot_id,
                    "field_map": {"table": "aircraft", "pk": {"aircraft_id": plane.aircraft_id}},
                },
            )
        )
    for mission in semantic.all_missions(session, snapshot_id):
        docs.append(
            CorpusDoc(
                doc_id=f"{prefix}:mission:{mission.mission_id}",
                text=mission.sentence(),
                collection=COLLECTION_ENTITIES,
                metadata={
                    "entity_type": "mission",
                    "entity_id": mission.mission_id,
                    "snapshot_id": snapshot_id,
                    "field_map": {"table": "missions", "pk": {"mission_id": mission.mission_id}},
                },
            )
        )
    for airspace in semantic.airspace_facts(session, snapshot_id):
        docs.append(
            CorpusDoc(
                doc_id=f"{prefix}:airspace:{airspace.airspace_id}",
                text=airspace.sentence(),
                collection=COLLECTION_ENTITIES,
                metadata={
                    "entity_type": "airspace",
                    "entity_id": airspace.airspace_id,
                    "snapshot_id": snapshot_id,
                    "field_map": {
                        "table": "airspaces",
                        "pk": {"airspace_id": airspace.airspace_id},
                    },
                },
            )
        )
    return docs


def rule_docs(ruleset: Ruleset | None = None) -> list[CorpusDoc]:
    """规则条文 → 每条一个文档，**不切分**。"""
    rules = ruleset or get_ruleset()
    prefix = ID_PREFIXES[COLLECTION_RULES]
    return [
        CorpusDoc(
            doc_id=f"{prefix}:{rules.version}:{rule_id:02d}",
            text=f"约束{rule_id}·{spec.title}（{spec.tier}，{spec.kind}）：{spec.statement}",
            collection=COLLECTION_RULES,
            metadata={
                "rule_id": rule_id,
                "hard_soft": spec.kind,
                "ruleset_version": rules.version,
                "title": spec.title,
            },
        )
        for rule_id, spec in sorted(rules.rules.items())
    ]


def episodic_docs(session: Session, *, include_archived: bool = True) -> list[CorpusDoc]:
    """情景记忆 → 摘要文档（§6.2）。

    **默认把归档的也取出来**：`Corpus.filter()` 才是决定「参不参与默认召回」
    的地方。语料里带着它们，是为了让「显式要求查归档」的路径不必重建语料。
    """
    prefix = ID_PREFIXES[COLLECTION_EPISODIC]
    rows = list(session.scalars(select(EpisodicMemory).order_by(EpisodicMemory.memory_id)))
    return [
        CorpusDoc(
            doc_id=f"{prefix}:{row.memory_id}",
            text=row.summary,
            collection=COLLECTION_EPISODIC,
            metadata={
                "memory_id": row.memory_id,
                "session_id": row.session_id,
                "kind": row.kind,
                "valid_from": row.valid_from.isoformat(),
                "archived": bool(row.archived),
            },
            valid_from=row.valid_from.isoformat(),
            valid_to=row.valid_to.isoformat() if row.valid_to is not None else None,
            archived=bool(row.archived),
        )
        for row in rows
        if include_archived or not row.archived
    ]


def build_corpus(
    session: Session,
    snapshot_id: str,
    *,
    ruleset: Ruleset | None = None,
    with_episodic: bool = True,
) -> Corpus:
    """组装全量语料。**按 doc_id 排序** —— 顺序固定是可复现性的前提。"""
    docs: list[CorpusDoc] = []
    docs.extend(entity_docs(session, snapshot_id))
    docs.extend(rule_docs(ruleset))
    if with_episodic:
        docs.extend(episodic_docs(session))
    return Corpus(docs=tuple(sorted(docs, key=lambda d: d.doc_id)))


__all__ = [
    "ID_PREFIXES",
    "Corpus",
    "CorpusDoc",
    "build_corpus",
    "entity_docs",
    "episodic_docs",
    "rule_docs",
]
