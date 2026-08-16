"""RRF 融合（v6 §6.5.4）。

```
score = Σ 1/(k + rank_i)，k = 60
```

## 为什么是 RRF 而不是加权分数

> RRF 的优势是**无需调参归一化不同检索器的分数**，只用排名。

BM25 的得分是无界正数，向量的余弦在 [−1, 1]，两者不可比。把它们线性加权就
要先归一化，而归一化的参数会随语料漂移 —— 今天调好的权重，下周摄取一批新
文档就不对了。RRF 只看名次，天然免疫这件事。

## 路 A 不参与竞争

> **结构化召回（路 A）的结果具有权威性，不参与 RRF 竞争，直接置顶。**
> 原因：人员资质、维修窗口、空域容量这类事实来自 PG，是唯一真源。如果 RRF
> 把一条向量召回的旧摘要排在 SQL 精确结果之上，就可能用过期信息回答。

这条在 :func:`fuse` 里是**结构性**的：权威文档在函数一开始就被抽出来放到结果
最前面，压根不进打分循环。所以「调 k 值让路 A 排上去」这种事做不到也不需要做。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from backend.retrieval.documents import RetrievedDoc

#: v6 §6.5.4 的默认平滑常数
DEFAULT_RRF_K: Final[int] = 60


@dataclass(frozen=True)
class FusionEntry:
    """融合结果里的一条，带可解释的来源与名次。"""

    doc: RetrievedDoc
    rrf_score: float
    #: 各路给它的名次（1 起）。路 A 直接置顶的条目这里是空字典
    ranks: dict[str, int]
    pinned: bool = False


def rrf_score(ranks: Sequence[int], k: int = DEFAULT_RRF_K) -> float:
    """`Σ 1/(k + rank)`。名次从 1 起。"""
    return sum(1.0 / (k + rank) for rank in ranks)


def fuse(
    rankings: Sequence[Sequence[RetrievedDoc]],
    *,
    k: int = DEFAULT_RRF_K,
    top_k: int = 20,
) -> list[FusionEntry]:
    """融合多路召回。

    权威文档（路 A）按传入顺序**直接置顶**，其余按 RRF 得分排。
    并列时按 `doc_id` 排序 —— 名次不能靠字典的插入顺序决定（铁律 9）。

    `top_k` 限制的是**融合部分**的条数；置顶的权威文档不占这个额度，
    因为它们不是「召回出来的候选」，而是必须呈现的事实。
    """
    pinned: list[RetrievedDoc] = []
    seen_pinned: set[str] = set()
    for ranking in rankings:
        for doc in ranking:
            if doc.authoritative and doc.doc_id not in seen_pinned:
                seen_pinned.add(doc.doc_id)
                pinned.append(doc)

    ranks: dict[str, dict[str, int]] = {}
    docs: dict[str, RetrievedDoc] = {}
    for ranking in rankings:
        position = 0
        for doc in ranking:
            if doc.authoritative:
                continue  # 不参与竞争
            position += 1
            if doc.doc_id in seen_pinned:
                continue  # 同一份内容路 A 已给出权威版本，不再重复呈现
            docs.setdefault(doc.doc_id, doc)
            ranks.setdefault(doc.doc_id, {})[doc.source_kind] = position

    fused = [
        FusionEntry(
            doc=docs[doc_id],
            rrf_score=rrf_score(list(per_route.values()), k=k),
            ranks=dict(sorted(per_route.items())),
        )
        for doc_id, per_route in ranks.items()
    ]
    fused.sort(key=lambda entry: (-entry.rrf_score, entry.doc.doc_id))

    return [
        FusionEntry(doc=doc, rrf_score=float("inf"), ranks={}, pinned=True) for doc in pinned
    ] + fused[:top_k]


__all__ = [
    "DEFAULT_RRF_K",
    "FusionEntry",
    "fuse",
    "rrf_score",
]
