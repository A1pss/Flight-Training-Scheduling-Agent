"""术语对齐表（v6 §6.5.2 第三步 / `rules/terminology.yaml`）。

三条口径逐条钉住：只映射不判断、快照里没有就静默失效、一个别名多个 target
就是歧义。
"""

from __future__ import annotations

import pytest

from backend.core.errors import RuleParseError
from backend.retrieval.terms import (
    TERMINOLOGY_PATH,
    TermEntry,
    Terminology,
    get_terminology,
    load_terminology,
    normalize,
    parse_terminology,
)


@pytest.fixture
def terms() -> Terminology:
    return load_terminology()


def test_ships_with_the_repo_and_parses() -> None:
    assert TERMINOLOGY_PATH.is_file(), "rules/terminology.yaml 必须随仓库交付"
    table = load_terminology()
    assert table.version
    assert len(table.entries) > 20


def test_business_owner_removed_the_main_runway_alias() -> None:
    """业务方 2026-08-14 明确剔除「主跑道」——原始资料里没有主副之分。"""
    aliases = {normalize(e.alias) for e in load_terminology().entries}
    assert "主跑道" not in aliases
    assert "副跑道" not in aliases
    assert normalize("2号跑道") in aliases


@pytest.mark.parametrize(
    ("text", "kind", "target"),
    [
        ("刘斌的仪表课目到期没", "mission_class", "C"),
        ("起落航线要飞几次", "mission_class", "A"),
        ("编队飞行安排一下", "mission_class", "F"),
        ("低空突防谁能飞", "mission_class", "H"),
        ("这几个都走 2 号跑道", "runway", "RWY-2"),
        ("一号跑道今天关闭", "runway", "RWY-1"),
        ("Small Area A 满了", "airspace", "SAA"),
    ],
)
def test_colloquial_terms_align_to_system_terms(
    terms: Terminology, text: str, kind: str, target: str
) -> None:
    matches, ambiguities = terms.align(text)
    assert not ambiguities
    assert (kind, target) in {(m.kind, m.target) for m in matches}


def test_longest_alias_wins_so_one_span_yields_one_match(terms: Terminology) -> None:
    """「本场起落航线」不该同时命中「起落航线」「本场」「起落」三条。"""
    matches, _ = terms.align("本场起落航线")
    assert len(matches) == 1
    assert matches[0].target == "A"


def test_target_absent_from_snapshot_silently_deactivates(terms: Terminology) -> None:
    """口径 ②：换一批数据时不报错，也不把基准编号偷带进新快照。"""
    matches, ambiguities = terms.align("这几个都走 2 号跑道", known_runways=["RWY-1"])
    assert matches == ()
    assert ambiguities == ()


def test_one_alias_two_targets_is_an_ambiguity_not_a_guess() -> None:
    """口径 ③：与「何超 / 高超」同一条规矩 —— 不自行选一个。"""
    table = Terminology(
        version="test",
        entries=(
            TermEntry(alias="跟飞", kind="mission_class", target="B"),
            TermEntry(alias="跟飞", kind="mission_class", target="F"),
        ),
    )
    matches, ambiguities = table.align("今天的跟飞怎么排")
    assert matches == ()
    assert len(ambiguities) == 1
    assert "B" in ambiguities[0] and "F" in ambiguities[0]


def test_mission_class_renders_as_the_term_used_in_rule_texts(terms: Terminology) -> None:
    matches, _ = terms.align("仪表课目")
    assert matches[0].as_term() == "C类"


def test_runway_renders_as_the_id(terms: Terminology) -> None:
    matches, _ = terms.align("2号跑道")
    assert matches[0].as_term() == "RWY-2"


def test_normalize_folds_spaces_and_case_but_nothing_else() -> None:
    assert normalize("Small Area A") == "smallareaa"
    assert normalize("小区域 A") == "小区域a"
    # 「何超」与「高超」绝不能被归一到一起
    assert normalize("何超") != normalize("高超")


@pytest.mark.parametrize(
    "raw",
    [
        {"missions": []},  # 缺 version
        {"version": "1.0", "missions": "不是列表"},
        {"version": "1.0", "missions": [{"aliases": ["x"]}]},  # 缺 mission_class
        {"version": "1.0", "missions": [{"mission_class": "A", "aliases": []}]},  # 空别名
        {"version": "1.0"},  # 一条映射都没有
    ],
)
def test_malformed_table_raises_instead_of_partial_load(raw: dict[str, object]) -> None:
    """半张术语表比没有更糟：一半词能翻译、一半静默失败，两者表现一样。"""
    with pytest.raises(RuleParseError):
        parse_terminology(raw)


def test_get_terminology_is_cached() -> None:
    get_terminology.cache_clear()
    assert get_terminology() is get_terminology()
    get_terminology.cache_clear()
