"""抽取层单测 —— **跑真实 PDF**，逐字对着 v6 §1.3 的实体全景表核。

单测直接读 `data/origin/*.pdf` 是刻意的：这四份文件已纳入版本管理、体积很小、
不依赖任何服务，而它们恰恰是修复层与跨页合并的唯一真实样本。用手搓的假表格
测 parser，等于把最容易出错的那部分排除在测试之外。
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from backend.core.config import PROJECT_ROOT
from backend.core.errors import IngestionError
from backend.ingestion.adapters import ExtractedDocument, extract_pdf
from backend.ingestion.classify import classify_by_rules, classify_document
from backend.ingestion.parsers import (
    parse_aircraft_document,
    parse_missions_document,
    parse_personnel_document,
    parse_rules_document,
    parse_runways_from_semantics,
)
from backend.ingestion.parsers.aircraft import parse_maintenance
from backend.ingestion.parsers.missions import parse_frequency, parse_prereqs
from backend.ingestion.parsers.personnel import parse_qualifications, parse_recurrent_due

ORIGIN = PROJECT_ROOT / "data" / "origin"


@pytest.fixture(scope="module")
def personnel_doc() -> ExtractedDocument:
    return extract_pdf(ORIGIN / "personnel.pdf")


@pytest.fixture(scope="module")
def aircraft_doc() -> ExtractedDocument:
    return extract_pdf(ORIGIN / "aircraft.pdf")


@pytest.fixture(scope="module")
def missions_doc() -> ExtractedDocument:
    return extract_pdf(ORIGIN / "missions.pdf")


@pytest.fixture(scope="module")
def rules_doc() -> ExtractedDocument:
    return extract_pdf(ORIGIN / "rules.pdf")


# ── 分类器 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("personnel.pdf", "人员档案"),
        ("aircraft.pdf", "飞机资源"),
        ("missions.pdf", "课目标准"),
        ("rules.pdf", "规则条文"),
    ],
)
def test_rule_classifier_hits_every_baseline_pdf(filename: str, expected: str) -> None:
    """四份基准 PDF 全部走规则命中，一次 LLM 调用都不需要。"""
    doc = extract_pdf(ORIGIN / filename)
    hit = classify_by_rules(doc.text)
    assert hit is not None
    assert hit.doc_class == expected
    assert hit.by == "rule"


def test_classifier_returns_unknown_without_provider() -> None:
    result = classify_document("一段完全不相干的文本")
    assert result.doc_class == "未知"


# ── personnel.pdf ────────────────────────────────────────────────────
def test_personnel_matches_v6_entity_table(personnel_doc: ExtractedDocument) -> None:
    persons = parse_personnel_document(personnel_doc)
    assert len(persons) == 8

    by_id = {p.person_id: p for p in persons}
    assert [p.name for p in persons] == [
        "孙军",
        "高超",
        "吴鹏",
        "刘斌",
        "罗磊",
        "张勇",
        "陈伟",
        "何超",
    ]

    # 3 教员 + 1 成熟飞行员 + 4 学员
    identities = [p.identity for p in persons]
    assert identities.count("教员") == 3
    assert identities.count("成熟飞行员") == 1
    assert identities.count("学员") == 4

    # 教员持双机型且完成全 12 门；学员只持 JL-8
    assert by_id["P01"].aircraft_types == ("JL-8", "JL-9")
    assert len(by_id["P01"].completed_missions) == 12
    assert by_id["P05"].aircraft_types == ("JL-8",)

    # 已完成课目逐人核对（v6 §1.3.1）
    assert by_id["P05"].completed_missions == (
        "missionA-1",
        "missionA-2",
        "missionB-1",
        "missionB-2",
        "missionC-1",
    )
    assert by_id["P06"].completed_missions == ("missionA-1", "missionA-2")
    assert by_id["P07"].completed_missions == ("missionA-1", "missionA-2", "missionB-1")
    assert by_id["P08"].completed_missions == ("missionA-1",)

    # 吴鹏 01-05 不可用
    assert by_id["P03"].unavailable_dates == (date(2026, 1, 5),)
    assert by_id["P01"].unavailable_dates == ()


def test_students_hold_solo_a_class_per_d1(personnel_doc: ExtractedDocument) -> None:
    """D-1：学员 A 类等级是「单飞」，与 missions.pdf 的带飞=否 一致。"""
    persons = {p.person_id: p for p in parse_personnel_document(personnel_doc)}
    for student in ("P05", "P06", "P07", "P08"):
        levels = {q.mission_class: q.level for q in persons[student].qualifications}
        assert levels["A"] == "单飞"
        assert levels["B"] == levels["C"] == levels["F"] == "带飞"
        # 学员没有 D/E/G/H 类资质
        assert set(levels) == {"A", "B", "C", "F"}


def test_parser_records_both_sides_of_x1_without_choosing(
    personnel_doc: ExtractedDocument,
) -> None:
    """§5.5 明令「不要在 parser 里悄悄选一个」。"""
    liu = {p.person_id: p for p in parse_personnel_document(personnel_doc)}["P04"]
    # 明细表侧：2026-02-07（原样保留，未被裁定覆盖）
    c_qual = next(q for q in liu.qualifications if q.mission_class == "C")
    assert c_qual.expiry_date == date(2026, 2, 7)
    # 总表侧：2026-01-07（原文保留）
    assert liu.recurrent_due_raw == "仪表等级(C类):2026-01-07"


def test_parse_recurrent_due() -> None:
    assert parse_recurrent_due("仪表等级(C类):2026-01-07") == ("C", date(2026, 1, 7))
    assert parse_recurrent_due("—") is None
    with pytest.raises(IngestionError, match="复训到期"):
        parse_recurrent_due("看不懂的内容")


def test_parse_qualifications_rejects_unparsed_residue() -> None:
    """静默丢弃残留 = 部分入库（铁律 7）。"""
    with pytest.raises(IngestionError, match="残留片段"):
        parse_qualifications("P05", "A类/单飞;???")


# ── aircraft.pdf ─────────────────────────────────────────────────────
def test_aircraft_fleet_matches_v6(aircraft_doc: ExtractedDocument) -> None:
    fleet, airspaces = parse_aircraft_document(aircraft_doc)
    assert len(fleet) == 8

    by_type: dict[str, list[str]] = {}
    for a in fleet:
        by_type.setdefault(a.aircraft_type, []).append(a.aircraft_id)

    # ⚠️ AC73 是 JL-8 不是 JL-9；JL-9 只有 AC84/AC95
    assert by_type["JL-8"] == ["AC10", "AC27", "AC34", "AC49", "AC61", "AC73"]
    assert by_type["JL-9"] == ["AC84", "AC95"]

    by_id = {a.aircraft_id: a for a in fleet}
    assert by_id["AC10"].turnaround_minutes == 30
    assert by_id["AC84"].turnaround_minutes == 40
    assert by_id["AC10"].daily_window_start == time(6, 0)
    assert by_id["AC10"].daily_window_end == time(18, 0)
    assert all(a.seats == 2 for a in fleet)

    # X2 现场：JL-8 的适配课目必须包含归一化后的 missionC-1
    assert "missionC-1" in by_id["AC10"].capable_missions
    assert len(by_id["AC10"].capable_missions) == 7
    assert len(by_id["AC84"].capable_missions) == 10

    assert len(airspaces) == 6


def test_ac73_maintenance(aircraft_doc: ExtractedDocument) -> None:
    fleet, _ = parse_aircraft_document(aircraft_doc)
    ac73 = next(a for a in fleet if a.aircraft_id == "AC73")
    assert len(ac73.maintenance) == 1
    entry = ac73.maintenance[0]
    assert entry.start_ts == datetime(2026, 1, 9, 0, 0)
    assert entry.all_day is True
    # 其余七架无维护
    assert all(not a.maintenance for a in fleet if a.aircraft_id != "AC73")


def test_airspace_capacities(aircraft_doc: ExtractedDocument) -> None:
    _, airspaces = parse_aircraft_document(aircraft_doc)
    caps = {a.airspace_id: a.capacity for a in airspaces}
    assert caps == {"SAA": 2, "SAB": 2, "IFR": 1, "RT1": 1, "RT2": 1, "RNG": 1}
    names = {a.airspace_id: a.name for a in airspaces}
    assert names["RNG"] == "Range Area"  # 第 2 页的续表被合并进来了


def test_parse_maintenance_edge_cases() -> None:
    assert parse_maintenance("AC10", "—") == ()
    with pytest.raises(IngestionError, match="维护计划"):
        parse_maintenance("AC10", "下周检修一下")


# ── missions.pdf ─────────────────────────────────────────────────────
def test_missions_match_v6(missions_doc: ExtractedDocument) -> None:
    missions = parse_missions_document(missions_doc)
    assert len(missions) == 12
    by_id = {m.mission_id: m for m in missions}

    assert by_id["missionA-1"].duration_minutes == 30
    assert by_id["missionA-2"].duration_minutes == 27
    assert by_id["missionE-2"].duration_minutes == 69

    # freq_days：A 类 3、B~F 类 7、G/H 类 14
    assert by_id["missionA-1"].freq_days == 3
    assert by_id["missionB-1"].freq_days == 7
    assert by_id["missionG-1"].freq_days == 14
    assert by_id["missionH-1"].freq_days == 14

    assert by_id["missionA-1"].cycle_weeks == 12
    assert by_id["missionB-1"].cycle_weeks == 16
    assert by_id["missionG-1"].cycle_weeks == 20


def test_a_class_is_weekly_required_and_solo(missions_doc: ExtractedDocument) -> None:
    """D-1 的落点：A-1/A-2 带飞列为「否」，且标了「每周必飞」。"""
    by_id = {m.mission_id: m for m in parse_missions_document(missions_doc)}
    for mid in ("missionA-1", "missionA-2"):
        assert by_id[mid].dual_required is False
        assert by_id[mid].weekly_required is True
    # 其余全部需带飞、且非每周必飞
    for mid, mission in by_id.items():
        if not mid.startswith("missionA-"):
            assert mission.dual_required is True
            assert mission.weekly_required is False


def test_prereqs_keep_class_refs_unexpanded(missions_doc: ExtractedDocument) -> None:
    """类引用的展开在 compile_spec，不在抽取层（v6 §6.1）。"""
    by_id = {m.mission_id: m for m in parse_missions_document(missions_doc)}
    assert by_id["missionA-1"].prereqs == ()
    assert [(p.prereq_ref, p.ref_kind) for p in by_id["missionB-1"].prereqs] == [("A类", "class")]
    assert [(p.prereq_ref, p.ref_kind) for p in by_id["missionC-2"].prereqs] == [
        ("missionC-1", "mission")
    ]
    assert [(p.prereq_ref, p.ref_kind) for p in by_id["missionD-1"].prereqs] == [
        ("B类", "class"),
        ("C类", "class"),
    ]
    assert [(p.prereq_ref, p.ref_kind) for p in by_id["missionG-1"].prereqs] == [
        ("A类", "class"),
        ("F类", "class"),
    ]


def test_mission_aircraft_types(missions_doc: ExtractedDocument) -> None:
    by_id = {m.mission_id: m for m in parse_missions_document(missions_doc)}
    assert by_id["missionA-1"].aircraft_types == ("JL-8",)
    assert by_id["missionB-1"].aircraft_types == ("JL-8", "JL-9")
    assert by_id["missionD-1"].aircraft_types == ("JL-9",)


def test_parse_frequency_variants() -> None:
    assert parse_frequency("m", "12周,每3天≥1次(每周必飞)") == (12, 3, True)
    assert parse_frequency("m", "16周,每7天≥1次") == (16, 7, False)
    with pytest.raises(IngestionError, match="周期/频率"):
        parse_frequency("m", "随便飞飞")


def test_parse_prereqs_rejects_unknown_form() -> None:
    with pytest.raises(IngestionError, match="既不是课目编号也不是类别"):
        parse_prereqs("missionX-1", "会飞就行")


# ── rules.pdf ────────────────────────────────────────────────────────
def test_rules_are_split_into_exactly_14(rules_doc: ExtractedDocument) -> None:
    rules = parse_rules_document(rules_doc)
    assert len(rules) == 14
    assert [r.rule_id for r in rules] == list(range(1, 15))
    assert all(r.hard_soft == "硬约束" for r in rules)


def test_rule_chunk_is_never_split_mid_sentence(rules_doc: ExtractedDocument) -> None:
    """单条约束是切分单元，禁止拆分 —— 每条都得自带完整的首尾。"""
    by_id = {r.rule_id: r for r in parse_rules_document(rules_doc)}
    rule9 = by_id[9]
    assert rule9.title == "起降密度限制"
    assert rule9.text.startswith("约束9(起降密度限制)【硬约束】")
    assert "20 分钟滑动窗口内起飞次数不得超过 2 次" in rule9.text
    assert "间隔不少于 7 分钟" in rule9.text
    assert rule9.text.rstrip().endswith("。")


def test_rules_parser_blocks_when_count_disagrees_with_preamble() -> None:
    doc = ExtractedDocument(
        path=ORIGIN / "fake.pdf",
        media_type="application/pdf",
        pages=["总则:本规则共 14 条。约束1(甲)【硬约束】:内容。"],
    )
    with pytest.raises(IngestionError, match="总则声明共 14 条"):
        parse_rules_document(doc)


def test_rules_parser_blocks_without_any_match() -> None:
    doc = ExtractedDocument(
        path=ORIGIN / "fake.pdf", media_type="application/pdf", pages=["什么都没有"]
    )
    with pytest.raises(IngestionError, match="未匹配到任何"):
        parse_rules_document(doc)


# ── 跑道（来自 semantics.yaml，不是 PDF）───────────────────────────────
def test_runways_come_from_semantics_not_pdf() -> None:
    runways = parse_runways_from_semantics()
    assert len(runways) == 2
    by_id = {r.runway_id: r for r in runways}
    # ⚠️ 不是「RWY-1=JL-8、RWY-2=JL-9」
    assert by_id["RWY-1"].aircraft_types == ("JL-8", "JL-9")
    assert by_id["RWY-2"].aircraft_types == ("JL-8",)


def test_runways_blocks_on_missing_switch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bad = tmp_path / "semantics.yaml"
    bad.write_text("switches: {}\n", encoding="utf-8")
    with pytest.raises(IngestionError, match="未定义 runways"):
        parse_runways_from_semantics(bad)
