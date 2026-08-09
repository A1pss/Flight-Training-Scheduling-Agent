"""**系统按用户上传的数据排班，不按基准周数据。**

这组测试钉的是产品前提，不是代码分支。它防的是一类很具体的交付事故：
代码全照着 `data/origin/` 那四份 PDF 写，验收时业务方换一批真实数据上来，
系统要么跑不动、要么拿基准数据顶替。

三条不变量：

1. **实体规模由数据决定** —— 9 个人、10 架飞机、15 门课目都必须能过；
   基准规模（8/8/12/6/2/14）只是**基准回归护栏**，不跑在上传路径上
2. **编号与机型由数据决定** —— 不写死位数、不写死 JL-8/JL-9
3. **少传数据就提问，绝不拿旧数据顶替**
"""

from __future__ import annotations

from datetime import date, time

import pytest
from pydantic import ValidationError

from backend.core.errors import IngestionError
from backend.ingestion.questions import REQUIRED_ENTITY_DOCS, detect_missing_inputs
from backend.ingestion.schema import (
    IngestedAircraft,
    IngestedAirspace,
    IngestedFacts,
    IngestedMission,
    IngestedPrereq,
)
from backend.ingestion.validate import (
    BASELINE_ENTITY_COUNTS,
    check_known_enums,
    validate_facts,
)
from tests.fixtures.ingestion_facts import make_mission, make_person, minimal_facts


def _aircraft(aircraft_id: str, aircraft_type: str, missions: tuple[str, ...]) -> IngestedAircraft:
    return IngestedAircraft(
        aircraft_id=aircraft_id,
        aircraft_type=aircraft_type,
        seats=2,
        daily_window_start=time(6, 0),
        daily_window_end=time(18, 0),
        turnaround_minutes=30,
        capable_missions=missions,
    )


def _other_org_facts() -> IngestedFacts:
    """一家**完全不同**的训练单位：120 人、105 架、3 种机型、类别到 J、三位编号。

    刻意把基准周的每一个「巧合」都改掉：人数不是 8、机型不是 JL-8/JL-9、
    类别超出 A~H、编号超过两位、课目序号超过一位。
    """
    missions = (
        make_mission(
            "missionA-1",
            airspace_name="北区",
            dual_required=False,
            aircraft_types=("CJ-6",),
        ),
        make_mission(
            "missionA-10",
            airspace_name="北区",
            dual_required=False,
            aircraft_types=("CJ-6",),
        ),
        make_mission(
            "missionJ-1",
            airspace_name="南区",
            dual_required=True,
            aircraft_types=("K-8", "L-15"),
            prereqs=(IngestedPrereq(prereq_ref="A类", ref_kind="class"),),
        ),
    )
    fleet = (
        _aircraft("AC100", "CJ-6", ("missionA-1", "missionA-10")),
        _aircraft("AC101", "K-8", ("missionJ-1",)),
        _aircraft("AC102", "L-15", ("missionJ-1",)),
    )
    persons = (
        make_person(
            "P100",
            identity="教员",
            quals=(("A", "教员"), ("J", "教员")),
            aircraft_types=("CJ-6", "K-8", "L-15"),
            completed=("missionA-1", "missionA-10", "missionJ-1"),
        ),
        make_person(
            "P101",
            identity="学员",
            quals=(("A", "单飞"), ("J", "带飞")),
            aircraft_types=("CJ-6", "K-8"),
            completed=("missionA-1",),
        ),
    )
    airspaces = (
        IngestedAirspace(
            airspace_id="NORTH",
            name="北区",
            capacity=3,
            bound_missions=("missionA-1", "missionA-10"),
        ),
        IngestedAirspace(
            airspace_id="SOUTH", name="南区", capacity=1, bound_missions=("missionJ-1",)
        ),
    )
    return IngestedFacts(persons=persons, aircraft=fleet, missions=missions, airspaces=airspaces)


# ── 不变量 1：实体规模由数据决定 ──────────────────────────────────────
def test_a_completely_different_organisation_ingests_cleanly() -> None:
    """换一家单位的数据，摄取校验必须干净通过 —— 一行代码都不用改。"""
    outcome = validate_facts(_other_org_facts())
    assert outcome.questions == []
    assert [c for c in outcome.conflicts if c.severity != "WARN"] == []


@pytest.mark.parametrize("extra_persons", [1, 5, 40])
def test_headcount_is_not_capped_by_the_baseline(extra_persons: int) -> None:
    facts = minimal_facts()
    grown = facts.persons + tuple(
        make_person(f"P{50 + i}", identity="学员", quals=(("A", "单飞"), ("B", "带飞")))
        for i in range(extra_persons)
    )
    validate_facts(facts.model_copy(update={"persons": grown}))  # 不该抛


def test_baseline_counts_are_a_regression_guard_not_a_limit() -> None:
    """基准规模只有在**显式传进来**时才生效，且只用于基准回归。"""
    facts = minimal_facts()
    # 默认路径：不检查
    validate_facts(facts)
    # 基准回归路径：检查，并且会发现 minimal_facts 不是基准规模
    with pytest.raises(IngestionError) as exc:
        validate_facts(facts, expected_counts=BASELINE_ENTITY_COUNTS)
    assert any("实体数为" in p for p in exc.value.details["problems"])


def test_baseline_counts_still_describe_the_baseline_dataset() -> None:
    assert BASELINE_ENTITY_COUNTS == {
        "persons": 8,
        "aircraft": 8,
        "missions": 12,
        "airspaces": 6,
        "runways": 2,
        "rules": 14,
    }


# ── 不变量 2：编号与机型由数据决定 ────────────────────────────────────
@pytest.mark.parametrize("person_id", ["P1", "P08", "P100", "P99999"])
def test_person_id_is_not_capped_at_two_digits(person_id: str) -> None:
    make_person(person_id, identity="学员", quals=(("A", "单飞"),))


@pytest.mark.parametrize("aircraft_id", ["AC1", "AC73", "AC100", "AC99999"])
def test_aircraft_id_is_not_capped_at_two_digits(aircraft_id: str) -> None:
    _aircraft(aircraft_id, "JL-8", ())


@pytest.mark.parametrize("aircraft_type", ["JL-8", "JL-9", "JL-10", "CJ-6", "L-15", "初教-6"])
def test_aircraft_type_is_whatever_the_fleet_says(aircraft_type: str) -> None:
    """机型不是枚举 —— 机队里出现什么就是什么。"""
    assert _aircraft("AC10", aircraft_type, ()).aircraft_type == aircraft_type


@pytest.mark.parametrize("mission_id", ["missionA-1", "missionA-10", "missionI-1", "missionZ-99"])
def test_mission_id_allows_new_classes_and_multi_digit_index(mission_id: str) -> None:
    mission = IngestedMission(
        mission_id=mission_id,
        name="x",
        mission_class=mission_id[len("mission")],
        kind="实装飞行课",
        duration_minutes=30,
        cycle_weeks=12,
        freq_days=3,
        dual_required=False,
        aircraft_types=("JL-8",),
        airspace_name="SAA",
    )
    assert mission.mission_id == mission_id


@pytest.mark.parametrize("bad", ["P0A", "PP01", "01", "person01"])
def test_person_id_still_rejects_malformed_values(bad: str) -> None:
    """放宽不等于放弃 —— 前缀约定（v6 §5.1 ④）仍然是错别字的第一道闸。"""
    with pytest.raises(ValidationError, match=r"String should match"):
        make_person(bad, identity="学员", quals=(("A", "单飞"),))


def test_unknown_identity_blocks_with_an_actionable_message() -> None:
    """新身份不是「缺数据」，是**新业务语义** —— 要说清楚该找谁做什么决定。"""
    facts = minimal_facts()
    stranger = make_person("P09", identity="见习教员", quals=(("A", "单飞"), ("B", "带飞")))
    broken = facts.model_copy(update={"persons": (*facts.persons, stranger)})

    problems = check_known_enums(broken)
    assert len(problems) == 1
    assert "见习教员" in problems[0]
    assert "机组编成" in problems[0] and "业务方" in problems[0]

    with pytest.raises(IngestionError) as exc:
        validate_facts(broken)
    assert any("见习教员" in p for p in exc.value.details["problems"])


def test_unknown_qualification_level_blocks_too() -> None:
    facts = minimal_facts()
    stranger = make_person("P09", identity="学员", quals=(("A", "见习"), ("B", "带飞")))
    problems = check_known_enums(facts.model_copy(update={"persons": (stranger,)}))
    assert any("见习" in p for p in problems)


# ── 不变量 3：少传数据就提问，绝不拿旧数据顶替 ────────────────────────
def test_required_document_classes() -> None:
    """跑道不在必需清单里 —— 业务方 2026-08-10 确认它维持配置形态。"""
    assert [attr for attr, _, _ in REQUIRED_ENTITY_DOCS] == [
        "persons",
        "aircraft",
        "missions",
        "airspaces",
    ]


@pytest.mark.parametrize("dropped", ["persons", "aircraft", "missions", "airspaces"])
def test_missing_any_required_class_produces_an_upload_request(dropped: str) -> None:
    facts = minimal_facts().model_copy(update={dropped: ()})
    questions = detect_missing_inputs(facts)
    assert [q.question_id for q in questions] == [f"Q_missing_{dropped}"]
    q = questions[0]
    # 「补传文件」不是「回答一个值」
    assert q.resolution == "upload"
    assert "不会" in q.why_it_matters and "顶替" in q.why_it_matters


def test_missing_class_short_circuits_before_integrity_noise() -> None:
    """少一份课目文件 → 一句「请补传课目标准」，而不是一屏外键错误。"""
    facts = minimal_facts().model_copy(update={"missions": (), "airspaces": ()})
    outcome = validate_facts(facts)
    assert {q.question_id for q in outcome.questions} == {
        "Q_missing_missions",
        "Q_missing_airspaces",
    }
    # 短路：不再顺带吐引用完整性问题
    assert outcome.conflicts == []


def test_complete_upload_asks_nothing_about_missing_files() -> None:
    assert detect_missing_inputs(_other_org_facts()) == []


def test_gate_cannot_be_satisfied_by_answering_an_upload_request() -> None:
    """缺文件的问题**给什么值都没用**，只能补传。"""
    from backend.ingestion.diff import build_changeset
    from backend.ingestion.gate import review
    from backend.ingestion.questions import QuestionAnswer

    facts = minimal_facts().model_copy(update={"missions": ()})
    questions = detect_missing_inputs(facts)
    changeset = build_changeset(facts, questions=questions)

    decision = review(
        changeset,
        answers={
            "Q_missing_missions": QuestionAnswer(
                question_id="Q_missing_missions", value="随便给个值", answered_by="alps"
            )
        },
        approver="alps",
    )
    assert not decision.approved
    assert any("需要补传文件" in r for r in decision.reasons)
    assert [q.question_id for q in decision.pending_questions] == ["Q_missing_missions"]


def test_cycle_start_question_is_not_asked_while_files_are_missing() -> None:
    """先把文件补齐，再谈周期起点 —— 一次只让用户处理一件事。"""
    from backend.ingestion.questions import QID_CYCLE_START

    facts = minimal_facts().model_copy(update={"missions": ()})
    ids = {q.question_id for q in validate_facts(facts).questions}
    assert QID_CYCLE_START not in ids


def test_x1_adjudication_is_not_offered_when_values_do_not_match() -> None:
    """同名同类别但取值不同的冲突，**不拿历史裁定当建议值**。"""
    from backend.ingestion.conflicts import detect_x1_expiry_conflicts

    person = make_person(
        "P04",
        identity="成熟飞行员",
        name="刘斌",
        quals=(("C", "单飞"),),
        expiries={"C": date(2030, 5, 5)},
        recurrent_due_raw="仪表等级(C类):2030-04-04",
    )
    conflict = detect_x1_expiry_conflicts([person])[0]
    assert conflict.kind == "X1_刘斌C类到期日"
    # §5.5 裁定的 2026-01-07 不是本次冲突的任一侧 → 不给建议，交给人
    assert conflict.adjudicated_value is None
