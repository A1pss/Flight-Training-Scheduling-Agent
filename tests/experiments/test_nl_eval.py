"""`backend/experiments/nl_eval.py` 的单测。

**两条回归闸**写在这里，它们各自钉住本窗口实测撞出来的一个坑：

1. `test_scope_all_and_empty_persons_are_the_same_thing` —— 槽位表示口径。
   不还原会把 152 条排班样本的 persons 槽全判成漏抽，F1 从 0.925 掉到 0.564。
2. `test_below_threshold_never_applies_to_rule_hits` —— 阈值管辖范围。
   规则命中的 confidence=1.0 是确定性事实，被阈值挡下就等于把一级路径废掉。
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.experiments.nl_eval import (
    EXECUTING_ACTIONS,
    SLOT_KINDS,
    action_at_threshold,
    slot_counts,
)


def obs(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "observed_intent": "schedule",
        "has_ambiguity": False,
        "source": "rule",
        "confidence": 1.0,
        "planner_asked": False,
        "expected_slots": {},
        "observed_slots": {},
    }
    base.update(kw)
    return base


# ── 动作判定 ──────────────────────────────────────────────────────────
def test_unknown_intent_becomes_refuse_not_ask_clarify() -> None:
    """`unknown` → refuse。

    nl_360 里 13 条 `unknown` 的期望动作全是 `refuse`（超纲请求 + 注入尝试），
    与「歧义 → 反问」是两件事：前者是不该做，后者是没问清。
    """
    assert action_at_threshold(obs(observed_intent="unknown"), 0.75) == "refuse"


def test_ambiguity_becomes_ask_clarify() -> None:
    assert action_at_threshold(obs(has_ambiguity=True), 0.75) == "ask_clarify"


def test_below_threshold_never_applies_to_rule_hits() -> None:
    """规则命中的 confidence=1.0 不归阈值管辖（`IntentResult.below_threshold` 的口径）。

    哪怕阈值拉到 1.0，规则路径也必须照常执行 —— 否则一级路径等于被废掉。
    """
    assert action_at_threshold(obs(source="rule", confidence=1.0), 1.0) == "solve"


def test_llm_path_below_threshold_asks() -> None:
    assert action_at_threshold(obs(source="llm", confidence=0.5), 0.75) == "ask_clarify"
    assert action_at_threshold(obs(source="llm", confidence=0.5), 0.0) == "solve"


def test_planner_question_turns_schedule_into_ask_clarify() -> None:
    assert action_at_threshold(obs(planner_asked=True), 0.75) == "ask_clarify"


@pytest.mark.parametrize(
    ("intent", "expected"),
    [("query", "answer"), ("ingest", "route_ingest"), ("export", "route_export")],
)
def test_non_scheduling_intents_map_to_their_handoff(intent: str, expected: str) -> None:
    assert action_at_threshold(obs(observed_intent=intent), 0.75) == expected


def test_ask_clarify_is_not_counted_as_executing() -> None:
    """误执行率的分子只看「真的动手了」的动作。"""
    assert "ask_clarify" not in EXECUTING_ACTIONS
    assert "refuse" not in EXECUTING_ACTIONS
    assert {"solve", "reschedule", "answer"} <= EXECUTING_ACTIONS


# ── 槽位比对 ──────────────────────────────────────────────────────────
def test_scope_all_and_empty_persons_are_the_same_thing() -> None:
    """标注写 `["ALL"]`、系统给 `[]`，两者说的是同一件事。

    `deterministic_intent` 正是这么做的（没点名 → `scope_persons="ALL"`）。
    不还原的话 152 条排班样本的 persons 槽会**全判成漏抽**。
    """
    row = obs(
        observed_intent="schedule",
        expected_slots={"persons": ["ALL"]},
        observed_slots={"persons": []},
    )
    counts = slot_counts(row, "persons")
    assert (counts.tp, counts.fp, counts.fn) == (1, 0, 0)


def test_query_intent_empty_persons_is_not_all() -> None:
    """查询类的空人员列表就是「没提到人」，不是「所有人」—— 还原只对排班类生效。"""
    row = obs(
        observed_intent="query",
        expected_slots={"persons": ["ALL"]},
        observed_slots={"persons": []},
    )
    counts = slot_counts(row, "persons")
    assert counts.tp == 0 and counts.fn == 1


def test_named_person_still_has_to_match() -> None:
    """点名了就得抽对 —— 还原不能变成「空列表万能通过」。"""
    row = obs(
        observed_intent="schedule",
        expected_slots={"persons": ["P08"]},
        observed_slots={"persons": []},
    )
    counts = slot_counts(row, "persons")
    assert counts.tp == 0 and counts.fn == 1


def test_constraint_modifiers_compare_by_kind_only() -> None:
    """修饰只比 `kind`：标注侧的 `surface` 是原话切片，逐字比等于测字符串相等。"""
    row = obs(
        expected_slots={
            "constraint_modifiers": [{"kind": "FORBID", "surface": "周三不要安排飞行"}]
        },
        observed_slots={"constraint_modifiers": ["FORBID"]},
    )
    counts = slot_counts(row, "constraint_modifiers")
    assert (counts.tp, counts.fp, counts.fn) == (1, 0, 0)


def test_extra_predicted_slot_is_a_false_positive() -> None:
    row = obs(
        expected_slots={"aircraft": ["AC84"]},
        observed_slots={"aircraft": ["AC84", "AC95"]},
    )
    counts = slot_counts(row, "aircraft")
    assert (counts.tp, counts.fp, counts.fn) == (1, 1, 0)


def test_all_five_slot_kinds_are_covered() -> None:
    """§12.2 的五类槽位一个都不能少。"""
    assert set(SLOT_KINDS) == {
        "persons",
        "aircraft",
        "missions",
        "week",
        "constraint_modifiers",
    }
