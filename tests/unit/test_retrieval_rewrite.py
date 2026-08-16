"""① 查询改写（v6 §6.5.2 / §6.5.3）。

最要紧的一条：**实体消解不靠 LLM 猜**。LLM 只圈表述，编号由字典裁决；
并列命中（何超 / 高超）写进 `ambiguities` 触发反问，不自行选择。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from backend.retrieval.rewrite import (
    ConversationTurn,
    decompose,
    normalize_time,
    resolve_anaphora,
    rewrite_query,
    scan_surfaces,
    this_week,
    week_range,
)
from backend.schemas.common import EntityRef
from tests.fixtures.graph_fixtures import FakeHarness, directory, text_output

TODAY = date(2026, 1, 7)  # 基准周 2026W02 的周三


# ─────────────────────────────────────────────────────────────────────
# 时间归一
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "start", "week"),
    [
        ("本周谁飞得最多", date(2026, 1, 5), "2026W02"),
        ("上周为什么推迟了", date(2025, 12, 29), "2026W01"),
        ("上上周的班", date(2025, 12, 22), "2025W52"),
        ("下周排了什么", date(2026, 1, 12), "2026W03"),
        ("2026W02 的方案", date(2026, 1, 5), "2026W02"),
        ("1月5日飞了什么", date(2026, 1, 5), "2026W02"),
    ],
)
def test_relative_time_normalizes_to_iso_week_and_date_range(
    text: str, start: date, week: str
) -> None:
    span, iso = normalize_time(text, today=TODAY)
    assert iso == week
    assert span is not None
    assert span.start == start
    assert (span.end - span.start).days == 6


def test_no_time_expression_means_no_range_not_this_week() -> None:
    """硬安一个「本周」会让时间过滤把该召回的东西滤掉。"""
    span, iso = normalize_time("刘斌的资质什么时候到期", today=TODAY)
    assert span is None and iso == ""


def test_week_helpers_agree_with_normalize_time() -> None:
    assert week_range("2026W02").start == date(2026, 1, 5)
    assert this_week(TODAY).start == date(2026, 1, 5)


# ─────────────────────────────────────────────────────────────────────
# 查询分解
# ─────────────────────────────────────────────────────────────────────
def test_compound_question_splits_on_explicit_markers() -> None:
    parts = decompose("刘斌的仪表等级何时到期；他还能不能飞仪表课目")
    assert len(parts) == 2


def test_single_question_is_not_hacked_apart() -> None:
    """从「能不能」处切开只会毁掉这个问题。"""
    assert decompose("何超能不能排 missionB-1") == ("何超能不能排 missionB-1",)


# ─────────────────────────────────────────────────────────────────────
# 指代消解
# ─────────────────────────────────────────────────────────────────────
def test_pronoun_resolves_to_the_single_person_of_the_previous_turn() -> None:
    history = [
        ConversationTurn(
            utterance="刘斌的仪表等级何时到期",
            entities=(EntityRef(kind="person", entity_id="P04", surface="刘斌"),),
        )
    ]
    assert resolve_anaphora("他还能不能飞", history) == ("P04",)


def test_pronoun_with_two_candidates_in_the_previous_turn_resolves_to_nothing() -> None:
    """上一轮提到两个人时「他」指谁是不确定的 —— 不猜，交给歧义反问。"""
    history = [
        ConversationTurn(
            utterance="何超和张勇",
            entities=(
                EntityRef(kind="person", entity_id="P08", surface="何超"),
                EntityRef(kind="person", entity_id="P06", surface="张勇"),
            ),
        )
    ]
    assert resolve_anaphora("他能飞吗", history) == ()


def test_no_history_means_no_anaphora() -> None:
    assert resolve_anaphora("他能飞吗", []) == ()


# ─────────────────────────────────────────────────────────────────────
# 表述扫描与消解
# ─────────────────────────────────────────────────────────────────────
def test_scan_finds_names_and_ids_from_the_current_directory() -> None:
    found = scan_surfaces("何超能不能排 missionB-1，用 AC73", directory())
    assert "何超" in found["person"]
    assert "missionB-1" in found["mission"]
    assert "AC73" in found["aircraft"]


def test_scan_does_not_do_ner_by_design() -> None:
    """认不出「郝超」是人名 —— 那是 NER 不是正则（M4-B §3.11 同一条理由）。"""
    assert scan_surfaces("郝超怎么样", directory())["person"] == []


def test_resolution_is_exact_and_never_confuses_the_two_chao() -> None:
    outcome = rewrite_query("何超的资质情况", directory=directory(), today=TODAY)
    ids = {e.entity_id for e in outcome.query.resolved_entities}
    assert ids == {"P08"}
    assert "P02" not in ids
    assert outcome.query.ambiguities == []


def test_llm_only_circles_surfaces_the_dictionary_decides_the_id() -> None:
    """§6.5.3：LLM 只识别「这里提到了一个人名」，映射走字典。"""
    harness = FakeHarness(
        responses=[
            text_output(
                "knowledge",
                json.dumps(
                    {
                        # 模型给的是**表述**；就算它顺手写了个编号也不该被采信
                        "person_surfaces": ["何超"],
                        "sub_queries": ["何超的资质情况"],
                        "semantic_query": "何超的资质情况",
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    )
    outcome = rewrite_query("何超的资质情况", directory=directory(), today=TODAY, harness=harness)
    assert [e.entity_id for e in outcome.query.resolved_entities] == ["P08"]
    assert outcome.degraded is False


def test_a_surface_that_ties_between_two_people_triggers_a_question() -> None:
    """「郝超」到 高超/何超 距离都是 1 —— 并列即歧义，不自行选择。"""
    harness = FakeHarness(
        responses=[
            text_output(
                "knowledge",
                json.dumps(
                    {
                        "person_surfaces": ["郝超"],
                        "sub_queries": ["郝超"],
                        "semantic_query": "郝超",
                    },
                    ensure_ascii=False,
                ),
            )
        ]
    )
    outcome = rewrite_query("郝超的资质", directory=directory(), today=TODAY, harness=harness)
    assert outcome.query.needs_clarification
    assert any("高超" in a and "何超" in a for a in outcome.query.ambiguities)
    assert outcome.query.resolved_entities == []


def test_entity_not_in_the_snapshot_is_also_a_question_not_a_silent_drop() -> None:
    harness = FakeHarness(
        responses=[
            text_output(
                "knowledge",
                json.dumps(
                    {"aircraft_surfaces": ["AC99"], "sub_queries": ["x"], "semantic_query": "x"},
                    ensure_ascii=False,
                ),
            )
        ]
    )
    outcome = rewrite_query("AC99 什么机型", directory=directory(), today=TODAY, harness=harness)
    assert outcome.query.needs_clarification


def test_llm_unavailable_falls_back_to_deterministic_scan() -> None:
    """FTS-4001：覆盖面窄一些，但不会猜错编号。"""
    outcome = rewrite_query("何超的资质情况", directory=directory(), today=TODAY, harness=None)
    assert outcome.degraded is True
    assert [e.entity_id for e in outcome.query.resolved_entities] == ["P08"]


def test_malformed_llm_output_does_not_break_the_pipeline() -> None:
    harness = FakeHarness(responses=[text_output("knowledge", "这不是 JSON")])
    outcome = rewrite_query("何超的资质", directory=directory(), today=TODAY, harness=harness)
    assert [e.entity_id for e in outcome.query.resolved_entities] == ["P08"]


# ─────────────────────────────────────────────────────────────────────
# 产物形状
# ─────────────────────────────────────────────────────────────────────
def test_original_query_is_always_preserved(  # §6.5.3 末段
) -> None:
    outcome = rewrite_query("为什么上周把编队飞行推迟了", directory=directory(), today=TODAY)
    assert outcome.query.original_query == "为什么上周把编队飞行推迟了"
    assert outcome.query.original_query in outcome.vector_queries()


def test_semantic_query_only_adds_it_never_deletes() -> None:
    """删字是改写丢语义的主要来源 —— 「为什么」正是那句话的重点。"""
    outcome = rewrite_query("为什么上周把编队飞行推迟了", directory=directory(), today=TODAY)
    assert "为什么" in outcome.query.semantic_query
    assert "编队" in outcome.query.semantic_query


def test_keyword_terms_carry_ids_and_system_terms_for_bm25() -> None:
    outcome = rewrite_query("何超的编队飞行排了吗", directory=directory(), today=TODAY)
    assert "P08" in outcome.query.keyword_terms
    assert "F类" in outcome.query.keyword_terms
    assert "P08" in outcome.bm25_query()


def test_vector_queries_are_deduplicated() -> None:
    outcome = rewrite_query("AC73 是什么机型", directory=directory(), today=TODAY)
    queries = outcome.vector_queries()
    assert len(queries) == len(set(queries))
