"""`memory_320` 的内容断言（v6 §12.4）。

覆盖度不是靠眼看：四条易错事实在不在、分层比例对不对、负例够不够、
20 周时间线的六个观测点是不是各有 5 条 —— 全部做成断言。
"""

from __future__ import annotations

from collections import Counter

import pytest

from backend.datasets.loader import load_eval_dataset
from backend.datasets.schemas import MemoryItem
from tests.datasets import memory_probes
from tests.datasets.memory_catalog import REVISION_PHRASES, phrasing_doc_id
from tests.datasets.memory_probes import DECAY_WEEKS


@pytest.fixture(scope="module")
def items() -> list[MemoryItem]:
    _manifest, rows = load_eval_dataset("memory_320")
    return [row for row in rows if isinstance(row, MemoryItem)]


def test_committed_file_matches_builder(items: list[MemoryItem]) -> None:
    built = [MemoryItem.model_validate(row) for row in memory_probes.build_full()]
    assert [i.model_dump() for i in items] == [b.model_dump() for b in built]


def test_type_split(items: list[MemoryItem]) -> None:
    assert len(items) == 320
    assert Counter(i.memory_type for i in items) == {
        "semantic": 120,
        "episodic": 120,
        "procedural": 80,
    }


def test_probe_kind_split(items: list[MemoryItem]) -> None:
    kinds = Counter(i.probe_kind for i in items)
    assert kinds["fact"] == 60
    assert kinds["prereq"] == 20
    assert kinds["rule_text"] == 20
    assert kinds["aggregate"] == 10
    assert kinds["episode_recall"] == 80
    assert kinds["decay"] == 30
    assert kinds["preference"] == 60
    assert kinds["temporal_validity"] == 17
    assert kinds["absent"] == 23


def test_four_error_prone_facts(items: list[MemoryItem]) -> None:
    """★ v6 §12.4 点名的 M1~M4，一条不许少，答案逐字对齐 v6 表格。"""
    by_query = {i.query: i for i in items}
    assert by_query["刘斌的仪表等级什么时候到期？"].expected_answer == "2026-01-07"
    assert by_query["AC73 是什么机型？"].expected_answer == "JL-8"
    assert by_query["何超能不能排 missionB-1？"].expected_answer == "不能，缺 missionA-2"
    assert by_query["学员飞 missionA-1 需要教员吗？"].expected_answer == "不需要"


def test_decay_covers_six_observation_points(items: list[MemoryItem]) -> None:
    """六个观测点各 5 条 —— 少一个点，那一段的衰减曲线就是猜的。"""
    weeks = Counter(i.timeline_week for i in items if i.probe_kind == "decay")
    assert set(weeks) == set(DECAY_WEEKS)
    assert set(weeks.values()) == {5}


def test_absent_probes_have_empty_gold(items: list[MemoryItem]) -> None:
    """负例的 gold 必须为空，且三类记忆都要有负例。"""
    absent = [i for i in items if i.probe_kind == "absent"]
    assert all(i.expected_doc_ids == [] for i in absent)
    assert Counter(i.memory_type for i in absent) == {
        "semantic": 10,
        "episodic": 5,
        "procedural": 8,
    }


def test_non_absent_probes_have_gold(items: list[MemoryItem]) -> None:
    for item in items:
        if item.probe_kind != "absent":
            assert item.expected_doc_ids, item.item_id


def test_prereq_has_both_polarities(items: list[MemoryItem]) -> None:
    """★ 先修判定的正负例都要有：只有负例时，一个「一律答不能」的系统也能满分。"""
    verdicts = [str(i.expected_answer) for i in items if i.probe_kind == "prereq"]
    # 「不能，缺 missionA-2」这类答案带了理由，所以按前缀判而不是全等
    negative = sum(1 for v in verdicts if v.startswith("不能"))
    positive = len(verdicts) - negative
    assert negative >= 8, verdicts
    assert positive >= 8, verdicts


def test_every_phrasing_key_is_probed(items: list[MemoryItem]) -> None:
    """24 个 phrasing key 一个不落，且每个至少被两条题指向。"""
    hits = Counter(
        doc for i in items for doc in i.expected_doc_ids if doc.startswith("proc:phrasing/")
    )
    expected = {phrasing_doc_id(p) for p in REVISION_PHRASES}
    assert set(hits) == expected
    assert min(hits.values()) >= 2


def test_every_entity_is_probed(items: list[MemoryItem]) -> None:
    """八人八机 12 课目 6 空域在 gold 里全部出现过。"""
    docs = {doc for i in items for doc in i.expected_doc_ids}
    assert len({d for d in docs if d.startswith("ent:person:")}) == 8
    assert len({d for d in docs if d.startswith("ent:aircraft:")}) == 8
    assert len({d for d in docs if d.startswith("ent:mission:")}) == 12
    assert len({d for d in docs if d.startswith("ent:airspace:")}) == 6


def test_rule_probes_cover_all_fourteen(items: list[MemoryItem]) -> None:
    """14 条规则每条至少一题 —— 规则集是 14 条，不是 13 条也不是 15 条。"""
    rules = {doc for i in items for doc in i.expected_doc_ids if doc.startswith("rule:")}
    assert len(rules) == 14


def test_temporal_probes_disagree_across_time(items: list[MemoryItem]) -> None:
    """★ 时效探针的意义在于**同一 gold 在不同时点有不同答案**。

    若所有时效探针的期望答案都一样，那它测的就只是「能不能召回」，
    时效维度等于没测。
    """
    same_gold = [
        i
        for i in items
        if i.probe_kind == "temporal_validity"
        and i.expected_doc_ids == ["proc:relaxation/preferred_tier"]
    ]
    assert {i.expected_answer for i in same_gold} > {"Tier 0"}
