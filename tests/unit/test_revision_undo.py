"""多轮修订的 `undo`（v6 §7.3.4 第 2 条硬性设计）。

> **可撤销**：每轮修订入栈，`undo` 弹出最后一条重解。

v6 定义了机制没定义入口。M5 把入口做成「修订轮里的一种表述」，理由见
`planner/revision.py` 的那一段注释：人工门禁的三种决策是 §7.2.4 的规格表，
加第四种要改设计方案。
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

import pytest

from backend.components.planner import planner_node
from backend.planner.revision import RevisionStack, undo_echo, undo_times
from backend.schemas.intent import (
    ConstraintSpec,
    IncrementalConstraint,
    ObjectiveWeights,
)


def constraint(round_no: int, utterance: str) -> IncrementalConstraint:
    return IncrementalConstraint(
        kind="FORBID",
        targets=["P04"],
        params={"day": "周五"},
        origin_utterance=utterance,
        round_no=round_no,
    )


# ─────────────────────────────────────────────────────────────────────
# 表述识别
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("utterance", "times"),
    [
        ("撤销", 1),
        ("撤回刚才那条", 1),
        ("undo", 1),
        ("退回上一版", 1),
        ("撤销两次", 2),
        ("连撤 3 条", 3),
        ("撤销 3 次", 3),
        ("回退两步", 2),
    ],
)
def test_undo_utterances_are_recognised(utterance: str, times: int) -> None:
    assert undo_times(utterance) == times


@pytest.mark.parametrize(
    "utterance",
    ["何超那个换成 AC49", "周三上午挪两个到下午", "刘斌周五别排了", "算了", "不要了", ""],
)
def test_non_undo_utterances_are_not_mistaken_for_undo(utterance: str) -> None:
    """「算了」「不要了」太含糊 —— 含糊的话按翻译不出来处理，不猜。"""
    assert undo_times(utterance) == 0


# ─────────────────────────────────────────────────────────────────────
# 栈语义
# ─────────────────────────────────────────────────────────────────────
def test_version_no_is_stack_depth_plus_one() -> None:
    """首轮方案是 v1，每条修订 +1。这是 UI 上那个 N 的唯一定义点。"""
    stack = RevisionStack()
    assert stack.version_no() == 1
    stack.push(constraint(1, "a"))
    stack.push(constraint(2, "b"))
    assert stack.version_no() == 3


def test_three_revisions_then_two_undos_returns_to_v2() -> None:
    """出口标准那一条：连做 3 轮修订后 undo 两次，方案回到 v2。"""
    stack = RevisionStack()
    for i, said in enumerate(["挪两个到下午", "换成 AC49", "周五别排了"], start=1):
        stack.push(constraint(i, said))
    assert stack.version_no() == 4

    popped = stack.undo_many(2)
    assert [c.round_no for c in popped] == [3, 2], "弹出顺序是从新到旧"
    assert stack.version_no() == 2
    assert stack.utterances() == ["挪两个到下午"]


def test_undo_more_than_the_stack_holds_empties_it_without_raising() -> None:
    """用户说「都撤了吧」而栈里只有两条 —— 撤两条，不报错。"""
    stack = RevisionStack(items=[constraint(1, "a"), constraint(2, "b")])
    assert len(stack.undo_many(5)) == 2
    assert stack.version_no() == 1


def test_undo_on_an_empty_stack_is_not_an_error() -> None:
    assert RevisionStack().undo_many(1) == []


# ─────────────────────────────────────────────────────────────────────
# 回显（第 4 条硬性设计：翻译结果必须回显确认）
# ─────────────────────────────────────────────────────────────────────
def test_echo_lists_every_utterance_that_was_undone() -> None:
    """「撤销了 2 条」看不出撤的是不是他想撤的那两条。"""
    stack = RevisionStack(items=[constraint(1, "挪两个到下午")])
    popped = [constraint(3, "周五别排了"), constraint(2, "换成 AC49")]
    echo = undo_echo(popped, stack)
    assert "周五别排了" in echo and "换成 AC49" in echo
    assert "挪两个到下午" in echo, "保留下来的修订也要列出来"
    assert "v2" in echo


def test_echo_on_empty_stack_says_it_went_back_to_v1() -> None:
    echo = undo_echo([constraint(1, "挪两个到下午")], RevisionStack())
    assert "首轮方案（v1）" in echo


def test_echo_when_there_is_nothing_to_undo() -> None:
    assert "没有可撤销" in undo_echo([], RevisionStack())


# ─────────────────────────────────────────────────────────────────────
# 接进 planner 节点
# ─────────────────────────────────────────────────────────────────────
def state_with_stack(utterance: str, depth: int) -> dict[str, Any]:
    items = [constraint(i, f"第{i}轮") for i in range(1, depth + 1)]
    return {
        "messages": [{"role": "user", "content": utterance}],
        "revision_round": depth + 1,
        "revision_stack": items,
        "constraint_spec": ConstraintSpec(
            snapshot_id="snap_x",
            ruleset_version="1.3.0",
            semantics_version="1.0.0",
            iso_week="2026W02",
            week_start=date(2026, 1, 5),
            week_end=date(2026, 1, 11),
            scope_persons="ALL",
            scope_missions="ALL",
            relaxation_tier=0,
            objective_weights=ObjectiveWeights(progress=1.0, disruption=1.0, balance=1.0),
            runway_model="dual_runway",
            incremental_constraints=items,
        ),
        "human_decision": {
            "decision": "REVISE",
            "user_id": "u1",
            "role": "scheduler",
            "comment": utterance,
        },
    }


def test_undo_round_pops_the_stack_and_the_solver_constraints_together() -> None:
    """只弹栈不改 `constraint_spec` = 静默失效：用户看到「已撤销」却拿到没变的方案。"""
    command = planner_node(cast(Any, state_with_stack("撤销两次", 3)))
    update = cast(dict[str, Any], command.update)
    # 先回门禁把回显摆出来（§7.3.4 第 4 条），APPROVE 之后才由 human_gate 去 solve
    assert command.goto == "human_gate"
    assert update["pending_revision"] is True
    assert len(update["revision_stack"]) == 1
    assert [c.round_no for c in update["constraint_spec"].incremental_constraints] == [1]
    assert "撤销最近 2 条修订" in update["revision_echo"]


def test_the_echo_lives_in_its_own_field_not_in_explanation() -> None:
    """`explanation` 归 `explain` 节点所有，每轮末尾会被方案解释覆盖。

    回显必须活到用户点确认的那一刻 —— 实测踩过：撤销的回显被方案解释盖掉，
    用户看到的是「共 14 个架次……」而不是「撤销最近 2 条修订」。
    """
    update = cast(dict[str, Any], planner_node(cast(Any, state_with_stack("撤销", 2))).update)
    assert "revision_echo" in update
    assert "explanation" not in update, "回显不许写进 explanation"


def test_undo_emits_a_traceable_event() -> None:
    command = planner_node(cast(Any, state_with_stack("撤销", 2)))
    update = cast(dict[str, Any], command.update)
    event = update["trace_events"][0]
    assert event.payload["action"] == "undo"
    assert event.payload["undone"] == 1
    assert event.payload["dropped_rounds"] == [2]
    assert event.payload["version_no"] == 2


def test_undo_with_nothing_to_undo_goes_to_the_human_gate_not_a_resolve() -> None:
    """没得撤销不是错误，但也没必要重解 —— 方案一个字都不会变。"""
    command = planner_node(cast(Any, state_with_stack("撤销", 0)))
    update = cast(dict[str, Any], command.update)
    assert command.goto == "human_gate"
    assert update["needs_human"] is True
    assert update["pending_revision"] is False, "没东西可确认，按 APPROVE 也不该触发求解"


# ─────────────────────────────────────────────────────────────────────
# 回显确认（v6 §7.3.4 第 4 条：用户确认后才重解）
# ─────────────────────────────────────────────────────────────────────
def test_a_translated_revision_waits_for_confirmation_before_solving() -> None:
    """翻译完**不直接去 solve** —— 先把「我理解为：…」摆出来。"""
    state = state_with_stack("刘斌周五别排了", 0)
    state["revision_round"] = 1
    command = planner_node(cast(Any, state))
    update = cast(dict[str, Any], command.update)
    assert command.goto == "human_gate", "先解再问 = 翻译错了也已经排了一版"
    assert update["pending_revision"] is True
    assert "我理解为" in update["revision_echo"]
    assert len(update["revision_stack"]) == 1


def test_rejecting_the_echo_pops_the_revision_without_any_solve() -> None:
    """用户说「你没听懂」→ 弹栈撤回，一次求解都不该发生。"""
    state = state_with_stack("刘斌周五别排了", 2)
    state["revision_cancelled"] = True
    command = planner_node(cast(Any, state))
    update = cast(dict[str, Any], command.update)
    assert command.goto == "human_gate"
    assert update["pending_revision"] is False
    assert update["revision_cancelled"] is False
    assert len(update["revision_stack"]) == 1, "弹掉最后一条"
    assert [c.round_no for c in update["constraint_spec"].incremental_constraints] == [1]
    assert "已撤回" in update["revision_echo"]
