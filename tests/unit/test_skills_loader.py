"""Skill 加载器与确定性路由（v6 §7.8）。

**这个文件里最重要的一条是 S3**：`authoritative` 未声明或声明为 `true` 一律
拒绝加载。它是 v6 §7.8.2 三重隔离机制里的第二重。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.config import PROJECT_ROOT
from backend.skills_loader import (
    DISCLAIMER,
    NO_SKILL_COMPONENTS,
    SKILL_ROUTES,
    SkillLoadError,
    SkillNotAuthoritativeError,
    all_routed_skills,
    diagnosis_conditions,
    ingest_conditions,
    load_library,
    missing_from_library,
    parse_skill,
    render_skills,
    route_for_component,
    route_skills,
)

SKILLS_DIR = PROJECT_ROOT / "skills"

#: 本仓库交付的 8 份 skill（v6 §7.8.1 的目录树）
EXPECTED_SKILLS = (
    "doc-parsing/aircraft",
    "doc-parsing/exception",
    "doc-parsing/mission",
    "doc-parsing/personnel",
    "doc-parsing/rules",
    "relaxation-playbook",
    "report-writing",
    "rule-interpretation",
)


def write_skill(root: Path, name: str, frontmatter: str, body: str = "正文") -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────────────
# 加载与校验
# ─────────────────────────────────────────────────────────────────────
def test_parses_a_well_formed_skill() -> None:
    skill = parse_skill(
        "---\nname: demo\ndescription: 演示\nversion: 2.0\n"
        "consumers: [explain_llm]\nauthoritative: false\n---\n正文\n",
        name="demo",
    )
    assert (skill.name, skill.version, skill.consumers) == ("demo", "2.0", ("explain_llm",))
    assert skill.authoritative is False
    assert skill.sha256


def test_missing_authoritative_is_rejected() -> None:
    """**缺省不等于 false** —— 缺省等于没人为这份文件的定位签过字。"""
    with pytest.raises(SkillNotAuthoritativeError, match="未声明"):
        parse_skill("---\nname: demo\ndescription: d\n---\n正文\n", name="demo")


@pytest.mark.parametrize("value", ["true", "True", "yes", "1"])
def test_authoritative_true_is_rejected(value: str) -> None:
    """§12.5.3 S3：构造一份 `authoritative: true` 的 skill → 加载器拒绝并报错。"""
    with pytest.raises(SkillNotAuthoritativeError, match="拒绝加载"):
        parse_skill(
            f"---\nname: demo\ndescription: d\nauthoritative: {value}\n---\n正文\n", name="demo"
        )


def test_missing_frontmatter_is_rejected() -> None:
    with pytest.raises(SkillLoadError, match="缺少 YAML frontmatter"):
        parse_skill("直接就是正文\n", name="demo")


def test_name_mismatch_is_rejected() -> None:
    with pytest.raises(SkillLoadError, match="不一致"):
        parse_skill(
            "---\nname: other\ndescription: d\nauthoritative: false\n---\n正文\n", name="demo"
        )


def test_empty_body_is_rejected() -> None:
    with pytest.raises(SkillLoadError, match="正文为空"):
        parse_skill("---\nname: demo\ndescription: d\nauthoritative: false\n---\n\n", name="demo")


def test_broken_skill_is_not_silently_skipped(tmp_path: Path) -> None:
    """一份坏了就抛 —— 静默跳过会伪装成「解释文本忽然变差」。"""
    write_skill(tmp_path, "good", "name: good\ndescription: d\nauthoritative: false")
    write_skill(tmp_path, "bad", "name: bad\ndescription: d\nauthoritative: true")
    with pytest.raises(SkillNotAuthoritativeError):
        load_library(tmp_path)


def test_missing_directory_is_a_warning_not_an_error(tmp_path: Path) -> None:
    """§12.5.3 S2 的前半：删掉整个目录不抛，只记 WARN。"""
    library = load_library(tmp_path / "nope")
    assert library.empty
    assert library.names() == ()
    assert library.fingerprint()  # 空库也有指纹（空串的 sha256）


def test_render_reports_unloaded_skills(tmp_path: Path) -> None:
    library = load_library(tmp_path / "nope")
    assert "未加载" in render_skills(library, ["rule-interpretation"])


# ─────────────────────────────────────────────────────────────────────
# 本仓库交付的 8 份
# ─────────────────────────────────────────────────────────────────────
def test_repository_ships_exactly_eight_skills() -> None:
    library = load_library(SKILLS_DIR)
    assert library.names() == EXPECTED_SKILLS


def test_every_skill_carries_the_disclaimer_on_its_first_lines() -> None:
    """每份首行固定写「本文件不影响排班结果」。"""
    library = load_library(SKILLS_DIR)
    assert library.missing_disclaimer() == ()
    for name in library.names():
        head = library.require(name).body.splitlines()[0]
        assert DISCLAIMER in head, f"{name} 的首行没有免责声明：{head!r}"


def test_rule_interpretation_covers_s01_through_s13() -> None:
    """v6 修订：描述是「14 条规则 + S-01~S-13」，不是 S-01~S-09/S-11。"""
    skill = load_library(SKILLS_DIR).require("rule-interpretation")
    assert "S-01~S-13" in skill.description
    for tag in (
        "S-01",
        "S-02",
        "S-03",
        "S-04",
        "S-05",
        "S-06",
        "S-07",
        "S-10",
        "S-11",
        "S-12",
        "S-13",
    ):
        assert tag in skill.body, f"rule-interpretation 没提到 {tag}"


def test_report_writing_covers_seven_blocks() -> None:
    """v6 修订：Sheet 4 是**七区块**，不是六区块。"""
    skill = load_library(SKILLS_DIR).require("report-writing")
    assert "七个区块" in skill.description or "七区块" in skill.description
    assert "七个区块" in skill.body
    for block in ("区块 7", "跑道与空域占用明细"):
        assert block in skill.body


def test_aircraft_skill_describes_the_variant_without_carrying_the_regex() -> None:
    """⚠️ **修复正则在代码里，不在 skill 里**（S6 防的正是把它挪进来）。"""
    skill = load_library(SKILLS_DIR).require("doc-parsing/aircraft")
    assert "missionC1" in skill.body  # 说明变体存在
    assert "backend/ingestion/repair.py" in skill.body  # 指明修复层在哪
    # 不含任何正则字面量
    for token in ("re.compile", "re.sub", r"\d+", "(?P<", "[A-Z]-"):
        assert token not in skill.body, f"doc-parsing/aircraft 里出现了正则片段：{token}"


def test_all_skills_are_non_authoritative() -> None:
    library = load_library(SKILLS_DIR)
    assert all(not library.require(n).authoritative for n in library.names())


def test_fingerprint_changes_when_content_changes(tmp_path: Path) -> None:
    write_skill(tmp_path, "demo", "name: demo\ndescription: d\nauthoritative: false", "甲")
    before = load_library(tmp_path).fingerprint()
    write_skill(tmp_path, "demo", "name: demo\ndescription: d\nauthoritative: false", "乙")
    assert load_library(tmp_path).fingerprint() != before


# ─────────────────────────────────────────────────────────────────────
# 确定性路由（v6 §7.8.3）
# ─────────────────────────────────────────────────────────────────────
def test_routes_are_verbatim_from_v6() -> None:
    keys = [key for key, _ in SKILL_ROUTES]
    assert ("Ingest", "doc_type=personnel") in keys
    assert ("Diagnosis", "*") in keys
    assert ("Diagnosis", "has_conflict") in keys
    assert ("Explain", "*") in keys


def test_explain_loads_two_skills() -> None:
    assert route_skills("Explain") == ("rule-interpretation", "report-writing")


def test_diagnosis_adds_playbook_only_when_there_is_a_conflict() -> None:
    assert route_skills("Diagnosis", diagnosis_conditions(has_conflict=False)) == (
        "rule-interpretation",
    )
    assert route_skills("Diagnosis", diagnosis_conditions(has_conflict=True)) == (
        "rule-interpretation",
        "relaxation-playbook",
    )


def test_ingest_routes_by_doc_type() -> None:
    assert route_for_component("extract", ingest_conditions("aircraft")) == (
        "doc-parsing/aircraft",
    )
    assert route_for_component("extract", ingest_conditions("personnel")) == (
        "doc-parsing/personnel",
    )


def test_route_and_planner_load_no_skills() -> None:
    """知识层不进这两个组件的上下文 —— 它们离「排什么班」最近。"""
    assert route_for_component("route") == ()
    assert route_for_component("planner") == ()
    assert {"route", "planner"} <= NO_SKILL_COMPONENTS


def test_routing_is_deterministic_and_order_stable() -> None:
    conditions = diagnosis_conditions(has_conflict=True)
    assert all(
        route_skills("Diagnosis", conditions) == route_skills("Diagnosis", conditions)
        for _ in range(5)
    )


def test_route_table_and_shipped_skills_do_not_drift() -> None:
    """路由表点名的 skill 必须都在库里，否则「命中却加载不到」。"""
    library = load_library(SKILLS_DIR)
    assert missing_from_library(library.names()) == ()
    assert set(all_routed_skills()) <= set(EXPECTED_SKILLS)
