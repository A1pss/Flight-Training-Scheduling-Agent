"""三路召回、RRF 融合、精排（v6 §6.5.2 第 ②③ 步）。

本文件最要紧的一组断言是**路 A 置顶**（§6.5.4）：结构化召回的结果不参与
RRF 竞争、不参与精排打分，直接进最终上下文的最前面。
"""

from __future__ import annotations

from backend.memory.embeddings import HashEmbedder
from backend.retrieval.bm25 import Bm25Index, bm25_search, tokenize
from backend.retrieval.corpus import Corpus, CorpusDoc
from backend.retrieval.documents import RetrievedDoc, dedupe, structured_doc
from backend.retrieval.rerank import LexicalReranker, rerank
from backend.retrieval.rrf import DEFAULT_RRF_K, fuse, rrf_score
from backend.retrieval.vector import InMemoryIndex

# ─────────────────────────────────────────────────────────────────────
# 夹具
# ─────────────────────────────────────────────────────────────────────
STUDENT = "何超（P08）身份为学员，可飞机型 JL-8；资质：A类/单飞、B类/带飞"
INSTRUCTOR = "高超（P02）身份为教员，可飞机型 JL-8、JL-9；资质：A类/教员"
MISSION = "missionB-1（导航飞行）属 B 类，时长 52 分钟，先修 A类"
RULE = "约束7·飞机排期冲突与周转时间：上一架次着陆到下一架次起飞的间隔不得小于周转时间"


def corpus() -> Corpus:
    return Corpus(
        docs=(
            CorpusDoc(doc_id="ent:person:P02", text=INSTRUCTOR, collection="entity_summaries"),
            CorpusDoc(doc_id="ent:person:P08", text=STUDENT, collection="entity_summaries"),
            CorpusDoc(doc_id="ent:mission:missionB-1", text=MISSION, collection="entity_summaries"),
            CorpusDoc(doc_id="rule:07", text=RULE, collection="rule_texts"),
        )
    )


# ─────────────────────────────────────────────────────────────────────
# 分词
# ─────────────────────────────────────────────────────────────────────
def test_ascii_ids_stay_whole_because_that_is_this_route_s_job() -> None:
    """编号是 BM25 的主场（§6.5.1）。拆成字符这一路就废了。"""
    tokens = tokenize("missionC-2 的先修")
    assert "missionc-2" in tokens
    assert "ac73" in tokenize("AC73 什么时候维护")
    assert "2026w02" in tokenize("2026W02 的班")


def test_letter_digit_boundary_is_also_split_so_bare_numbers_hit() -> None:
    tokens = tokenize("AC73")
    assert {"ac73", "ac", "73"} <= set(tokens)


def test_chinese_becomes_unigrams_and_bigrams_no_segmenter_needed() -> None:
    tokens = tokenize("导航飞行")
    assert "导" in tokens and "导航" in tokens and "飞行" in tokens


def test_tokenize_is_deterministic() -> None:
    assert tokenize("何超 missionB-1") == tokenize("何超 missionB-1")


# ─────────────────────────────────────────────────────────────────────
# 路 B · BM25
# ─────────────────────────────────────────────────────────────────────
def test_bm25_finds_the_document_carrying_the_id() -> None:
    hits = bm25_search(corpus(), "missionB-1 的先修是什么", top_k=3)
    assert hits[0].doc_id == "ent:mission:missionB-1"
    assert hits[0].source_kind == "bm25"
    assert hits[0].authoritative is False


def test_bm25_drops_zero_score_documents() -> None:
    """0 分文档塞进融合只会稀释 RRF 的名次。"""
    hits = bm25_search(corpus(), "完全无关的外星语", top_k=10)
    assert all((h.score or 0.0) > 0 for h in hits)


def test_bm25_on_empty_corpus_returns_empty_not_raises() -> None:
    """语料为空不是异常，是「还没摄取」。"""
    assert Bm25Index.build(Corpus(docs=())).search("何超") == []


# ─────────────────────────────────────────────────────────────────────
# 路 C · 向量
# ─────────────────────────────────────────────────────────────────────
def test_vector_search_is_deterministic_with_hash_embedder() -> None:
    index = InMemoryIndex(corpus(), embedder=HashEmbedder())
    first = [d.doc_id for d in index.search("导航飞行", top_k=3)]
    second = [d.doc_id for d in index.search("导航飞行", top_k=3)]
    assert first == second


def test_search_many_unions_and_keeps_the_best_score_per_document() -> None:
    """§6.5.3 末段：改写后与原始查询取并集，同一文档只占一个名次。"""
    index = InMemoryIndex(corpus(), embedder=HashEmbedder())
    hits = index.search_many(["导航飞行", "missionB-1 先修", "导航飞行"], top_k=4)
    ids = [d.doc_id for d in hits]
    assert len(ids) == len(set(ids)), "同一文档不得在并集里出现两次"


# ─────────────────────────────────────────────────────────────────────
# ③ RRF —— 路 A 置顶
# ─────────────────────────────────────────────────────────────────────
def test_rrf_score_matches_the_formula_in_the_spec() -> None:
    assert rrf_score([1], k=60) == 1 / 61
    assert rrf_score([1, 2], k=60) == 1 / 61 + 1 / 62


def test_default_k_is_sixty() -> None:
    assert DEFAULT_RRF_K == 60


def test_authoritative_documents_are_pinned_above_everything() -> None:
    """§6.5.4：路 A 的结果不参与 RRF 竞争，直接置顶。"""
    sql = structured_doc("pg:persons:P08", STUDENT, table="persons", pk={"person_id": "P08"})
    # 故意让向量路给出一堆排名很前的条目
    vectors = [RetrievedDoc(doc_id=f"v{i}", text="旧摘要", source_kind="vector") for i in range(5)]
    entries = fuse([[sql], vectors], top_k=10)
    assert entries[0].doc.doc_id == "pg:persons:P08"
    assert entries[0].pinned is True
    assert all(not e.pinned for e in entries[1:])


def test_a_stale_summary_can_never_outrank_the_sql_result() -> None:
    """这正是 §6.5.4 给的理由：否则可能用过期信息回答。"""
    stale = [RetrievedDoc(doc_id="ent:person:P08", text="旧摘要", source_kind="vector")]
    fresh = structured_doc("pg:persons:P08", STUDENT, table="persons", pk={"person_id": "P08"})
    order = [e.doc.doc_id for e in fuse([stale, [fresh]], top_k=10)]
    assert order[0] == "pg:persons:P08"


def test_fusion_top_k_limits_the_competing_part_only() -> None:
    """置顶的权威文档不占 `top_k` 额度 —— 它们是必须呈现的事实，不是候选。"""
    sql = [structured_doc(f"pg:{i}", "事实", table="t", pk={"i": i}) for i in range(3)]
    noise = [RetrievedDoc(doc_id=f"n{i}", text="噪声", source_kind="bm25") for i in range(10)]
    entries = fuse([sql, noise], top_k=2)
    assert sum(1 for e in entries if e.pinned) == 3
    assert sum(1 for e in entries if not e.pinned) == 2


def test_fusion_ties_break_on_doc_id_so_order_is_reproducible() -> None:
    a = [RetrievedDoc(doc_id="b", text="x", source_kind="bm25")]
    b = [RetrievedDoc(doc_id="a", text="x", source_kind="vector")]
    assert [e.doc.doc_id for e in fuse([a, b])] == ["a", "b"]


def test_fusion_ranks_are_reported_per_route_for_explainability() -> None:
    bm25 = [RetrievedDoc(doc_id="d1", text="x", source_kind="bm25")]
    vector = [RetrievedDoc(doc_id="d1", text="x", source_kind="vector")]
    entry = fuse([bm25, vector])[0]
    assert entry.ranks == {"bm25": 1, "vector": 1}


# ─────────────────────────────────────────────────────────────────────
# ③ 精排
# ─────────────────────────────────────────────────────────────────────
def test_rerank_pins_authoritative_documents_without_scoring_them() -> None:
    """让交叉编码器给一条 SQL 精确结果打分，等于把权威交给概率模型。"""
    sql = structured_doc("pg:persons:P08", STUDENT, table="persons", pk={"person_id": "P08"})
    others = [
        RetrievedDoc(doc_id="ent:person:P02", text=INSTRUCTOR, source_kind="vector"),
        RetrievedDoc(doc_id="rule:07", text=RULE, source_kind="bm25"),
    ]
    result = rerank("何超的资质", [*others, sql], top_k=1, reranker=LexicalReranker())
    assert result.docs[0].doc_id == "pg:persons:P08"
    assert len(result.docs) == 2  # 1 条置顶 + top_k=1 条精排


def test_rerank_reports_which_implementation_ran() -> None:
    """替身必须如实标注（与 `HashEmbedder` 同一条处置）。"""
    result = rerank(
        "x", [RetrievedDoc(doc_id="d", text="y", source_kind="bm25")], reranker=LexicalReranker()
    )
    assert result.provider == "lexical"
    assert result.candidates == 1


def test_rerank_with_only_authoritative_documents_skips_the_model() -> None:
    sql = structured_doc("pg:1", "事实", table="t", pk={})
    assert rerank("x", [sql], reranker=LexicalReranker()).docs == (sql,)


# ─────────────────────────────────────────────────────────────────────
# 文档工具
# ─────────────────────────────────────────────────────────────────────
def test_structured_doc_always_marks_itself_authoritative() -> None:
    """调用方无从关掉这一位 —— 忘了置位就会掉进 RRF 里和旧摘要比排名。"""
    doc = structured_doc("pg:x", "事实", table="persons", pk={"person_id": "P08"})
    assert doc.authoritative is True
    assert doc.metadata["table"] == "persons"
    assert doc.citation().source_kind == "structured"


def test_dedupe_keeps_the_first_occurrence_so_authority_survives() -> None:
    sql = structured_doc("d", "权威", table="t", pk={})
    weak = RetrievedDoc(doc_id="d", text="摘要", source_kind="vector")
    assert dedupe([sql, weak])[0].authoritative is True


def test_corpus_filter_excludes_archived_by_default() -> None:
    """§6.4 遗忘策略：归档条目仍可检索，但不参与默认召回。"""
    docs = (
        CorpusDoc(doc_id="a", text="新", collection="episodic_summaries"),
        CorpusDoc(doc_id="b", text="旧", collection="episodic_summaries", archived=True),
    )
    assert [d.doc_id for d in Corpus(docs=docs).filter().docs] == ["a"]
    assert len(Corpus(docs=docs).filter(include_archived=True).docs) == 2
