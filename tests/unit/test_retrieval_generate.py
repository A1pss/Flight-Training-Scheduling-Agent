"""④ 带引用生成 + 事实核验（v6 §6.5.2 第四阶段）。

两条底线：**每条断言都要有出处**；**核验不过就退回事实直出**。
少一点文采，好过一句查无实据的话。
"""

from __future__ import annotations

from backend.retrieval.documents import RetrievedDoc, structured_doc
from backend.retrieval.generate import (
    EvidenceIndex,
    answer,
    compose_facts,
    split_claims,
    verify,
)
from backend.retrieval.pipeline import RetrievalConfig, RetrievalResult
from backend.retrieval.rewrite import RewriteOutcome
from backend.retrieval.structured import FactAnswer, StructuredResult
from backend.schemas.retrieval import RewrittenQuery
from tests.fixtures.graph_fixtures import FakeHarness, degraded_output, text_output

FACT_DOC = structured_doc(
    "pg:person_qualifications:P04:C",
    "刘斌（P04）的 C 类资质等级为单飞，复训到期日 2026-01-07",
    table="person_qualifications",
    pk={"person_id": "P04", "mission_class": "C"},
)
FACT = FactAnswer(
    kind="qualification_expiry",
    statement="刘斌（P04）的 C 类资质复训到期日是 2026-01-07。",
    citations=(FACT_DOC.citation(),),
)


def result(
    *,
    answers: tuple[FactAnswer, ...] = (FACT,),
    contexts: tuple[RetrievedDoc, ...] = (FACT_DOC,),
    ambiguities: list[str] | None = None,
) -> RetrievalResult:
    query = RewrittenQuery(
        original_query="刘斌的仪表等级何时到期？",
        semantic_query="刘斌的仪表等级何时到期？",
        ambiguities=ambiguities or [],
    )
    return RetrievalResult(
        rewrite=RewriteOutcome(query=query),
        structured=StructuredResult(docs=contexts, answers=answers),
        contexts=contexts,
        fusion=(),
        per_route={"structured": len(contexts), "bm25": 0, "vector": 0},
        config=RetrievalConfig(),
    )


# ─────────────────────────────────────────────────────────────────────
# 核验
# ─────────────────────────────────────────────────────────────────────
def test_split_claims_drops_empty_and_punctuation_only_sentences() -> None:
    assert split_claims("第一句。第二句。\n\n。") == ["第一句。", "第二句。"]


def test_a_claim_whose_numbers_and_ids_all_appear_is_supported() -> None:
    index = EvidenceIndex.build((FACT_DOC,), (FACT,))
    report = verify("刘斌（P04）的 C 类资质 2026-01-07 到期。", index)
    assert report.claims[0].supported
    assert report.claims[0].citations, "有据的句子必须挂上出处"


def test_a_fabricated_date_is_caught() -> None:
    """M1 的错答形态：02-07 而不是 01-07。"""
    index = EvidenceIndex.build((FACT_DOC,), (FACT,))
    report = verify("刘斌（P04）的 C 类资质 2026-02-07 到期。", index)
    assert report.unsupported_claims


def test_a_fabricated_entity_id_is_caught() -> None:
    index = EvidenceIndex.build((FACT_DOC,), (FACT,))
    report = verify("P99 的资质已到期。", index)
    assert report.unsupported_claims


def test_sentences_without_facts_need_no_evidence() -> None:
    """「下面是查询结果：」不是断言，不该被判无据。"""
    index = EvidenceIndex.build((FACT_DOC,), (FACT,))
    report = verify("下面是查询结果。", index)
    assert report.claims[0].supported
    assert report.claims[0].citations == []


def test_evidence_index_deduplicates_citations() -> None:
    index = EvidenceIndex.build((FACT_DOC, FACT_DOC), (FACT,))
    ids = [c.source_id for c in index.citations]
    assert len(ids) == len(set(ids))


# ─────────────────────────────────────────────────────────────────────
# 生成
# ─────────────────────────────────────────────────────────────────────
def test_without_a_harness_the_answer_is_the_structured_facts_verbatim() -> None:
    out = answer(result(), harness=None)
    assert out.text == FACT.statement
    assert out.fallback and out.degraded
    assert out.faithful


def test_compose_falls_back_to_listing_contexts_when_path_a_gave_no_verdict() -> None:
    """路 A 没给结论时直陈召回内容，**不做推理**。"""
    text = compose_facts(result(answers=()))
    assert "检索到以下相关内容" in text


def test_compose_says_so_when_nothing_was_retrieved() -> None:
    assert "没有检索到" in compose_facts(result(answers=(), contexts=()))


def test_a_faithful_llm_answer_is_kept() -> None:
    harness = FakeHarness(
        responses=[text_output("knowledge", "刘斌（P04）的 C 类仪表资质将在 2026-01-07 到期。")]
    )
    out = answer(result(), harness=harness)
    assert "2026-01-07" in out.text
    assert out.fallback is False
    assert out.faithful


def test_an_unfaithful_llm_answer_is_discarded_for_the_fact_direct_version() -> None:
    """业务方口径：LLM 只组织语言，改了数字就退回事实直出。"""
    harness = FakeHarness(
        responses=[text_output("knowledge", "刘斌（P04）的 C 类资质 2026-02-07 到期。")]
    )
    out = answer(result(), harness=harness)
    assert out.fallback is True
    assert out.text == FACT.statement
    assert "查无实据" in out.notes[0]


def test_a_degraded_llm_call_falls_back_too() -> None:
    out = answer(result(), harness=FakeHarness(responses=[degraded_output("knowledge")]))
    assert out.fallback and out.degraded
    assert out.text == FACT.statement


def test_an_empty_llm_answer_falls_back() -> None:
    out = answer(result(), harness=FakeHarness(responses=[text_output("knowledge", "  ")]))
    assert out.fallback


def test_ambiguity_asks_back_instead_of_answering() -> None:
    """§6.5.3：命中多个候选时不自行选择。**连问的是谁都没定，不作答。**"""
    out = answer(
        result(ambiguities=["「郝超」有多个可能：高超(P02)、何超(P08)。请问是哪一个？"]),
        harness=FakeHarness(responses=[text_output("knowledge", "是何超。")]),
    )
    assert "需要先确认" in out.text
    assert "高超(P02)" in out.text
    assert out.llm_calls == 0, "有歧义时不该发起生成调用"


def test_structured_facts_go_first_in_the_prompt() -> None:
    """§6.5.4 的优先级：已核实的事实在前，召回内容在后。"""
    harness = FakeHarness(responses=[text_output("knowledge", FACT.statement)])
    answer(result(), harness=harness)
    _, blocks = harness.calls[0]
    labels = [b.label for b in blocks if b.label]
    assert labels[0] == "structured_facts"
    assert "一个数字都不许改" in blocks[0].content
