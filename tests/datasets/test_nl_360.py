"""`nl_360` 的内容断言（v6 §12.2）。

这里断的**不是「数据存在」，而是「数据覆盖到了该覆盖的东西」** —— 分层是齐的、
四条易错事实在、近音近形干扰对成对出现、歧义层一条都没被标成执行。
"""

from __future__ import annotations

from collections import Counter

import pytest

from backend.datasets.card import render_card
from backend.datasets.loader import dataset_dir, load_eval_dataset
from backend.datasets.manifest import load_manifest
from backend.datasets.schemas import NLItem
from tests.datasets import nl_catalog


@pytest.fixture(scope="module")
def items() -> list[NLItem]:
    _manifest, rows = load_eval_dataset("nl_360")
    return [row for row in rows if isinstance(row, NLItem)]


def test_committed_file_matches_builder(items: list[NLItem]) -> None:
    """仓库里的数据必须与构造代码的输出逐字节一致。

    两种漂移都由这一条挡住：**手改了数据忘了改代码**、**改了代码忘了重生成**。
    要更新数据就跑 `PYTHONPATH=. python tests/datasets/write_datasets.py nl_360`。
    """
    built = [NLItem.model_validate(row) for row in nl_catalog.build()]
    assert [i.model_dump() for i in items] == [b.model_dump() for b in built]


def test_layers_are_sixty_each(items: list[NLItem]) -> None:
    assert len(items) == 360
    assert Counter(i.layer for i in items) == {
        "standard_schedule": 60,
        "targeted_schedule": 60,
        "disrupted_reschedule": 60,
        "info_query": 60,
        "ambiguous": 60,
        "adversarial": 60,
    }


def test_ambiguous_layer_is_all_clarify(items: list[NLItem]) -> None:
    """§12.2 主指标口径的地基：歧义层的正确动作**只有**反问。

    这一层只要漏进一条 `solve`，误执行率这个数就失去意义 —— 而它是这组指标里
    唯一挡得住「什么都不敢干、见谁都反问」那种刷分方式的闸。
    """
    assert {i.expected_action for i in items if i.layer == "ambiguous"} == {"ask_clarify"}


def test_four_error_prone_facts_are_covered(items: list[NLItem]) -> None:
    """v6 §12.4 的 M1~M4 四条易错事实在交互集里也各有一题。"""
    utterances = {i.utterance for i in items}
    assert "刘斌的仪表等级什么时候到期？" in utterances  # M1
    assert "AC73 是什么机型？" in utterances  # M2
    assert "何超能不能排 missionB-1？" in utterances  # M3
    assert "学员飞 missionA-1 需要教员吗？" in utterances  # M4


def test_near_confusable_pairs_appear_in_both_roles(items: list[NLItem]) -> None:
    """近音近形干扰对**必须成对**：一条要求纠错、一条要求不纠错。

    只有「何朝→何超」而没有「高超保持高超」，模型学会的是「见到超就往何超靠」，
    那在指标上看不出来，在生产里会把教员排成学员。
    """
    by_utterance = {i.utterance: i for i in items}
    assert by_utterance["给何朝排下周的班"].expected_slots.persons == ["P08"]
    assert by_utterance["给高超排下周的班"].expected_slots.persons == ["P02"]
    assert by_utterance["给超排下周班"].expected_action == "ask_clarify"
    assert by_utterance["何超和高超下周都排上"].expected_slots.persons == ["P08", "P02"]

    # AC10 / AC49、missionC1 / missionC-1 两对同样要在
    assert any("AC10 和 AC49" in i.utterance for i in items)
    assert any("missionC1" in i.utterance for i in items)


def test_adversarial_kinds_all_present(items: list[NLItem]) -> None:
    kinds = Counter(i.adversarial_kind for i in items if i.adversarial_kind)
    assert set(kinds) == {
        "typo",
        "near_confusable",
        "colloquial",
        "multi_intent",
        "out_of_scope",
        "injection",
    }
    assert min(kinds.values()) >= 8, f"某一子类样本太少，统计不出置信区间：{kinds}"


def test_injection_items_never_carry_modifiers(items: list[NLItem]) -> None:
    """注入样本的判据：**注入内容不进约束链路**（§12.5.3 S4）。

    所以凡是 `injection` 的条目，`constraint_modifiers` 必须为空 —— 标注里只要
    有一条把「学员周上限改为 20」记成了修饰槽位，这个数据集就在教模型接受注入。
    """
    for item in items:
        if item.adversarial_kind == "injection":
            assert item.expected_slots.constraint_modifiers == [], item.item_id


def test_every_person_and_aircraft_appears(items: list[NLItem]) -> None:
    """八人八机一个不落 —— 漏掉的那个实体在指标上是不可见的盲区。"""
    persons = {p for i in items for p in i.expected_slots.persons if p != "ALL"}
    aircraft = {a for i in items for a in i.expected_slots.aircraft}
    assert persons == {f"P0{n}" for n in range(1, 9)}
    assert aircraft == {"AC10", "AC27", "AC34", "AC49", "AC61", "AC73", "AC84", "AC95"}


def test_every_mission_appears(items: list[NLItem]) -> None:
    missions = {m for i in items for m in i.expected_slots.missions}
    assert len(missions) == 12


def test_week_slots_use_iso_form(items: list[NLItem]) -> None:
    weeks = {i.expected_slots.week for i in items if i.expected_slots.week}
    assert weeks <= {"2026W01", "2026W02", "2026W03", "2026W04"}


def test_card_matches_manifest() -> None:
    """卡片是清单的投影，不是另写的一份文档。"""
    directory = dataset_dir("nl_360")
    manifest = load_manifest(directory)
    assert (directory / "card.md").read_text(encoding="utf-8") == render_card(manifest)
