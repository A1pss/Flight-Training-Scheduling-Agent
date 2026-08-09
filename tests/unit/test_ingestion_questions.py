"""课程周期起点的三条路径：文件里有 / 对话里说了 / 都没有就提问。

这组测试钉住的是一个**产品行为**，不只是代码分支：换一批带「课程开始日期」列的
数据，系统必须直接用文件里的值，一行代码都不用改；两边都没有时必须**问**，
不许悄悄填一个日期。
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.core.errors import IngestionError, RequiredInputMissingError
from backend.ingestion.diff import build_changeset, normalize_facts
from backend.ingestion.gate import (
    ConflictResolution,
    GateDecision,
    answered_cycle_start,
    baseline_answers,
    format_questions,
    review,
)
from backend.ingestion.loader import resolve_cycle_start
from backend.ingestion.parsers.missions import CYCLE_START_COLUMNS, parse_cycle_start
from backend.ingestion.questions import (
    BASELINE_ANSWERS,
    QID_CYCLE_START,
    OpenQuestion,
    QuestionAnswer,
    detect_open_questions,
    parse_answer,
)
from tests.fixtures.ingestion_facts import make_mission, minimal_facts


def _answer(value: str, **kw: object) -> QuestionAnswer:
    return QuestionAnswer(
        question_id=QID_CYCLE_START,
        value=value,
        answered_by="alps",
        **kw,  # type: ignore[arg-type]
    )


# ── 路径 ①：文件里有「课程开始日期」列 ────────────────────────────────
@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        ("2026-01-05", date(2026, 1, 5)),
        ("2026/01/05", date(2026, 1, 5)),
        ("2026年1月5日", date(2026, 1, 5)),
        ("2026年11月30日", date(2026, 11, 30)),
    ],
)
def test_parse_cycle_start_accepts_common_date_forms(cell: str, expected: date) -> None:
    assert parse_cycle_start("missionA-1", cell) == expected


def test_parse_cycle_start_treats_placeholder_as_absent() -> None:
    assert parse_cycle_start("missionA-1", "—") is None
    assert parse_cycle_start("missionA-1", "") is None


def test_parse_cycle_start_blocks_on_garbage() -> None:
    """写了看不懂的东西就阻断，不静默当作「没填」（铁律 7）。"""
    with pytest.raises(IngestionError, match="课程开始日期无法解析"):
        parse_cycle_start("missionA-1", "下个月吧")


def test_parse_cycle_start_blocks_on_impossible_date() -> None:
    with pytest.raises(IngestionError, match="不是合法日期"):
        parse_cycle_start("missionA-1", "2026-02-30")


def test_file_supplied_date_wins_and_asks_nothing() -> None:
    """文件给了日期 → 不产生问题，落库直接用文件里的值。"""
    facts = minimal_facts()
    dated = tuple(m.model_copy(update={"cycle_start": date(2027, 3, 1)}) for m in facts.missions)
    facts = facts.model_copy(update={"missions": dated})

    assert detect_open_questions(facts) == []
    assert resolve_cycle_start(dated[0], None) == date(2027, 3, 1)


def test_per_mission_dates_may_differ() -> None:
    """各门课目起点可以不同 —— 逐行读，不是全局一个值。"""
    a = make_mission("missionA-1", airspace_name="SAA", dual_required=False)
    b = make_mission("missionB-1", airspace_name="RT2", dual_required=True)
    a = a.model_copy(update={"cycle_start": date(2026, 1, 5)})
    b = b.model_copy(update={"cycle_start": date(2026, 6, 1)})
    assert resolve_cycle_start(a, None) == date(2026, 1, 5)
    assert resolve_cycle_start(b, None) == date(2026, 6, 1)


def test_column_aliases_are_documented() -> None:
    assert "课程开始日期" in CYCLE_START_COLUMNS
    assert "开始日期" in CYCLE_START_COLUMNS


# ── 路径 ②：文件没有，但用户答了 ──────────────────────────────────────
def test_answer_supplies_the_value_when_file_does_not() -> None:
    mission = minimal_facts().missions[0]
    assert mission.cycle_start is None
    assert resolve_cycle_start(mission, date(2026, 9, 1)) == date(2026, 9, 1)


def test_gate_accepts_a_valid_answer() -> None:
    facts = minimal_facts()
    changeset = build_changeset(facts, questions=detect_open_questions(facts))
    assert changeset.questions

    decision = review(changeset, answers={QID_CYCLE_START: _answer("2026-09-01")}, approver="alps")
    assert decision.approved
    assert answered_cycle_start(decision) == date(2026, 9, 1)


def test_answered_question_ids_suppress_re_asking() -> None:
    facts = minimal_facts()
    assert detect_open_questions(facts, provided=[QID_CYCLE_START]) == []


# ── 路径 ③：两边都没有 → 提问并阻断 ──────────────────────────────────
def test_question_is_raised_when_nobody_supplied_a_date() -> None:
    facts = minimal_facts()
    questions = detect_open_questions(facts)
    assert len(questions) == 1
    q = questions[0]
    assert q.question_id == QID_CYCLE_START
    assert q.value_kind == "date"
    # 问题文本里要点名是哪些课目缺
    assert "missionA-1" in q.applies_to["missions"]


def test_gate_refuses_and_returns_the_question() -> None:
    facts = minimal_facts()
    changeset = build_changeset(facts, questions=detect_open_questions(facts))

    decision = review(changeset, approver="alps")
    assert not decision.approved
    assert decision.outcome == "REJECTED"
    assert [q.question_id for q in decision.pending_questions] == [QID_CYCLE_START]
    assert any("尚未回答" in r for r in decision.reasons)


def test_gate_refuses_a_malformed_answer() -> None:
    facts = minimal_facts()
    changeset = build_changeset(facts, questions=detect_open_questions(facts))

    decision = review(changeset, answers={QID_CYCLE_START: _answer("下个月")}, approver="alps")
    assert not decision.approved
    assert any("不是合法日期" in r for r in decision.reasons)
    assert decision.pending_questions


def test_resolve_refuses_to_invent_a_date() -> None:
    """绕过门禁直接落库时的最后一道闸：抛错，不编日期。"""
    mission = minimal_facts().missions[0]
    with pytest.raises(RequiredInputMissingError, match="既不在文件里，也没有用户回答"):
        resolve_cycle_start(mission, None)


def test_no_change_outcome_requires_no_pending_questions() -> None:
    """有待答问题时不能因为「没变更」就放行。"""
    facts = minimal_facts()
    changeset = build_changeset(
        facts, normalize_facts(facts), questions=detect_open_questions(facts)
    )
    assert changeset.is_empty
    assert review(changeset, approver="alps").outcome == "REJECTED"


# ── 基准数据集：已问过、已答过，记录在案 ──────────────────────────────
def test_baseline_answers_carry_the_2026_08_09_decision() -> None:
    facts = minimal_facts()
    changeset = build_changeset(facts, questions=detect_open_questions(facts))
    answers = baseline_answers(changeset)
    assert answers[QID_CYCLE_START].value == "2026-01-05"
    assert answers[QID_CYCLE_START].source == "baseline"
    assert review(changeset, answers=answers, approver="baseline").approved


def test_baseline_answers_only_cover_recorded_questions() -> None:
    """换一批新数据、冒出没记录过的问题 → 照样会问，不会被自动放行。"""
    unknown = OpenQuestion(
        question_id="Q_未登记的问题",
        topic="测试",
        question="?",
        why_it_matters="?",
        value_kind="text",
    )
    changeset = build_changeset(minimal_facts(), questions=[unknown])
    assert baseline_answers(changeset) == {}
    assert not review(changeset, answers={}, approver="baseline").approved


def test_baseline_answer_table_is_explicit_about_provenance() -> None:
    entry = BASELINE_ANSWERS[QID_CYCLE_START]
    assert "2026-08-09" in entry.note
    assert entry.answered_by


# ── 答案解析与展示 ───────────────────────────────────────────────────
def test_parse_answer_by_kind() -> None:
    q_int = OpenQuestion(
        question_id="Q_i", topic="t", question="q", why_it_matters="w", value_kind="int"
    )
    assert parse_answer(q_int, QuestionAnswer(question_id="Q_i", value="7", answered_by="a")) == 7
    with pytest.raises(RequiredInputMissingError, match="不是整数"):
        parse_answer(q_int, QuestionAnswer(question_id="Q_i", value="七", answered_by="a"))

    q_text = OpenQuestion(
        question_id="Q_t", topic="t", question="q", why_it_matters="w", value_kind="text"
    )
    assert (
        parse_answer(q_text, QuestionAnswer(question_id="Q_t", value=" x ", answered_by="a")) == "x"
    )
    with pytest.raises(RequiredInputMissingError, match="答案为空"):
        parse_answer(q_text, QuestionAnswer(question_id="Q_t", value="   ", answered_by="a"))


def test_format_questions_is_user_facing_chinese() -> None:
    facts = minimal_facts()
    text = format_questions(detect_open_questions(facts))
    assert "【待确认】课程周期起点" in text
    assert "为什么要问" in text
    assert "课程开始日期" in text
    assert format_questions([]) == ""


def test_answered_cycle_start_is_none_when_never_asked() -> None:
    assert answered_cycle_start(GateDecision(outcome="APPROVED")) is None


def test_questions_and_conflicts_are_independent_gates() -> None:
    """既有 X1 冲突又有待答问题时，两个都得处理完才放行。"""
    from backend.ingestion.conflicts import detect_x1_expiry_conflicts
    from tests.fixtures.ingestion_facts import make_person

    person = make_person(
        "P04",
        identity="成熟飞行员",
        name="刘斌",
        quals=(("C", "单飞"),),
        expiries={"C": date(2026, 2, 7)},
        recurrent_due_raw="仪表等级(C类):2026-01-07",
    )
    facts = minimal_facts()
    changeset = build_changeset(
        facts,
        conflicts=detect_x1_expiry_conflicts([person]),
        questions=detect_open_questions(facts),
    )
    cid = changeset.blocking_conflicts[0].conflict_id

    # 只答问题、不裁冲突 → 拒绝
    only_answer = review(
        changeset, answers={QID_CYCLE_START: _answer("2026-01-05")}, approver="alps"
    )
    assert not only_answer.approved

    # 只裁冲突、不答问题 → 拒绝
    only_resolution = review(
        changeset,
        {cid: ConflictResolution(conflict_id=cid, chosen_value="2026-01-07", decided_by="alps")},
        approver="alps",
    )
    assert not only_resolution.approved

    # 两个都给 → 放行
    both = review(
        changeset,
        {cid: ConflictResolution(conflict_id=cid, chosen_value="2026-01-07", decided_by="alps")},
        answers={QID_CYCLE_START: _answer("2026-01-05")},
        approver="alps",
    )
    assert both.approved
