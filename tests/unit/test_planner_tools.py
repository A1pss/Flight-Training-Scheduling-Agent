"""Planner / 意图路由的工具接线（v6 §7.7.2 第 1~4 行）。

**这个文件是为了不再漏接。** 真机端到端第一次跑时，`planner` 节点当场抛
`ToolNotBoundError: 工具 'resolve_week' 在目录中但没有接上实现` —— 九个工具
声明了但没接 handler，而单测用 `FakeHarness` 照不出来。所以这里逐个验：

1. **ACL 行里的每个工具都有 handler**（漏一个就红）；
2. **每个 handler 的返回值可 JSON 序列化**（要进 trace 与 Redis 缓存）；
3. 消解不了时返回 `resolved: false` 而不是抛异常。
"""

from __future__ import annotations

import json
from datetime import date, time
from typing import Any

import pytest

from backend.harness import ACL_MATRIX, DEFAULT_REGISTRY, ToolRegistry
from backend.planner.intent import PLANNER_TOOLS
from backend.planner.tools import planner_tool_handlers, route_tool_handlers
from backend.routing.classify import ROUTE_AGENT
from backend.schemas.intent import ObjectiveWeights, SolveIntent
from tests.fixtures.graph_fixtures import directory, plan, sortie

TODAY = date(2026, 1, 7)


@pytest.fixture
def handlers() -> dict[str, Any]:
    return planner_tool_handlers(
        directory=directory(),
        today=TODAY,
        prev_plan=plan(
            [
                sortie("S000001", day=0, crew=(("P01", "教员"), ("P06", "学员"))),
                sortie(
                    "S000002",
                    day=2,
                    takeoff=time(9, 0),
                    aircraft_id="AC27",
                    crew=(("P08", "单飞"),),
                ),
            ]
        ),
        user_role="scheduler",
    )


# ─────────────────────────────────────────────────────────────────────
# 覆盖面：ACL 行里的工具一个都不能漏
# ─────────────────────────────────────────────────────────────────────
def test_every_planner_tool_has_a_handler(handlers: dict[str, Any]) -> None:
    """`AgentSpec` 暴露什么，就得有什么 handler —— 漏一个真机就抛。"""
    assert set(PLANNER_TOOLS) <= set(handlers)


def test_every_route_tool_has_a_handler() -> None:
    assert set(ROUTE_AGENT.tools) <= set(route_tool_handlers(directory=directory(), today=TODAY))


def test_route_handlers_are_a_strict_subset_of_planner_handlers() -> None:
    """路由与 Planner 共用前五个工具 —— 少给可以，多给不行。"""
    route = set(route_tool_handlers(directory=directory(), today=TODAY))
    assert route <= ACL_MATRIX["route"]
    assert route == {
        "resolve_person",
        "resolve_aircraft",
        "resolve_week",
        "ask_user",
        "escalate",
    }


def test_handlers_can_actually_be_registered(handlers: dict[str, Any]) -> None:
    """注册期还要过 `ToolACL.assert_registrable` —— 名字不在目录里就抛。"""
    registry = ToolRegistry()
    registry.register_many(handlers)
    for name in PLANNER_TOOLS:
        assert registry.is_bound(name)
    # 默认注册表不该被污染（一个请求一份接线）
    assert not DEFAULT_REGISTRY.is_bound("estimate_scope")


# ─────────────────────────────────────────────────────────────────────
# 返回值必须可 JSON 序列化
# ─────────────────────────────────────────────────────────────────────
SAMPLE_ARGS: dict[str, dict[str, Any]] = {
    "resolve_person": {"surface": "何超"},
    "resolve_aircraft": {"surface": "49 号机"},
    "resolve_week": {"surface": "下周", "reference_date": ""},
    "ask_user": {"question": "要连带调整教员吗？", "resolution": "answer"},
    "escalate": {"reason": "用户要求绕过资质要求", "severity": "ERROR"},
    "estimate_scope": {"iso_week": "2026W02", "scope_persons": "ALL", "scope_missions": "ALL"},
    "assess_disruption": {"iso_week": "2026W02", "changed_persons": ["P08"]},
    "translate_revision": {"utterance": "刘斌周五别排了", "round_no": 1, "iso_week": "2026W02"},
    "check_authority": {"actor_role": "排班员", "requested_tier": 3},
}


@pytest.mark.parametrize("name", sorted(SAMPLE_ARGS))
def test_handler_returns_json_serializable(name: str, handlers: dict[str, Any]) -> None:
    json.dumps(handlers[name](SAMPLE_ARGS[name]), ensure_ascii=False)


def test_propose_solve_intent_round_trips(handlers: dict[str, Any]) -> None:
    intent = SolveIntent(
        scope_persons=["P08"],
        scope_missions="ALL",
        freeze_policy="CONSERVATIVE",
        freeze_reason="只动何超",
        objective_weights=ObjectiveWeights(progress=2.0, disruption=1.0, balance=1.0),
        pre_authorized_tiers=[0],
        estimated_blast_radius=0,
    )
    out = handlers["propose_solve_intent"](
        {"iso_week": "2026W02", "intent": intent.model_dump(mode="json"), "rationale": "理由"}
    )
    json.dumps(out, ensure_ascii=False)
    assert out["accepted"] and out["scope_persons"] == ["P08"]
    assert out["freeze_policy"] == "CONSERVATIVE"


# ─────────────────────────────────────────────────────────────────────
# 行为
# ─────────────────────────────────────────────────────────────────────
def test_resolution_payload_shape(handlers: dict[str, Any]) -> None:
    resolved = handlers["resolve_person"]({"surface": "何超"})
    assert resolved == {
        "resolved": True,
        "entity_id": "P08",
        "confidence": 1.0,
        "reason": "exact_name",
    }


def test_ambiguity_comes_back_as_a_question_not_an_exception(handlers: dict[str, Any]) -> None:
    """模型该看见的是「去问用户」，不是一个栈回溯。"""
    out = handlers["resolve_person"]({"surface": "郝超"})
    assert out["resolved"] is False
    assert out["reason"] == "ambiguous"
    assert "高超(P02)" in out["question"]
    assert {c["entity_id"] for c in out["candidates"]} == {"P02", "P08"}


def test_unknown_id_is_not_found_not_invented(handlers: dict[str, Any]) -> None:
    out = handlers["resolve_person"]({"surface": "P99"})
    assert out["resolved"] is False and out["entity_id"] is None


def test_resolve_week_uses_the_injected_reference_date(handlers: dict[str, Any]) -> None:
    """模型没给参照日期时用调用方注入的 `today` —— 重放要它稳定。"""
    assert handlers["resolve_week"]({"surface": "本周"})["entity_id"] == "2026W02"
    assert (
        handlers["resolve_week"]({"surface": "本周", "reference_date": "2026-01-14"})["entity_id"]
        == "2026W03"
    )


def test_estimate_scope_is_zero_without_a_baseline() -> None:
    handlers = planner_tool_handlers(directory=directory(), today=TODAY, prev_plan=None)
    out = handlers["estimate_scope"]({"iso_week": "2026W02"})
    assert out["estimated_blast_radius"] == 0
    assert "首轮排班" in out["note"]


def test_assess_disruption_says_so_when_there_is_no_baseline() -> None:
    """没有基线时如实说「没有基线」，不编一个 0 出来。"""
    handlers = planner_tool_handlers(directory=directory(), today=TODAY, prev_plan=None)
    out = handlers["assess_disruption"]({"iso_week": "2026W02"})
    assert out["has_baseline"] is False


def test_assess_disruption_lists_directly_touched_sorties(handlers: dict[str, Any]) -> None:
    out = handlers["assess_disruption"]({"iso_week": "2026W02", "changed_persons": ["P08"]})
    assert out["has_baseline"] is True
    assert out["directly_touched"] == ["S000002"]


def test_translate_revision_reports_failure_instead_of_guessing(handlers: dict[str, Any]) -> None:
    out = handlers["translate_revision"](
        {"utterance": "嗯就这样吧", "round_no": 1, "iso_week": "2026W02"}
    )
    assert out["translated"] is False
    assert "换一种说法" in out["note"]


def test_translate_revision_returns_the_echo(handlers: dict[str, Any]) -> None:
    out = handlers["translate_revision"](
        {"utterance": "刘斌周五别排了", "round_no": 2, "iso_week": "2026W02"}
    )
    assert out["translated"] is True
    assert out["kind"] == "FORBID"
    assert out["targets"] == ["P04"]
    assert out["round_no"] == 2
    assert out["echo"].startswith("我理解为：")


def test_check_authority_blocks_tier3_for_a_scheduler(handlers: dict[str, Any]) -> None:
    out = handlers["check_authority"]({"actor_role": "排班员", "requested_tier": 3})
    assert out["granted"] is False
    assert out["required_role"] == "director"


def test_check_authority_falls_back_to_the_session_role() -> None:
    handlers = planner_tool_handlers(directory=directory(), today=TODAY, user_role="director")
    out = handlers["check_authority"]({"actor_role": "", "requested_tier": 3})
    assert out["granted"] is True


def test_ask_user_and_escalate_land_in_the_sink() -> None:
    """它们是**要交给人的东西**，不是给模型自己看的返回值。"""
    sink: list[dict[str, Any]] = []
    handlers = planner_tool_handlers(directory=directory(), today=TODAY, sink=sink)
    handlers["ask_user"]({"question": "排哪一周？", "resolution": "answer"})
    handlers["escalate"]({"reason": "要求绕过资质", "severity": "CRITICAL"})
    assert [item["kind"] for item in sink] == ["ask_user", "escalate"]
    assert sink[0]["question"] == "排哪一周？"
    assert sink[1]["severity"] == "CRITICAL"
