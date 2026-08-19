"""`sft_seed` 的内容断言（v6 §15.2）。"""

from __future__ import annotations

from collections import Counter

import pytest

from backend.core.ruleset import get_ruleset
from backend.datasets.entities import AIRCRAFT, AIRSPACES, MISSIONS, PERSONS, RUNWAYS
from backend.datasets.loader import load_eval_dataset
from backend.datasets.schemas import SftSeedItem
from tests.datasets import seed_catalog


@pytest.fixture(scope="module")
def items() -> list[SftSeedItem]:
    _manifest, rows = load_eval_dataset("sft_seed")
    return [row for row in rows if isinstance(row, SftSeedItem)]


def test_committed_file_matches_builder(items: list[SftSeedItem]) -> None:
    built = [SftSeedItem.model_validate(row) for row in seed_catalog.build()]
    assert [i.model_dump() for i in items] == [b.model_dump() for b in built]


def test_seed_composition(items: list[SftSeedItem]) -> None:
    """§15.2 那一格点名的三样：60 条需求 + 14 规则 + 13 语义假设 + 实体表。"""
    assert Counter(i.kind for i in items) == {
        "request": 60,
        "rule": 14,
        "semantic": 13,
        "entity": 36,
    }


def test_rules_come_from_the_yaml_not_from_hand(items: list[SftSeedItem]) -> None:
    """★ 14 条规则必须与 ruleset 逐条对上。

    手抄的规则会在下一次改规则时**悄悄分叉** —— 而合成数据是拿它们当事实用的：
    一条抄错的约束会被扩写成几十条训练样本，模型学到的就是那条错的。
    """
    ruleset = get_ruleset()
    seeds = {i.payload["rule_id"]: i for i in items if i.kind == "rule"}
    assert set(seeds) == set(ruleset.rules)
    for rule_id, spec in ruleset.rules.items():
        assert spec.statement in seeds[rule_id].text
        assert seeds[rule_id].payload["tier"] == spec.tier


def test_all_thirteen_semantic_switches(items: list[SftSeedItem]) -> None:
    """S-01~S-13 一条不少，取值与 yaml 一致。"""
    switches = {i.payload["switch_id"] for i in items if i.kind == "semantic"}
    assert switches == {f"S-{n:02d}" for n in range(1, 14)}


def test_entity_table_is_complete(items: list[SftSeedItem]) -> None:
    """8 人 / 12 课目 / 8 机 / 6 空域 / 2 跑道。"""
    kinds = Counter(i.payload["entity_type"] for i in items if i.kind == "entity")
    assert kinds == {
        "person": len(PERSONS),
        "mission": len(MISSIONS),
        "aircraft": len(AIRCRAFT),
        "airspace": len(AIRSPACES),
        "runway": len(RUNWAYS),
    }


def test_requests_span_all_three_scheduling_layers(items: list[SftSeedItem]) -> None:
    layers = Counter(i.payload["layer"] for i in items if i.kind == "request")
    assert layers == {
        "standard_schedule": 20,
        "targeted_schedule": 20,
        "disrupted_reschedule": 20,
    }


def test_requests_trace_back_to_nl_360(items: list[SftSeedItem]) -> None:
    """每条需求都指得回 nl_360 的条目号 —— 种子的出处必须可查。"""
    for item in items:
        if item.kind == "request":
            assert item.source_ref.startswith("NL-"), item.item_id
