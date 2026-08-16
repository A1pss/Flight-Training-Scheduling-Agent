"""路 C · 稠密召回：Chroma 向量（bge-m3）。

> **语义描述**（v6 §6.5.1）：「为什么上周把编队飞行推迟了」这类问题只有这一路
> 能答 —— 它既没有可精确匹配的实体，也没有能命中 BM25 的编号。

## 两个后端，同一套语义

| 后端 | 何时用 | 说明 |
|---|---|---|
| :class:`ChromaIndex` | 生产路径 | v6 §6.1 指定的向量库，嵌入式部署、离线友好 |
| :class:`InMemoryIndex` | 单测 | 精确余弦，零外部依赖 |

两者的检索语义**必须一致**（都是 L2 归一化向量上的余弦），否则单测绿、
真机红。所以嵌入一律走 `memory.embeddings`（那里已经 `normalize_embeddings=True`），
本模块不自己算归一化。

## 「同时检索改写后与原始查询，取并集」

v6 §6.5.3 末段的硬要求：

> **改写结果保留原查询**：三路召回中，向量路同时检索改写后与原始查询，取并集。
> 改写有时会丢失原句的细微语义。

落点是 :meth:`VectorIndex.search_many` —— 它按查询逐个检索再取并集，
**同一文档取最高分那次**。不是简单拼接：拼接会让同一篇文档在 RRF 里占两个名次。

## ⚠️ 顺序陷阱（M1 实测，本模块沿用）

必须**先把全部向量算完，再创建 Chroma client**。反过来会在本机让进程直接
`Segmentation fault`（chromadb 与 torch 的原生运行时在同一进程里初始化打架）。
:meth:`ChromaIndex.build` 因此严格按「先 embed、后 build_client」的顺序写。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from backend.core.logging import get_logger
from backend.memory.chroma import build_client, init_collections
from backend.memory.embeddings import Embedder, build_embedder
from backend.retrieval.corpus import Corpus, CorpusDoc
from backend.retrieval.documents import RetrievedDoc

logger = get_logger(__name__)


def _to_doc(doc: CorpusDoc, similarity: float) -> RetrievedDoc:
    return RetrievedDoc(
        doc_id=doc.doc_id,
        text=doc.text,
        source_kind="vector",
        authoritative=False,
        score=round(similarity, 6),
        metadata=dict(doc.metadata),
        valid_from=doc.valid_from,
        valid_to=doc.valid_to,
    )


class VectorIndex(ABC):
    """向量索引的公共形状。"""

    corpus: Corpus

    @abstractmethod
    def search(self, query: str, *, top_k: int = 10) -> list[RetrievedDoc]:
        """单查询检索。"""

    def search_many(self, queries: Sequence[str], *, top_k: int = 10) -> list[RetrievedDoc]:
        """多查询检索并取并集（v6 §6.5.3：改写后 + 原始，取并集）。

        同一文档被多个查询命中时**保留最高相似度那次**，且只占一个名次。
        排序按 (−相似度, doc_id)，并列可复现。
        """
        best: dict[str, RetrievedDoc] = {}
        for query in queries:
            if not query.strip():
                continue
            for doc in self.search(query, top_k=top_k):
                previous = best.get(doc.doc_id)
                if previous is None or (doc.score or 0.0) > (previous.score or 0.0):
                    best[doc.doc_id] = doc
        return sorted(best.values(), key=lambda d: (-(d.score or 0.0), d.doc_id))[:top_k]


class InMemoryIndex(VectorIndex):
    """精确余弦检索。**给单测用**，语义与 Chroma 一致。"""

    def __init__(self, corpus: Corpus, embedder: Embedder | None = None) -> None:
        self.corpus = corpus
        self._embedder = embedder or build_embedder()
        texts = [d.text for d in corpus.docs]
        self._vectors = self._embedder.embed(texts) if texts else []

    def search(self, query: str, *, top_k: int = 10) -> list[RetrievedDoc]:
        if not self._vectors:
            return []
        vector = self._embedder.embed([query])[0]
        scored = [
            (doc, _cosine(vector, row))
            for doc, row in zip(self.corpus.docs, self._vectors, strict=True)
        ]
        scored.sort(key=lambda pair: (-pair[1], pair[0].doc_id))
        return [_to_doc(doc, sim) for doc, sim in scored[:top_k]]


class ChromaIndex(VectorIndex):
    """Chroma 后端（v6 §6.1 指定）。

    `build()` 是**幂等**的：文档 id 由语料决定，重复 upsert 覆盖同一行。
    这样「摄取写过一遍、检索侧再补一遍」不会产生重复条目。
    """

    def __init__(self, corpus: Corpus, client: Any, embedder: Embedder) -> None:
        self.corpus = corpus
        self._client = client
        self._embedder = embedder

    @classmethod
    def build(
        cls,
        corpus: Corpus,
        *,
        client: Any = None,
        embedder: Embedder | None = None,
    ) -> ChromaIndex:
        """把语料写进 Chroma 并返回索引。

        ⚠️ **先算向量再建 client** —— 见模块开头的「顺序陷阱」，改了会段错误。
        """
        emb = embedder or build_embedder()
        grouped: dict[str, list[CorpusDoc]] = {}
        for doc in corpus.docs:
            grouped.setdefault(doc.collection, []).append(doc)
        # ① 先算向量（此时进程里还没有 Chroma 的原生运行时）
        vectors = {
            name: emb.embed([d.text for d in items]) for name, items in sorted(grouped.items())
        }
        # ② 再建 client / collection
        api = client or build_client()
        collections = init_collections(api, embedder=emb)
        for name, items in sorted(grouped.items()):
            collections[name].upsert(
                ids=[d.doc_id for d in items],
                documents=[d.text for d in items],
                embeddings=vectors[name],
                metadatas=[_flatten(d.metadata) for d in items],
            )
        logger.info(
            "检索语料已写入 Chroma",
            docs=len(corpus.docs),
            collections={k: len(v) for k, v in sorted(grouped.items())},
            embedder=emb.name,
        )
        return cls(corpus=corpus, client=api, embedder=emb)

    def search(self, query: str, *, top_k: int = 10) -> list[RetrievedDoc]:
        vector = self._embedder.embed([query])[0]
        hits: list[tuple[CorpusDoc, float]] = []
        names = sorted({d.collection for d in self.corpus.docs})
        for name in names:
            collection = self._client.get_or_create_collection(name)
            count = collection.count()
            if count == 0:
                continue
            result = collection.query(
                query_embeddings=[vector],
                n_results=min(top_k, count),
                include=["distances"],
            )
            ids = (result.get("ids") or [[]])[0]
            distances = (result.get("distances") or [[]])[0]
            for doc_id, distance in zip(ids, distances, strict=False):
                doc = self.corpus.get(str(doc_id))
                if doc is None:
                    continue  # Chroma 里有、本次语料里没有 —— 不是本次的作用域
                hits.append((doc, 1.0 - float(distance)))
        hits.sort(key=lambda pair: (-pair[1], pair[0].doc_id))
        return [_to_doc(doc, sim) for doc, sim in hits[:top_k]]


def _flatten(metadata: dict[str, Any]) -> dict[str, str | int | float | bool]:
    """Chroma 的 metadata 只接受标量，嵌套结构序列化为 JSON。"""
    import json

    flat: dict[str, str | int | float | bool] = {}
    for key, value in metadata.items():
        if isinstance(value, str | bool | int | float):
            flat[key] = value
        else:
            flat[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return flat


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def build_vector_index(
    corpus: Corpus,
    *,
    backend: str = "memory",
    client: Any = None,
    embedder: Embedder | None = None,
) -> VectorIndex:
    """按后端名建索引。`chroma` 走 v6 §6.1 的向量库，`memory` 走精确余弦。"""
    if backend == "chroma":
        return ChromaIndex.build(corpus, client=client, embedder=embedder)
    if backend == "memory":
        return InMemoryIndex(corpus, embedder=embedder)
    raise ValueError(f"未知的向量后端 {backend!r}，可选：chroma / memory")


__all__ = [
    "ChromaIndex",
    "InMemoryIndex",
    "VectorIndex",
    "build_vector_index",
]
