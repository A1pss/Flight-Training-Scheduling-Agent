"""图组装（v6 §7.5）。

不连库的那一半：节点集、边、三处动态跳转的去向、`"END"` 的翻译、以及
`interrupt()` + Checkpointer 的中断/恢复语义（用内存 checkpointer）。

连库的完整链路在 `tests/integration/test_graph_live.py`。
"""

from __future__ import annotations

from datetime import date
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END
from langgraph.types import Command

from backend.graph.graph import NODE_NAMES, GraphDeps, build_graph
from backend.graph.state import initial_state
from backend.graph.store import MEMORY_KINDS, build_store, namespace, recall, remember
from backend.nodes import DETERMINISTIC_NODE_NAMES
from tests.fixtures.graph_fixtures import directory

TODAY = date(2026, 1, 7)


def exploding_session() -> Any:
    raise AssertionError("本用例不该碰数据库")


def deps(**kwargs: Any) -> GraphDeps:
    base: dict[str, Any] = {
        "session_factory": exploding_session,
        "directory": directory(),
        "today": TODAY,
        "library": None,
        "prompt_versions": {"route/system": "v1"},
    }
    base.update(kwargs)
    return GraphDeps(**base)


# ─────────────────────────────────────────────────────────────────────
# 结构
# ─────────────────────────────────────────────────────────────────────
def test_graph_has_exactly_the_v6_node_set() -> None:
    """4 个 LLM 组件 + 6 个确定性节点 + 1 个 Agent。`knowledge` 由 W8 承接。"""
    app = build_graph(deps())
    nodes = set(app.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == set(NODE_NAMES)
    assert set(DETERMINISTIC_NODE_NAMES) <= nodes
    assert "knowledge" not in nodes


def test_start_goes_to_route() -> None:
    app = build_graph(deps())
    edges = {(e.source, e.target) for e in app.get_graph().edges}
    assert ("__start__", "route") in edges


def test_declared_destinations_cover_the_three_dynamic_jumps() -> None:
    """三处动态跳转：route 的意图分流、planner 的追问回退、validate 的驳回回环。"""
    app = build_graph(deps())
    edges = {(e.source, e.target) for e in app.get_graph().edges}
    assert ("route", "planner") in edges  # ① 意图分流
    assert ("planner", "route") in edges  # ② 追问回退
    assert ("validate", "solve") in edges  # ③ 驳回回环（自检，非常规路径）
    # 主路径的固定边
    for pair in (
        ("compile_spec", "solve"),
        ("solve", "validate"),
        ("validate", "explain"),
        ("explain", "resume_guard"),
        ("resume_guard", "human_gate"),
        ("human_gate", "commit_plan"),
    ):
        assert pair in edges, f"主路径缺边：{pair}"


def test_revision_loop_is_wired() -> None:
    """修订循环（无界）：human_gate → planner → solve → validate → explain。"""
    app = build_graph(deps())
    edges = {(e.source, e.target) for e in app.get_graph().edges}
    assert ("human_gate", "planner") in edges
    assert ("planner", "solve") in edges


def test_commit_plan_terminates() -> None:
    app = build_graph(deps())
    edges = {(e.source, e.target) for e in app.get_graph().edges}
    assert ("commit_plan", "__end__") in edges


# ─────────────────────────────────────────────────────────────────────
# 走一条不碰库的路径
# ─────────────────────────────────────────────────────────────────────
def test_query_intent_finishes_without_touching_the_database() -> None:
    """问答意图在图内到此为止 —— 由 W8 的 KnowledgeAgent 承接。"""
    app = build_graph(deps())
    state = initial_state(
        trace_id="t1",
        user_id="u1",
        messages=[{"role": "user", "content": "何超的训练进度"}],
    )
    result = app.invoke(state)
    assert result["intent"] == "query"
    assert result["request"].kind == "query"
    kinds = [e.kind for e in result["trace_events"]]
    assert "handoff" in kinds


def test_ambiguity_stops_at_the_human_gate() -> None:
    """歧义 → 反问 → `interrupt()` 挂起。整个过程一次都没碰库。"""
    app = build_graph(deps(), checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "amb-1"}}
    state = initial_state(
        trace_id="t2", user_id="u1", messages=[{"role": "user", "content": "帮我看一下"}]
    )
    result = app.invoke(state, config=cast(Any, config))
    assert "__interrupt__" in result


def test_trace_events_accumulate_across_nodes() -> None:
    """`add` reducer：多个节点写入不相互覆盖。"""
    app = build_graph(deps())
    state = initial_state(
        trace_id="t3", user_id="u1", messages=[{"role": "user", "content": "导出这周的表"}]
    )
    result = app.invoke(state)
    # route 至少写了 decision + handoff 两条
    assert len(result["trace_events"]) >= 2
    assert [e.seq for e in result["trace_events"]] == list(range(len(result["trace_events"])))


# ─────────────────────────────────────────────────────────────────────
# interrupt / resume 语义（内存 checkpointer）
# ─────────────────────────────────────────────────────────────────────
def test_interrupt_then_resume_carries_the_decision() -> None:
    """`interrupt()` 挂起 → `Command(resume=...)` 从断点继续，**不重跑前面的节点**。"""
    app = build_graph(deps(), checkpointer=InMemorySaver())
    config = cast(Any, {"configurable": {"thread_id": "resume-1"}})
    state = initial_state(
        trace_id="t4", user_id="u1", messages=[{"role": "user", "content": "帮我看一下"}]
    )
    first = app.invoke(state, config=config)
    assert "__interrupt__" in first

    resumed = app.invoke(
        Command(resume={"decision": "REJECT", "user_id": "u1", "role": "scheduler"}),
        config=config,
    )
    assert resumed["human_decision"].decision == "REJECT"
    # route 只跑过一次 —— 恢复不重跑前面的节点
    assert sum(1 for e in resumed["trace_events"] if e.agent == "route") == 1


def test_resume_with_an_unparseable_decision_raises() -> None:
    app = build_graph(deps(), checkpointer=InMemorySaver())
    config = cast(Any, {"configurable": {"thread_id": "resume-2"}})
    app.invoke(
        initial_state(
            trace_id="t5", user_id="u1", messages=[{"role": "user", "content": "帮我看一下"}]
        ),
        config=config,
    )
    with pytest.raises(ValueError, match="无法解析人工决策"):
        app.invoke(Command(resume=42), config=config)


# ─────────────────────────────────────────────────────────────────────
# Store（v6 §6.2）
# ─────────────────────────────────────────────────────────────────────
def test_memory_kinds_match_v6() -> None:
    assert MEMORY_KINDS == ("progress", "episodic", "procedural")


def test_namespace_is_tenant_first() -> None:
    assert namespace("nau", "episodic") == ("nau", "episodic")


def test_unknown_memory_kind_raises() -> None:
    with pytest.raises(ValueError, match="未知记忆类别"):
        namespace("nau", "whatever")  # type: ignore[arg-type]


def test_in_memory_store_round_trip() -> None:
    store = build_store(in_memory=True)
    remember(store, tenant_id="nau", kind="procedural", key="pref-1", value={"note": "周五少排"})
    assert recall(store, tenant_id="nau", kind="procedural", key="pref-1") == {"note": "周五少排"}
    assert recall(store, tenant_id="nau", kind="procedural", key="missing") is None


def test_store_namespaces_are_isolated_per_tenant() -> None:
    store = build_store(in_memory=True)
    remember(store, tenant_id="a", kind="episodic", key="k", value={"v": 1})
    assert recall(store, tenant_id="b", kind="episodic", key="k") is None


# ─────────────────────────────────────────────────────────────────────
# END 的翻译
# ─────────────────────────────────────────────────────────────────────
def test_end_literal_is_translated_at_the_graph_boundary() -> None:
    """`routing/rules.py` 写的是字符串 `"END"`，不依赖 LangGraph。"""
    from backend.graph.graph import _retarget
    from backend.routing.rules import next_node_for

    assert next_node_for("query") == "END"
    assert _retarget(Command(goto="END")).goto is END
    assert _retarget(Command(goto="planner")).goto == "planner"
