"""校验层单测（v6 §5.1 校验段 + §3.1.1 断言）。"""

from __future__ import annotations

from datetime import date

import pytest

from backend.core.errors import DataConflictError, IngestionError
from backend.ingestion.schema import IngestedFacts
from backend.ingestion.validate import (
    EXPECTED_COUNTS,
    assert_crew_composition,
    check_entity_counts,
    check_referential_integrity,
    check_time_logic,
    check_value_domains,
    validate_facts,
)
from tests.fixtures.ingestion_facts import make_person, minimal_facts


def test_minimal_facts_validate_clean() -> None:
    outcome = validate_facts(minimal_facts(), check_counts=False)
    assert outcome.conflicts == []
    assert outcome.blocking == []


def test_expected_counts_match_v6_entity_panorama() -> None:
    """8 人 / 8 机 / 12 课目 / 6 空域 / 2 跑道 / 14 条规则。"""
    assert EXPECTED_COUNTS == {
        "persons": 8,
        "aircraft": 8,
        "missions": 12,
        "airspaces": 6,
        "runways": 2,
        "rules": 14,
    }


def test_entity_count_mismatch_is_reported() -> None:
    problems = check_entity_counts(minimal_facts())
    assert any("persons 实体数为 2" in p for p in problems)


# ── 引用完整性 ───────────────────────────────────────────────────────
def test_orphan_capable_mission_is_caught() -> None:
    facts = minimal_facts()
    broken = facts.aircraft[0].model_copy(update={"capable_missions": ("missionA-1", "missionH-1")})
    problems = check_referential_integrity(facts.model_copy(update={"aircraft": (broken,)}))
    assert any("missionH-1 不存在于课目表" in p for p in problems)


def test_unknown_airspace_name_is_caught() -> None:
    facts = minimal_facts()
    broken = facts.missions[0].model_copy(update={"airspace_name": "Large Area C"})
    problems = check_referential_integrity(
        facts.model_copy(update={"missions": (broken, facts.missions[1])})
    )
    assert any("Large Area C" in p for p in problems)


def test_airspace_binding_must_mirror_mission_airspace() -> None:
    """两份源文件说的是同一件事，对不上就是数据打架。"""
    facts = minimal_facts()
    broken = facts.airspaces[0].model_copy(update={"bound_missions": ()})
    problems = check_referential_integrity(
        facts.model_copy(update={"airspaces": (broken, facts.airspaces[1])})
    )
    assert any("绑定课目" in p and "不一致" in p for p in problems)


def test_dangling_mission_prereq_is_caught() -> None:
    from backend.ingestion.schema import IngestedPrereq

    facts = minimal_facts()
    broken = facts.missions[1].model_copy(
        update={"prereqs": (IngestedPrereq(prereq_ref="missionZ-9", ref_kind="mission"),)}
    )
    problems = check_referential_integrity(
        facts.model_copy(update={"missions": (facts.missions[0], broken)})
    )
    assert any("missionZ-9" in p for p in problems)


# ── 值域 ─────────────────────────────────────────────────────────────
def test_duplicate_primary_key_is_caught() -> None:
    facts = minimal_facts()
    dupe = facts.model_copy(update={"persons": (facts.persons[0], facts.persons[0])})
    assert any("主键重复" in p for p in check_value_domains(dupe))


def test_runway_serving_unknown_type_is_caught() -> None:
    facts = minimal_facts()
    broken = facts.runways[0].model_copy(update={"aircraft_types": ("JL-9",)})
    assert any(
        "机队里没有" in p
        for p in check_value_domains(facts.model_copy(update={"runways": (broken,)}))
    )


def test_mission_type_disagreeing_with_fleet_is_caught() -> None:
    facts = minimal_facts()
    broken = facts.missions[0].model_copy(update={"aircraft_types": ("JL-9",)})
    problems = check_value_domains(
        facts.model_copy(update={"missions": (broken, facts.missions[1])})
    )
    assert any("反推出" in p for p in problems)


# ── 时间逻辑 ─────────────────────────────────────────────────────────
def test_duplicate_unavailable_dates_are_caught() -> None:
    facts = minimal_facts()
    person = make_person(
        "P06",
        identity="学员",
        quals=(("A", "单飞"), ("B", "带飞")),
        unavailable=(date(2026, 1, 5), date(2026, 1, 5)),
    )
    problems = check_time_logic(facts.model_copy(update={"persons": (person,)}))
    assert any("不可用日期有重复" in p for p in problems)


def test_time_logic_clean_on_minimal_facts() -> None:
    assert check_time_logic(minimal_facts()) == []


# ── 后置断言接进主流程 ───────────────────────────────────────────────
def test_validate_blocks_on_orphan_token() -> None:
    facts = minimal_facts()
    broken = facts.persons[1].model_copy(update={"completed_missions": ("sionB-1",)})
    with pytest.raises(IngestionError, match="残缺课目编号"):
        validate_facts(
            facts.model_copy(update={"persons": (facts.persons[0], broken)}), check_counts=False
        )


def test_validate_collects_all_problems_at_once() -> None:
    """一次报完整清单，不挤牙膏。"""
    facts = minimal_facts()
    broken_aircraft = facts.aircraft[0].model_copy(
        update={"capable_missions": ("missionA-1", "missionH-1")}
    )
    broken_runway = facts.runways[0].model_copy(update={"aircraft_types": ("JL-9",)})
    with pytest.raises(IngestionError) as exc:
        validate_facts(
            facts.model_copy(update={"aircraft": (broken_aircraft,), "runways": (broken_runway,)}),
            check_counts=False,
        )
    assert len(exc.value.details["problems"]) >= 2


# ── §3.1.1 机组编成断言 ──────────────────────────────────────────────
def test_crew_composition_assertion_passes_on_valid_facts() -> None:
    assert_crew_composition(minimal_facts())


def test_crew_composition_assertion_blocks_student_dual_in_a_class() -> None:
    """出口标准里那条构造违例：把学员 A 类改回「带飞」。"""
    facts = minimal_facts()
    student = make_person(
        "P05", identity="学员", quals=(("A", "带飞"), ("B", "带飞")), completed=("missionA-1",)
    )
    broken = facts.model_copy(update={"persons": (facts.persons[0], student)})

    with pytest.raises(DataConflictError, match="机组编成一致性断言失败"):
        assert_crew_composition(broken)
    # 走主入口同样会被拦
    with pytest.raises(DataConflictError):
        validate_facts(broken, check_counts=False)


def test_validate_reports_x4_as_warning_not_error() -> None:
    outcome = validate_facts(
        minimal_facts(), [("x.pdf", "发布日期:2026-01-26")], check_counts=False
    )
    assert len(outcome.warnings) == 1
    assert outcome.blocking == []


def test_validate_empty_facts_without_count_check() -> None:
    outcome = validate_facts(IngestedFacts(), check_counts=False)
    assert outcome.conflicts == []
