"""路 B · 稀疏召回：BM25（`rank_bm25`，本地）。

> **编号、专有名词**（v6 §6.5.1）。「约束7 是什么」「missionC-2 的先修」这类
> 查询里，编号是低频 token，语义模型不敏感，而 BM25 对它们最擅长。

## 分词：字符 n-gram，不引中文分词器

中文没有空格，`rank_bm25` 要的是词表。可选做法有两种：

| 做法 | 代价 |
|---|---|
| jieba / pkuseg 之类的分词器 | 多一个离线交付要装的依赖（v6 §11.4）；分词结果随词典版本变化，**破坏可复现性**（铁律 9） |
| **字符 unigram + bigram**（本模块） | 零依赖、逐字节可复现；中文检索上与分词器差距很小 |

选后者。同时**保留 ASCII 串的完整形态**：`missionC-2`、`AC73`、`P08`、`RWY-2`
必须作为**一个** token 存在 —— 把它们拆成字符会让编号类查询直接失效，而那正是
这一路的主场。所以分词是「ASCII 串整取 + 中文字符 n-gram」的混合。

## 为什么不做停用词

停用词表是另一份要维护、要随语料漂移的东西。BM25 的 IDF 本来就会把「的」
「是」这类高频字压到接近 0 权重 —— 让算法自己处理，比手工列表更稳。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from rank_bm25 import BM25Okapi

from backend.retrieval.corpus import Corpus
from backend.retrieval.documents import RetrievedDoc

#: ASCII 串（含连字符与下划线）整取：`missionC-2` / `AC73` / `RWY-2` / `2026W02`
_ASCII_TOKEN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]*")
#: 非 ASCII 的连续片段（中文为主），按字符切 n-gram
_CJK_RUN: Final[re.Pattern[str]] = re.compile(r"[^\x00-\x7f]+")


def tokenize(text: str) -> list[str]:
    """混合分词：ASCII 串整取 + 中文 unigram/bigram。

    `missionC-2` 会同时产出 `missionc-2`（整串）与 `missionc`、`2`（子串），
    这样「missionC-2 的先修」与「mission C 2」两种打法都能命中。
    """
    lowered = text.lower()
    tokens: list[str] = []
    for match in _ASCII_TOKEN.finditer(lowered):
        token = match.group()
        tokens.append(token)
        # 编号里的分隔符两侧各自也成词：`ac73` → `ac73`；`rwy-2` → `rwy`, `2`
        parts = [p for p in re.split(r"[_\-]", token) if p]
        if len(parts) > 1:
            tokens.extend(parts)
        # 字母与数字的交界处再切一刀：`ac73` → `ac`, `73`
        for piece in re.findall(r"[a-z]+|\d+", token):
            if piece != token:
                tokens.append(piece)
    for run in _CJK_RUN.finditer(lowered):
        chars = run.group()
        tokens.extend(chars)
        tokens.extend(chars[i : i + 2] for i in range(len(chars) - 1))
    return tokens


@dataclass
class Bm25Index:
    """一份语料上的 BM25 索引。

    **建索引是纯函数**：同一份语料建两次，打分逐位相同。`rank_bm25` 自身
    不带随机性，不需要 seed。
    """

    corpus: Corpus
    _bm25: BM25Okapi
    _ids: tuple[str, ...]

    @classmethod
    def build(cls, corpus: Corpus) -> Bm25Index:
        docs = corpus.docs
        if not docs:
            # 空语料下 `BM25Okapi([])` 会 ZeroDivisionError。给一个空索引，
            # 由 `search` 直接返回空 —— 语料为空不是异常，是「还没摄取」。
            return cls(corpus=corpus, _bm25=_EMPTY_BM25, _ids=())
        tokenized = [tokenize(d.text) or ["∅"] for d in docs]
        return cls(
            corpus=corpus,
            _bm25=BM25Okapi(tokenized),
            _ids=tuple(d.doc_id for d in docs),
        )

    def search(self, query: str, *, top_k: int = 10) -> list[RetrievedDoc]:
        """按 BM25 得分取前 `top_k`。

        **得分 ≤0 的一律丢弃**：BM25 对完全不相关的文档给 0 分，把它们塞进
        融合只会稀释 RRF 的名次。并列时按 `doc_id` 排序，保证可复现。
        """
        if not self._ids:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(
            ((doc_id, float(score)) for doc_id, score in zip(self._ids, scores, strict=True)),
            key=lambda pair: (-pair[1], pair[0]),
        )
        out: list[RetrievedDoc] = []
        for doc_id, score in ranked[:top_k]:
            if score <= 0.0:
                continue
            doc = self.corpus.get(doc_id)
            if doc is None:  # pragma: no cover - _ids 恒来自 corpus
                continue
            out.append(
                RetrievedDoc(
                    doc_id=doc.doc_id,
                    text=doc.text,
                    source_kind="bm25",
                    authoritative=False,
                    score=round(score, 6),
                    metadata=dict(doc.metadata),
                    valid_from=doc.valid_from,
                    valid_to=doc.valid_to,
                )
            )
        return out


def bm25_search(corpus: Corpus, query: str, *, top_k: int = 10) -> list[RetrievedDoc]:
    """一次性建索引并检索。语料稳定时请复用 :class:`Bm25Index`。"""
    return Bm25Index.build(corpus).search(query, top_k=top_k)


def _make_empty_bm25() -> BM25Okapi:
    """空索引占位。`BM25Okapi` 不接受空语料，给它一个哨兵文档。"""
    return BM25Okapi([["∅"]])


_EMPTY_BM25: Final[BM25Okapi] = _make_empty_bm25()


def multi_query_search(
    index: Bm25Index, queries: Sequence[str], *, top_k: int = 10
) -> list[list[RetrievedDoc]]:
    """对一组子查询各跑一次（§6.5.2 的「查询分解」产物）。"""
    return [index.search(q, top_k=top_k) for q in queries if q.strip()]


__all__ = [
    "Bm25Index",
    "bm25_search",
    "multi_query_search",
    "tokenize",
]
