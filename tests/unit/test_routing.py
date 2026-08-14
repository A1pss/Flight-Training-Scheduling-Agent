"""两级意图路由与实体消解（v6 §7.2.1）。

本文件盯三件事：

1. **一级规则表逐条命中**，且规则命中路径**一次 LLM 都不调**（§7.6）；
2. **消解不猜**：并列即歧义，形态对但库里没有即 `not_found`，两者都要反问；
3. **LLM 挂了照样能用**：FTS-4001 降级路径不抛异常、不丢意图。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from backend.core.config import Settings
from backend.routing import (
    INTENT_RULES,
    classify_intent,
    levenshtein,
    match_rule,
    next_node_for,
    resolve_aircraft,
    resolve_mission,
    resolve_person,
    resolve_week,
    scan_slots,
    week_start_of,
)
from backend.routing.entities import EntityDirectory, collect_ambiguities
from backend.schemas.intent import SchedulingRequest
from tests.fixtures.graph_fixtures import (
    BASELINE_WEEK,
    FakeHarness,
    degraded_output,
    directory,
    text_output,
)

TODAY = date(2026, 1, 7)  # 基准周周三


@pytest.fixture
def dir_() -> EntityDirectory:
    return directory()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


# ─────────────────────────────────────────────────────────────────────
# 一级：规则表
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("重新排一下这周的班", "reschedule"),
        ("重排何超的课", "reschedule"),
        ("帮我调整下周的计划", "reschedule"),
        ("给所有人排班", "schedule"),
        ("安排一下 2026W02", "schedule"),
        ("生成本周的飞行计划", "schedule"),
        ("生成下周时间表", "schedule"),
        ("上传人员表", "ingest"),
        ("导入新的飞机资源", "ingest"),
        ("帮我读取这个文件", "ingest"),
        ("导出这周的表", "export"),
        ("下载 excel", "export"),
        ("输出一份 Excel 表", "export"),
        ("何超的资质情况", "query"),
        ("刘斌什么时候复训", "query"),
        ("为什么张勇没排上", "query"),
        ("陈伟的训练进度", "query"),
    ],
)
def test_intent_rules_cover_the_six_classes(text: str, expected: str) -> None:
    assert match_rule(text) == expected


def test_rule_table_is_verbatim_from_v6() -> None:
    """规则表逐条照抄 v6 §7.2.1，**顺序即优先级**。"""
    assert [intent for _, intent in INTENT_RULES] == [
        "reschedule",
        "schedule",
        "ingest",
        "export",
        "query",
    ]


def test_reschedule_wins_over_schedule() -> None:
    """「重新排班」同时命中两条，先写的赢 —— 这就是顺序即优先级。"""
    assert match_rule("重新排班") == "reschedule"


def test_unmatched_text_falls_through() -> None:
    assert match_rule("今天天气不错") is None


def test_rule_table_does_not_cover_every_natural_phrasing() -> None:
    """「排下周的班」不命中任何一条 —— **这是设计如此，不是 bug**。

    v6 §7.2.1 的规则表覆盖约 70% 的典型表述，剩下的交给 LLM 兜底。把规则写到
    能吃下所有说法，等于把一个语言问题塞进正则，而正则错了没人能发现。
    """
    assert match_rule("给何超排下周的班") is None


@pytest.mark.parametrize(
    ("intent", "node"),
    [
        ("schedule", "planner"),
        ("reschedule", "planner"),
        ("query", "END"),
        ("ingest", "END"),
        ("export", "END"),
        ("unknown", "human_gate"),
    ],
)
def test_next_node_mapping(intent: str, node: str) -> None:
    assert next_node_for(intent) == node  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────
# 实体消解
# ─────────────────────────────────────────────────────────────────────
def test_levenshtein_basics() -> None:
    assert levenshtein("何超", "何超") == 0
    assert levenshtein("何超", "高超") == 1
    assert levenshtein("", "abc") == 3
    assert levenshtein("abc", "") == 3


def test_exact_id_resolves(dir_: EntityDirectory) -> None:
    r = resolve_person("P08", dir_)
    assert (r.entity_id, r.reason, r.confidence) == ("P08", "exact_id", 1.0)


def test_exact_name_resolves(dir_: EntityDirectory) -> None:
    r = resolve_person("何超", dir_)
    assert (r.entity_id, r.reason) == ("P08", "exact_name")


def test_id_shaped_but_unknown_is_not_found(dir_: EntityDirectory) -> None:
    """形态对但库里没有 —— 这正是 `entity_hallucination` 的样子，绝不放行。"""
    r = resolve_person("P99", dir_)
    assert not r.resolved
    assert r.reason == "not_found"


def test_ambiguous_when_two_candidates_tie(dir_: EntityDirectory) -> None:
    """「郝超」到 高超(P02) 与 何超(P08) 距离都是 1 —— **不自行选择**。"""
    r = resolve_person("郝超", dir_)
    assert r.ambiguous
    assert not r.resolved
    assert {c.entity_id for c in r.candidates} == {"P02", "P08"}
    assert "高超(P02)" in r.question() and "何超(P08)" in r.question()


def test_unique_fuzzy_resolves_with_reduced_confidence(dir_: EntityDirectory) -> None:
    r = resolve_person("刘彬", dir_)
    assert (r.entity_id, r.reason) == ("P04", "fuzzy")
    assert 0.0 < r.confidence < 1.0


def test_duplicate_names_are_ambiguous_too() -> None:
    """库里允许重名，消解不允许猜。"""
    dupe = EntityDirectory(persons={"P01": "何超", "P02": "何超"})
    r = resolve_person("何超", dupe)
    assert r.ambiguous
    assert {c.entity_id for c in r.candidates} == {"P01", "P02"}


@pytest.mark.parametrize("surface", ["AC49", "ac49", "49 号机", "49号", "49"])
def test_aircraft_surfaces(surface: str, dir_: EntityDirectory) -> None:
    assert resolve_aircraft(surface, dir_).entity_id == "AC49"


def test_unknown_aircraft_number_is_not_found(dir_: EntityDirectory) -> None:
    assert not resolve_aircraft("99 号机", dir_).resolved


def test_mission_id_resolves(dir_: EntityDirectory) -> None:
    assert resolve_mission("missionC-2", dir_).entity_id == "missionC-2"


def test_duplicate_mission_names_are_ambiguous(dir_: EntityDirectory) -> None:
    """基准数据里 missionB-1 / B-2 同名「导航飞行」 —— 那是歧义不是槽位。"""
    r = resolve_mission("导航飞行", dir_)
    assert r.ambiguous


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("2026W02", "2026W02"),
        ("2026-W02", "2026W02"),
        ("2026-01-05", "2026W02"),
        ("2026/1/11", "2026W02"),
        ("1月5日", "2026W02"),
        ("本周", "2026W02"),
        ("下周", "2026W03"),
        ("上周", "2026W01"),
    ],
)
def test_week_resolution(surface: str, expected: str) -> None:
    assert resolve_week(surface, today=TODAY).entity_id == expected


def test_week_resolution_is_injected_not_wall_clock() -> None:
    """`today` 必须由调用方给 —— 否则重放时「本周」会变成另一周。"""
    a = resolve_week("本周", today=date(2026, 1, 7)).entity_id
    b = resolve_week("本周", today=date(2026, 1, 14)).entity_id
    assert a != b


def test_invalid_week_number_is_not_found() -> None:
    assert not resolve_week("2026W77", today=TODAY).resolved


def test_week_start_of() -> None:
    assert week_start_of("2026W02") == BASELINE_WEEK


def test_collect_ambiguities_includes_not_found(dir_: EntityDirectory) -> None:
    """`not_found` 也要反问 —— 静默忽略等于把「这架飞机不存在」藏起来。"""
    items = collect_ambiguities([resolve_aircraft("AC99", dir_), resolve_person("郝超", dir_)])
    assert len(items) == 2
    assert {i["reason"] for i in items} == {"not_found", "ambiguous"}


# ─────────────────────────────────────────────────────────────────────
# 槽位扫描与两级分类
# ─────────────────────────────────────────────────────────────────────
def test_scan_slots_finds_ids_and_names(dir_: EntityDirectory) -> None:
    slots = scan_slots("给 何超 和 P05 排 missionC-1，用 AC49，下周", dir_)
    assert "P05" in slots.persons and "何超" in slots.persons
    assert slots.aircraft == ["AC49"]
    assert slots.missions == ["missionC-1"]
    assert slots.week == "下周"


def test_rule_hit_costs_zero_llm_calls(dir_: EntityDirectory, settings: Settings) -> None:
    """v6 §7.6：规则命中即 0 次 LLM 调用。"""
    harness = FakeHarness(responses=[text_output("route", "{}")])
    d = classify_intent(
        "给何超排班，下周", directory=dir_, today=TODAY, harness=harness, settings=settings
    )
    assert (d.intent, d.source, d.confidence) == ("schedule", "rule", 1.0)
    assert d.llm_calls == 0
    assert harness.calls == []


def test_rule_hit_builds_scheduling_request(dir_: EntityDirectory, settings: Settings) -> None:
    d = classify_intent("给何超排班，下周", directory=dir_, today=TODAY, settings=settings)
    assert isinstance(d.request, SchedulingRequest)
    assert d.request.persons == ["P08"]
    assert d.request.iso_week == "2026W03"
    assert d.request.week_start == date(2026, 1, 12)


def test_llm_fallback_classifies_and_resolves(dir_: EntityDirectory, settings: Settings) -> None:
    payload = json.dumps({"intent": "schedule", "persons": ["何超"], "week": "本周"})
    harness = FakeHarness(responses=[text_output("route", payload)])
    d = classify_intent(
        "把小何那摊子事儿弄一弄", directory=dir_, today=TODAY, harness=harness, settings=settings
    )
    assert (d.intent, d.source) == ("schedule", "llm")
    assert d.llm_calls == settings.SELF_CONSISTENCY_SAMPLES
    assert d.agreement == 1.0
    assert isinstance(d.request, SchedulingRequest)
    assert d.request.persons == ["P08"]


def test_llm_fallback_entity_goes_through_resolver_not_model(
    dir_: EntityDirectory, settings: Settings
) -> None:
    """模型给的是**原文表述**；它编一个不存在的名字出来，消解层照样拦下。"""
    payload = json.dumps({"intent": "schedule", "persons": ["赵六"]})
    harness = FakeHarness(responses=[text_output("route", payload)])
    d = classify_intent(
        "随便排点什么", directory=dir_, today=TODAY, harness=harness, settings=settings
    )
    assert d.ambiguities
    assert d.ambiguities[0]["surface"] == "赵六"


def test_out_of_range_intent_falls_back_to_unknown(
    dir_: EntityDirectory, settings: Settings
) -> None:
    payload = json.dumps({"intent": "洗飞机"})
    harness = FakeHarness(responses=[text_output("route", payload)])
    d = classify_intent(
        "洗一下飞机", directory=dir_, today=TODAY, harness=harness, settings=settings
    )
    assert d.intent == "unknown"


def test_self_consistency_ratio_drops_when_samples_disagree(
    dir_: EntityDirectory, settings: Settings
) -> None:
    harness = FakeHarness(
        responses=[
            text_output("route", json.dumps({"intent": "schedule"})),
            text_output("route", json.dumps({"intent": "query"})),
            text_output("route", json.dumps({"intent": "export"})),
        ]
    )
    d = classify_intent("看看这个", directory=dir_, today=TODAY, harness=harness, settings=settings)
    assert d.agreement == pytest.approx(1 / 3)
    assert d.confidence < 0.75


def test_degraded_llm_keeps_working_and_records_fts_4001(
    dir_: EntityDirectory, settings: Settings
) -> None:
    """FTS-4001：LLM 挂了不抛异常，降级为规则匹配 + 表单追问。"""
    harness = FakeHarness(responses=[degraded_output("route")])
    d = classify_intent("帮我看看", directory=dir_, today=TODAY, harness=harness, settings=settings)
    assert d.source == "degraded"
    assert d.intent == "unknown"
    assert [e.code.value for e in d.errors] == ["FTS-4001"]
    assert d.errors[0].severity == "WARN"
    assert d.errors[0].retryable is True


def test_no_harness_means_no_llm_path(dir_: EntityDirectory, settings: Settings) -> None:
    d = classify_intent("帮我看看", directory=dir_, today=TODAY, settings=settings)
    assert (d.source, d.llm_calls) == ("degraded", 0)


def test_threshold_never_blocks_rule_hits(dir_: EntityDirectory, settings: Settings) -> None:
    d = classify_intent("给所有人排班", directory=dir_, today=TODAY, settings=settings)
    assert d.below_threshold(0.99) is False
