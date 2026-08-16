"""三路召回共用的文档类型（v6 §6.5.2 / §6.5.4）。

三路召回各有各的产物形态 —— 路 A 是 SQL 行、路 B 是 BM25 打分、路 C 是向量
距离。**融合之前必须先统一形状**，否则 RRF 拿到的是三种不同的东西。

## `authoritative` 这一位是本模块的要害

v6 §6.5.4：

> 结构化召回（路 A）的结果具有权威性，**不参与 RRF 竞争，直接置顶**。

所以「是不是路 A 来的」不能靠 `source_kind == "structured"` 这种字符串比较散落
在各处判断，它是文档自身的一个属性，融合器只认这一位。理由在 §6.5.4 写得很直白：
人员资质、维修窗口、空域容量来自 PG，是唯一真源；让一条向量召回的旧摘要排在
SQL 精确结果之上，就可能拿过期信息回答。

## `citation()` 为什么带 `table` / `pk`

路 A 的每条结果都要能回答「这个数是从哪张表哪一行读出来的」。生成层的事实
核验（§6.5.2 第 ④ 步）据此判断一条断言有没有出处，验收时人也据此复核。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from backend.schemas.retrieval import Citation

#: 召回来源。`structured` = 路 A，`bm25` = 路 B，`vector` = 路 C，
#: `memory` = 三类记忆里走精确 key 命中的那一路（也算结构化，见下）。
SourceKind = Literal["structured", "bm25", "vector", "memory"]

#: 具有权威性、不参与 RRF 竞争的来源（v6 §6.5.4）。
#: `memory` 里的**语义记忆**同样来自 PG 事实表，与路 A 同源，所以也在其中；
#: 情景/程序记忆走的是向量与 key 前缀，由 `authoritative=False` 的文档承载。
AUTHORITATIVE_KINDS: Final[frozenset[str]] = frozenset({"structured"})


@dataclass(frozen=True)
class RetrievedDoc:
    """一条召回结果。三路统一形状。"""

    doc_id: str
    text: str
    source_kind: SourceKind
    #: 权威来源直接置顶，不参与 RRF（v6 §6.5.4）
    authoritative: bool = False
    #: 该路自己的原始分数（BM25 得分 / 1-距离）。RRF 只用排名，这个值仅供展示
    score: float | None = None
    #: 结构化来源填 `{"table": ..., "pk": {...}}`；向量来源填 Chroma metadata
    metadata: Mapping[str, Any] = field(default_factory=dict)
    #: 该文档的时效区间（§6.4）。`None` 表示不带时效（如规则原文）
    valid_from: str | None = None
    valid_to: str | None = None
    #: 同 key 的历史版本数（§6.4：返回最新有效版本 + 显式标注历史版本数量）
    superseded_count: int = 0

    def citation(self) -> Citation:
        """转成对外的引用契约。"""
        return Citation(
            source_kind=self.source_kind,
            source_id=self.doc_id,
            snippet=self.text[:400],
            score=self.score,
        )

    def with_score(self, score: float) -> RetrievedDoc:
        return RetrievedDoc(
            doc_id=self.doc_id,
            text=self.text,
            source_kind=self.source_kind,
            authoritative=self.authoritative,
            score=score,
            metadata=self.metadata,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            superseded_count=self.superseded_count,
        )


def structured_doc(
    doc_id: str,
    text: str,
    *,
    table: str,
    pk: Mapping[str, Any],
    valid_from: str | None = None,
    valid_to: str | None = None,
    superseded_count: int = 0,
    extra: Mapping[str, Any] | None = None,
) -> RetrievedDoc:
    """造一条路 A 文档。**`authoritative` 恒为 True**，调用方无从关掉。

    做成工厂函数而不是让调用方自己填那一位，是因为它决定融合行为：
    某个 handler 忘了置位，那条 SQL 精确结果就会掉进 RRF 里和一条旧摘要
    比排名 —— 而这正是 §6.5.4 要杜绝的那件事。
    """
    metadata: dict[str, Any] = {"table": table, "pk": dict(pk)}
    if extra:
        metadata.update(extra)
    return RetrievedDoc(
        doc_id=doc_id,
        text=text,
        source_kind="structured",
        authoritative=True,
        metadata=metadata,
        valid_from=valid_from,
        valid_to=valid_to,
        superseded_count=superseded_count,
    )


def dedupe(docs: Sequence[RetrievedDoc]) -> list[RetrievedDoc]:
    """按 `doc_id` 去重，**保留第一次出现的那条**。

    顺序敏感：调用方按优先级排好再传进来（路 A 在前），于是同一份内容被
    两路召回时留下的是权威的那一条。
    """
    seen: set[str] = set()
    out: list[RetrievedDoc] = []
    for doc in docs:
        if doc.doc_id in seen:
            continue
        seen.add(doc.doc_id)
        out.append(doc)
    return out


__all__ = [
    "AUTHORITATIVE_KINDS",
    "RetrievedDoc",
    "SourceKind",
    "dedupe",
    "structured_doc",
]
