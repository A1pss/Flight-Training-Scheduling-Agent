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
        "missing_required_info": False,
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


def test_missing_required_info_becomes_ask_clarify_even_for_rule_hit() -> None:
    """规则命中只确定意图，缺周次等必需输入仍必须追问。"""
    assert (
        action_at_threshold(obs(missing_required_info=True, source="rule"), 0.75) == "ask_clarify"
    )


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


# ── 槽位形态清洗（M9-B 后续窗口）────────────────────────────────────
def test_tool_call_expressions_are_not_slot_values() -> None:
    """模型把工具调用当槽位值交出来时必须丢掉，**哪怕扫描器没有候选可用**。

    `merge_slots` 的兼并只在扫描器抓到东西时生效；原话里压根没有周次表述时
    （「何超现在能排 missionC-1 吗？」），垃圾值会一路走到消解层 →
    `not_found` → 歧义 → 人工门禁，**knowledge 节点根本没机会运行**。
    实测 30 条 query 轨迹里 7 条栽在这。
    """
    from backend.routing.classify import _looks_like_tool_call

    assert _looks_like_tool_call("resolve_week(2026W03)")
    assert _looks_like_tool_call("resolve_week('当前')('ISO')()()")
    assert _looks_like_tool_call("resolve_person('何超')")


def test_entity_labels_with_parentheses_are_kept() -> None:
    """判据必须窄：实体标签本身就带括号，按「含括号」一刀切会删掉真槽位。"""
    from backend.routing.classify import _looks_like_tool_call

    assert not _looks_like_tool_call("JL-9(AC84)")
    assert not _looks_like_tool_call("高超(P02)")
    assert not _looks_like_tool_call("何超")
    assert not _looks_like_tool_call("2026-W03")


def test_merge_drops_junk_even_when_scanner_found_nothing() -> None:
    from backend.routing.classify import _Slots, merge_slots

    merged = merge_slots(
        _Slots(),
        _Slots(persons=["resolve_person('何超')"], week="resolve_week('当前')()"),
    )
    assert merged.persons == []
    assert merged.week == ""


def test_merge_keeps_model_value_when_it_is_a_real_surface() -> None:
    """扫描器覆盖不到「下周」这类口语表述，模型那一路不能被误伤。"""
    from backend.routing.classify import _Slots, merge_slots

    merged = merge_slots(_Slots(), _Slots(week="下周", persons=["何超"]))
    assert merged.week == "下周"
    assert merged.persons == ["何超"]


def test_merge_drops_model_entity_not_present_in_original_text() -> None:
    """模型把 IFR 幻觉成 missionI-1 时，不得制造一个人工歧义门禁。"""
    from backend.routing.classify import _Slots, merge_slots

    merged = merge_slots(
        _Slots(persons=["吴鹏", "高超"], week="本周"),
        _Slots(persons=["吴鹏", "高超"], missions=["missionI-1"], week="本周"),
        raw_text="本周吴鹏和高超都请假，IFR 也关了",
    )
    assert merged.persons == ["吴鹏", "高超"]
    assert merged.missions == []
    assert merged.week == "本周"


# ── Z-44 置信信号重建 ────────────────────────────────────────────────
def test_query_intent_with_no_slots_does_not_ask() -> None:
    """★ 判据只对**排班类**生效。

    查询类问题本来就常常不含任何槽位 —— 「IFR Route 的容量是多少？」
    没有人名/机号/周次，却完全不需要反问。第一版对所有意图生效，实测把
    查询类打残了：实验五零工具轨迹 5 → 10 条、缺失调用率 16.67% → 33.33%、
    工具选择 65.71% → 42.86%。**排班必须知道「给谁、哪一周」，查询不必。**
    """
    from backend.routing.classify import IntentResult

    q = IntentResult(
        intent="query",
        confidence=1.0,
        source="rule",
        next_node="knowledge",
        calibration_features={"no_slots_at_all": True, "week_missing": True},
    )
    assert not q.missing_required_info


def test_rule_hit_with_no_slots_must_still_ask() -> None:
    """★ `Z-44` 的回归闸：规则命中只确定了**意图**，不确定**槽位**。

    「给他排班」命中排班规则、`confidence=1.0`，但指代不明、一个槽位都没解出来。
    M9-B 实测：期望反问的 62 条里 17 条走规则路径，其中 **16 条被直接执行**，
    占全量 1080 的 4.4% —— **单这一项就超过原 ≤4% 的目标**，且阈值取任何值
    都碰不到它们（`below_threshold` 原本对规则路径不生效）。
    """
    from backend.routing.classify import IntentResult

    vague = IntentResult(
        intent="schedule",
        confidence=1.0,
        source="rule",
        next_node="planner",
        calibration_features={"no_slots_at_all": True, "week_missing": True},
    )
    assert vague.missing_required_info, "规则命中但周次没解出来 → 必须问"
    assert not vague.below_threshold(0.75), "但**不是**因为置信度低 —— 两个判据刻意分开"


def test_rule_hit_with_slots_is_not_blocked() -> None:
    """「给何超排班」解出了人 —— 不能被误伤。粒度是**有没有解出槽位**，
    不是「走没走规则路径」。"""
    from backend.routing.classify import IntentResult

    clear = IntentResult(
        intent="schedule",
        confidence=1.0,
        source="rule",
        next_node="planner",
        calibration_features={"no_slots_at_all": False, "week_missing": False},
    )
    assert not clear.missing_required_info
    assert not clear.below_threshold(0.75)


def test_rule_path_confidence_stays_deterministic() -> None:
    """新判据是**确定性**的，不引入概率 —— 规则路径的置信度仍是 1.0，
    阈值再高也不该因为「不自信」而挡它（挡它的理由是缺信息）。"""
    from backend.routing.classify import IntentResult

    clear = IntentResult(
        intent="schedule",
        confidence=1.0,
        source="rule",
        next_node="planner",
        calibration_features={"no_slots_at_all": False, "week_missing": False},
    )
    assert not clear.below_threshold(1.0)
    assert not clear.missing_required_info


def test_llm_path_threshold_still_applies() -> None:
    """v6 §7.5 原式那半句必须保留。"""
    from backend.routing.classify import IntentResult

    unsure = IntentResult(intent="schedule", confidence=0.35, source="llm", next_node="planner")
    assert unsure.below_threshold(0.75)
    assert not unsure.below_threshold(0.30)


def test_slot_quality_features_have_real_spread() -> None:
    """`Z-44` 的动机：原五项特征没有区分度（1075/1080 恒为 1.0）。
    新特征必须真的会变，否则等于换了个名字的常数。"""
    from backend.planner.calibration import CalibrationFeatures, heuristic_confidence

    clear = CalibrationFeatures()
    vague = CalibrationFeatures(no_slots_at_all=True, week_missing=True)
    assert heuristic_confidence(clear) == 1.0
    assert heuristic_confidence(vague) < 0.5, "说不清楚的请求要显著低于说得清的"
