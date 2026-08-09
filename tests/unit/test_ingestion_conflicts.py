"""X1~X4 冲突检出与裁定映射单测（v6 §5.5）。"""

from __future__ import annotations

from datetime import date

import pytest

from backend.core.errors import DataConflictError, ErrorCode, IngestionError
from backend.ingestion.conflicts import (
    ADJUDICATIONS,
    BASELINE_WEEK,
    apply_x1_resolution,
    detect_all,
    detect_x1_expiry_conflicts,
    detect_x3_crew_composition,
    detect_x4_publish_dates,
    expected_qualification_level,
    raise_on_fatal,
    verify_x2_no_variants,
)
from backend.ingestion.schema import IngestedFacts
from tests.fixtures.ingestion_facts import make_mission, make_person, minimal_facts


# ── X1：刘斌 C 类到期日 ──────────────────────────────────────────────
def test_x1_detected_when_summary_and_detail_disagree() -> None:
    person = make_person(
        "P04",
        identity="成熟飞行员",
        name="刘斌",
        quals=(("C", "单飞"),),
        expiries={"C": date(2026, 2, 7)},
        recurrent_due_raw="仪表等级(C类):2026-01-07",
    )
    conflicts = detect_x1_expiry_conflicts([person])
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.kind == "X1_刘斌C类到期日"
    assert c.severity == "BLOCKING"
    assert (c.value_a, c.value_b) == ("2026-01-07", "2026-02-07")
    assert c.adjudicated_value == "2026-01-07"
    assert c.error_code is ErrorCode.DATA_INTEGRITY_OR_CONFLICT


def test_x1_silent_when_both_sides_agree() -> None:
    person = make_person(
        "P04",
        identity="成熟飞行员",
        name="刘斌",
        quals=(("C", "单飞"),),
        expiries={"C": date(2026, 1, 7)},
        recurrent_due_raw="仪表等级(C类):2026-01-07",
    )
    assert detect_x1_expiry_conflicts([person]) == []


def test_x1_silent_when_no_recurrent_due() -> None:
    person = make_person("P05", identity="学员", quals=(("A", "单飞"),))
    assert detect_x1_expiry_conflicts([person]) == []


def test_adjudication_table_pins_2026_01_07() -> None:
    """§5.5 裁定：取总表的 2026-01-07，明细表的 02-07 视为笔误。"""
    assert ADJUDICATIONS["X1_刘斌C类到期日"].value == "2026-01-07"


def test_apply_x1_resolution_rewrites_only_that_class() -> None:
    person = make_person(
        "P04",
        identity="成熟飞行员",
        name="刘斌",
        quals=(("B", "单飞"), ("C", "单飞")),
        expiries={"C": date(2026, 2, 7)},
    )
    updated = apply_x1_resolution(person, "C", date(2026, 1, 7))
    by_class = {q.mission_class: q.expiry_date for q in updated.qualifications}
    assert by_class["C"] == date(2026, 1, 7)
    assert by_class["B"] is None


# ── X2：课目编号变体 ─────────────────────────────────────────────────
def test_x2_verification_passes_on_clean_facts() -> None:
    verify_x2_no_variants(minimal_facts())


def test_x2_verification_blocks_leftover_variant() -> None:
    """修复层退化时必须「响」，不能被这里悄悄兜住。"""
    facts = minimal_facts()
    broken = facts.aircraft[0].model_copy(update={"capable_missions": ("missionC1",)})
    with pytest.raises(IngestionError, match="未归一化的课目编号变体"):
        verify_x2_no_variants(facts.model_copy(update={"aircraft": (broken,)}))


# ── X3：机组编成一致性（§3.1.1 判定式） ──────────────────────────────
@pytest.mark.parametrize(
    ("identity", "dual_required", "expected"),
    [
        ("教员", True, "教员"),
        ("教员", False, "教员"),
        ("成熟飞行员", True, "单飞"),
        ("成熟飞行员", False, "单飞"),
        ("学员", True, "带飞"),
        ("学员", False, "单飞"),  # ★ D-1：A-1/A-2 带飞列为否 → 学员 A 类单飞
    ],
)
def test_expected_level_matches_the_decision_formula(
    identity: str, dual_required: bool, expected: str
) -> None:
    mission = make_mission("missionA-1", airspace_name="SAA", dual_required=dual_required)
    assert expected_qualification_level(identity, mission) == expected


def test_x3_passes_on_consistent_facts() -> None:
    facts = minimal_facts()
    assert detect_x3_crew_composition(facts.persons, facts.missions) == []


def test_x3_catches_the_2026_08_06_regression() -> None:
    """把学员 A 类改回「带飞」—— 正是 08-06 那个真实触发的冲突。"""
    facts = minimal_facts()
    student = make_person(
        "P05", identity="学员", quals=(("A", "带飞"), ("B", "带飞")), completed=("missionA-1",)
    )
    broken = facts.model_copy(update={"persons": (facts.persons[0], student)})

    conflicts = detect_x3_crew_composition(broken.persons, broken.missions)
    assert len(conflicts) == 1
    assert conflicts[0].kind == "X3_机组编成口径"
    assert conflicts[0].severity == "FATAL"
    assert conflicts[0].details["expected_level"] == "单飞"
    assert conflicts[0].details["actual_level"] == "带飞"

    with pytest.raises(DataConflictError) as exc:
        raise_on_fatal(conflicts)
    assert exc.value.code is ErrorCode.DATA_INTEGRITY_OR_CONFLICT


def test_x3_flags_split_dual_column_inside_one_class() -> None:
    """同一类别里「带飞」列取值不一致，本身就是源数据打架。"""
    missions = (
        make_mission("missionA-1", airspace_name="Small Area A", dual_required=False),
        make_mission("missionA-2", airspace_name="Small Area A", dual_required=True),
    )
    persons = (make_person("P05", identity="学员", quals=(("A", "单飞"),)),)
    conflicts = detect_x3_crew_composition(persons, missions)
    assert conflicts and conflicts[0].severity == "FATAL"
    assert "取值不一致" in conflicts[0].message


def test_raise_on_fatal_is_noop_without_fatal() -> None:
    raise_on_fatal([])


# ── X4：发布日期晚于基准周 ───────────────────────────────────────────
def test_x4_needs_a_reference_period_to_mean_anything() -> None:
    """不给参考排班周就不检查 —— 拿一个写死的周去卡任意数据只会制造噪声。"""
    assert detect_x4_publish_dates([("x.pdf", "发布日期:2099-01-01")]) == []


def test_x4_warns_but_never_blocks() -> None:
    conflicts = detect_x4_publish_dates(
        [("personnel.pdf", "发布单位:x 发布日期:2026-01-26\n正文")],
        reference_period=BASELINE_WEEK,
    )
    assert len(conflicts) == 1
    assert conflicts[0].severity == "WARN"
    assert conflicts[0].kind == "X4_发布日期晚于排班周"
    # WARN 不进人工门禁
    assert not conflicts[0].requires_human_gate
    raise_on_fatal(conflicts)  # 不该抛


def test_x4_silent_when_published_within_baseline_week() -> None:
    assert (
        detect_x4_publish_dates([("x.pdf", "发布日期:2026-01-06")], reference_period=BASELINE_WEEK)
        == []
    )


def test_x4_silent_without_publish_date() -> None:
    assert (
        detect_x4_publish_dates([("x.pdf", "没有发布日期字段")], reference_period=BASELINE_WEEK)
        == []
    )


# ── 汇总 ─────────────────────────────────────────────────────────────
def test_detect_all_runs_every_check() -> None:
    facts = minimal_facts()
    person = make_person(
        "P04",
        identity="成熟飞行员",
        name="刘斌",
        quals=(("A", "单飞"), ("B", "单飞")),
        expiries={"B": date(2026, 2, 7)},
        recurrent_due_raw="导航(B类):2026-01-07",
        completed=("missionA-1", "missionB-1"),
    )
    facts = facts.model_copy(update={"persons": (*facts.persons, person)})
    conflicts = detect_all(
        facts, [("x.pdf", "发布日期:2026-01-26")], reference_period=BASELINE_WEEK
    )
    kinds = {c.kind for c in conflicts}
    assert any(k.startswith("X1_") for k in kinds)
    assert "X4_发布日期晚于排班周" in kinds


def test_detect_all_on_empty_facts() -> None:
    assert detect_all(IngestedFacts()) == []
