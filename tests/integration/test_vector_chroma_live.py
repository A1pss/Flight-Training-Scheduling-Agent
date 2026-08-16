"""路 C 的 Chroma 后端（v6 §6.1 指定的向量库）。

## 为什么这个文件能在 CI 上跑，而 `bge-m3` 不能

Chroma 是**嵌入式**的（`PersistentClient` + 本地目录），不需要任何外部服务。
本文件一律用 `HashEmbedder`：确定性、零权重文件 —— 于是 CI 上跑的是**真的
Chroma 读写**，只是向量来自哈希而不是 bge-m3。

⚠️ **M1 记的那个段错误在这里不成立，但别把它忘了**：`chromadb` 与 `torch` 的
原生运行时在同一进程里初始化会打架，所以 `upsert_chunks` / `ChromaIndex.build`
都严格按「**先算完全部向量，再建 client**」的顺序写。`HashEmbedder` 不碰 torch，
所以本文件安全；换成 `BGEM3Embedder` 就要守那个顺序（`ChromaIndex.build` 已经
守着了，别去动它的两步）。

## 断言什么

**两个后端的检索语义必须一致** —— 否则单测（`InMemoryIndex`）绿、真机（Chroma）红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.memory.chroma import build_client, collection_counts
from backend.memory.embeddings import HashEmbedder
from backend.retrieval.corpus import Corpus, CorpusDoc
from backend.retrieval.vector import ChromaIndex, InMemoryIndex, build_vector_index

pytestmark = pytest.mark.integration


def corpus() -> Corpus:
    return Corpus(
        docs=(
            CorpusDoc(
                doc_id="ent:person:P02",
                text="高超（P02）身份为教员，可飞机型 JL-8、JL-9；资质：A类/教员",
                collection="entity_summaries",
                metadata={
                    "entity_type": "person",
                    "entity_id": "P02",
                    "snapshot_id": "snap_test",
                    "field_map": {"table": "persons", "pk": {"person_id": "P02"}},
                },
            ),
            CorpusDoc(
                doc_id="ent:person:P08",
                text="何超（P08）身份为学员，可飞机型 JL-8；资质：A类/单飞、B类/带飞",
                collection="entity_summaries",
                metadata={
                    "entity_type": "person",
                    "entity_id": "P08",
                    "snapshot_id": "snap_test",
                    "field_map": {"table": "persons", "pk": {"person_id": "P08"}},
                },
            ),
            CorpusDoc(
                doc_id="rule:07",
                text="约束7·飞机排期冲突与周转时间：上一架次着陆到下一架次起飞不得小于周转时间",
                collection="rule_texts",
                metadata={
                    "rule_id": 7,
                    "hard_soft": "hard",
                    "ruleset_version": "1.3.0",
                    "title": "飞机排期冲突与周转时间",
                },
            ),
        )
    )


@pytest.fixture
def chroma_index(tmp_path: Path) -> ChromaIndex:
    """每个用例一个独立的 Chroma 目录 —— 用例之间不许互相看见。"""
    client = build_client(tmp_path / "chroma")
    return ChromaIndex.build(corpus(), client=client, embedder=HashEmbedder())


def test_the_corpus_lands_in_the_collections_it_declares(
    tmp_path: Path, chroma_index: ChromaIndex
) -> None:
    counts = collection_counts(build_client(tmp_path / "chroma"))
    assert counts["entity_summaries"] == 2
    assert counts["rule_texts"] == 1
    # 声明了五个 collection，没写入的那几个是空的，不是不存在
    assert counts["episodic_summaries"] == 0


def test_search_returns_documents_from_the_corpus(chroma_index: ChromaIndex) -> None:
    hits = chroma_index.search("周转时间是多少", top_k=3)
    assert hits, "Chroma 后端必须真的召回得到东西"
    assert all(h.source_kind == "vector" for h in hits)
    assert all(not h.authoritative for h in hits)
    assert {h.doc_id for h in hits} <= set(corpus().ids)


def test_build_is_idempotent_so_reindexing_does_not_duplicate(
    tmp_path: Path, chroma_index: ChromaIndex
) -> None:
    """文档 id 由语料决定，重复 upsert 覆盖同一行。

    「摄取写过一遍、检索侧再补一遍」不该产生重复条目。
    """
    client = build_client(tmp_path / "chroma")
    ChromaIndex.build(corpus(), client=client, embedder=HashEmbedder())
    counts = collection_counts(client)
    assert counts["entity_summaries"] == 2, "重建索引不该让条数翻倍"


def test_chroma_and_in_memory_agree_on_the_top_hit(chroma_index: ChromaIndex) -> None:
    """**两个后端的检索语义必须一致** —— 否则单测绿、真机红。

    两边都是 L2 归一化向量上的余弦（`HashEmbedder` 已归一化，Chroma 建表时
    `hnsw:space=cosine`），所以 top-1 应当相同。
    """
    memory = InMemoryIndex(corpus(), embedder=HashEmbedder())
    for query in ("周转时间", "何超的资质", "教员能飞哪些机型"):
        assert chroma_index.search(query, top_k=1)[0].doc_id == (
            memory.search(query, top_k=1)[0].doc_id
        ), f"两个后端在「{query}」上给出了不同的 top-1"


def test_search_many_unions_without_duplicating(chroma_index: ChromaIndex) -> None:
    hits = chroma_index.search_many(["何超的资质", "周转时间", "何超的资质"], top_k=5)
    ids = [h.doc_id for h in hits]
    assert len(ids) == len(set(ids))


def test_documents_outside_the_current_corpus_are_ignored(tmp_path: Path) -> None:
    """Chroma 里有、本次语料里没有的条目**不进结果** —— 那不是本次的作用域。

    换快照之后旧向量还躺在库里，召回到它们等于跨快照串数据。
    """
    client = build_client(tmp_path / "chroma")
    ChromaIndex.build(corpus(), client=client, embedder=HashEmbedder())

    narrowed = Corpus(docs=(corpus().docs[2],))  # 只留规则那条
    index = ChromaIndex(corpus=narrowed, client=client, embedder=HashEmbedder())
    hits = index.search("何超的资质", top_k=5)
    assert {h.doc_id for h in hits} <= {"rule:07"}


def test_build_vector_index_dispatches_on_the_backend_name(tmp_path: Path) -> None:
    client = build_client(tmp_path / "chroma")
    assert isinstance(
        build_vector_index(corpus(), backend="chroma", client=client, embedder=HashEmbedder()),
        ChromaIndex,
    )
    assert isinstance(
        build_vector_index(corpus(), backend="memory", embedder=HashEmbedder()), InMemoryIndex
    )
    with pytest.raises(ValueError, match="未知的向量后端"):
        build_vector_index(corpus(), backend="随便写的")


def test_an_empty_corpus_does_not_blow_up(tmp_path: Path) -> None:
    client = build_client(tmp_path / "chroma")
    index = ChromaIndex.build(Corpus(docs=()), client=client, embedder=HashEmbedder())
    assert index.search("任意问题") == []
