"""故障注入 ③：畸形 tool call 的五类分类与回灌（v6 §12.5.1 / §15.2）。

五类失败模式（`missing_field` / `type_error` / `entity_hallucination` /
`enum_out_of_range` / `json_malformed`）在这里逐类构造、逐类断言：

1. **被正确分类**——归错桶，§15.2 的难负例挖掘就取错样本；
2. **被正确回灌**——回灌消息里必须有「哪个字段、期望什么、实际收到什么」；
3. **可纠正的那几类真能被纠正**，**纠正不了的那一类如实耗尽重试**。

第 3 条是这组用例的重点：`entity_hallucination` 是 §12.5.1 里那个**硬地板**——
模型不知道「何超」对应哪个 `person_id`，回灌一百次它还是在猜。所以这里刻意构造
「一直猜」的场景，验证系统的反应是**如实降级**（FTS-4002 转人工表单），
而不是把某次瞎猜当成成功。W13 判断最终通过率能不能从 97% 上调回 98%，
靠的就是这类失败在真实分布里占多少。
"""

from __future__ import annotations

import pytest

from backend.harness.types import AgentSpec, FailureMode
from backend.llm.mock import text_response, tool_response
from tests.fixtures.harness_fixtures import build_harness

pytestmark = pytest.mark.guardrail

ROUTE = AgentSpec(name="route", tools=("resolve_person", "resolve_week", "ask_user"))
KNOWLEDGE = AgentSpec(name="knowledge", tools=("prereq_cte", "sql_query", "memory.search"))
PLANNER = AgentSpec(name="planner", tools=("check_authority", "estimate_scope"))

#: 五类畸形输出的构造样本：(失败模式, 组件, 畸形调用, 修正后的调用)
MALFORMED_CASES = [
    (
        FailureMode.MISSING_FIELD,
        ROUTE,
        tool_response("resolve_person", {}),
        tool_response("resolve_person", {"surface": "何超"}),
    ),
    (
        FailureMode.TYPE_ERROR,
        KNOWLEDGE,
        tool_response("memory.search", {"query": "何超的进度", "top_k": "五条"}),
        tool_response("memory.search", {"query": "何超的进度", "top_k": 5}),
    ),
    (
        FailureMode.ENTITY_HALLUCINATION,
        KNOWLEDGE,
        tool_response("prereq_cte", {"person_id": "何超", "mission_id": "missionB-1"}),
        tool_response("prereq_cte", {"person_id": "P08", "mission_id": "missionB-1"}),
    ),
    (
        FailureMode.ENUM_OUT_OF_RANGE,
        PLANNER,
        tool_response("check_authority", {"actor_role": "值班长", "requested_tier": 1}),
        tool_response("check_authority", {"actor_role": "训练主任", "requested_tier": 1}),
    ),
    (
        FailureMode.JSON_MALFORMED,
        ROUTE,
        tool_response("resolve_person", '{"surface": "何超"'),
        tool_response("resolve_person", {"surface": "何超"}),
    ),
]


@pytest.mark.parametrize(
    ("mode", "agent", "bad", "good"),
    MALFORMED_CASES,
    ids=[case[0].value for case in MALFORMED_CASES],
)
def test_each_malformed_shape_is_classified_and_fed_back(
    mode: FailureMode, agent: AgentSpec, bad: object, good: object
) -> None:
    harness, _, _ = build_harness([bad, good])  # type: ignore[list-item]
    out = harness.call(agent)

    # ① 分类正确
    failure = out.attempts[0].failures[0]
    assert failure.mode is mode
    assert harness.stats.failure_modes == {mode.value: 1}

    # ② 回灌带够信息
    feedback = harness.recorder.events[2].request.messages[-1]["content"]  # type: ignore[union-attr]
    assert mode.value in feedback
    if failure.field_path:
        assert failure.field_path in feedback

    # ③ 纠正后通过
    assert out.degraded is False
    assert out.llm_calls == 2


def test_all_five_modes_are_exercised() -> None:
    assert {case[0] for case in MALFORMED_CASES} == set(FailureMode)


def test_entity_hallucination_that_never_corrects_degrades_honestly() -> None:
    """硬地板：一直猜编号 → 如实转人工表单，不把瞎猜当成功。"""
    harness, provider, handlers = build_harness(
        [
            tool_response("prereq_cte", {"person_id": "何超", "mission_id": "missionB-1"}),
            tool_response("prereq_cte", {"person_id": "P99", "mission_id": "missionB-1"}),
            tool_response("prereq_cte", {"person_id": "HE-CHAO", "mission_id": "missionB-1"}),
        ]
    )
    out = harness.call(KNOWLEDGE)

    assert out.degraded is True
    assert out.error_code == "FTS-4002"
    assert provider.call_count == 3  # 首次 + 2 次重试，不多不少
    assert handlers["prereq_cte"].calls == 0  # 一次都没执行到工具
    assert harness.stats.failure_modes == {"entity_hallucination": 3}


def test_feedback_tells_the_model_to_resolve_ids_first() -> None:
    """对编造实体这一类，回灌里要给出**可执行**的下一步，而不只是「错了」。"""
    harness, _, _ = build_harness(
        [
            tool_response("prereq_cte", {"person_id": "何超", "mission_id": "missionB-1"}),
            tool_response("prereq_cte", {"person_id": "P08", "mission_id": "missionB-1"}),
        ]
    )
    harness.call(KNOWLEDGE)
    feedback = harness.recorder.events[2].request.messages[-1]["content"]  # type: ignore[union-attr]
    assert "resolve_person" in feedback


def test_mixed_batch_reports_every_bad_call() -> None:
    """一轮里多个调用都畸形时，每一个都要计数，不能只记第一个。"""
    from backend.llm.types import LLMResponse, RawToolCall

    response = LLMResponse(
        tool_calls=(
            RawToolCall(name="resolve_person", arguments={}),
            RawToolCall(name="resolve_week", arguments={"surface": ""}),
        )
    )
    harness, _, _ = build_harness([response] * 3)
    out = harness.call(ROUTE)
    assert len(out.attempts[0].failures) == 2
    assert harness.stats.failure_modes == {"missing_field": 6}


def test_partial_batch_still_fails_the_whole_attempt() -> None:
    """一好一坏时整轮重试：先执行「好的那半」会让重试变成重复执行。"""
    from backend.llm.types import LLMResponse, RawToolCall

    bad_batch = LLMResponse(
        tool_calls=(
            RawToolCall(name="resolve_person", arguments={"surface": "何超"}),
            RawToolCall(name="resolve_week", arguments={}),
        )
    )
    harness, _, handlers = build_harness(
        [bad_batch, tool_response("resolve_person", {"surface": "何超"})]
    )
    out = harness.call(ROUTE)
    assert out.degraded is False
    assert handlers["resolve_person"].calls == 1  # 只在成功那轮跑了一次


def test_malformed_json_in_constrained_mode_is_classified() -> None:
    harness, _, _ = build_harness([text_response("{'tool': 'resolve_person',}")] * 3)
    for _ in range(5):
        harness.modes.report_failure("route")
    out = harness.call(ROUTE)
    assert out.mode == "constrained_json"
    assert out.attempts[0].failures[0].mode is FailureMode.JSON_MALFORMED


def test_failure_mode_distribution_is_available_for_offline_mining() -> None:
    """§15.2 的难负例挖掘直接吃这张分布表——它必须是可读出来的。"""
    harness, _, _ = build_harness(
        [
            tool_response("resolve_person", {}),
            tool_response("resolve_person", {"surface": "何超"}),
            tool_response("prereq_cte", {"person_id": "何超", "mission_id": "missionB-1"}),
            tool_response("prereq_cte", {"person_id": "P08", "mission_id": "missionB-1"}),
        ]
    )
    harness.call(ROUTE)
    harness.call(KNOWLEDGE)
    assert harness.stats.failure_mode_counter == {"missing_field": 1, "entity_hallucination": 1}
    assert harness.stats.first_pass == 0
    assert harness.stats.llm_requests == 4


def test_mode_switches_after_repeated_parse_failures(capsys: pytest.CaptureFixture[str]) -> None:
    """失败率上升 → 自动切 `constrained_json`（§7.7.1 第 2 行，统计驱动）。"""
    harness, _, _ = build_harness([tool_response("resolve_person", {})] * 9)
    assert harness.modes.pick("route") == "native"

    harness.call(ROUTE)  # 3 次失败：样本还不够（min_samples=5），不动
    assert harness.modes.pick("route") == "native"

    harness.call(ROUTE)  # 累计 5 次失败 → 失败率 100% ≥ 30%，切
    assert harness.modes.pick("route") == "constrained_json"

    stats = harness.modes.stats("route")
    switches = [e for e in harness.recorder.events if getattr(e, "topic", "") == "mode_switch"]
    with capsys.disabled():
        print(
            f"\n   模式切换：失败率 {stats.failure_rate:.0%}（窗口 {stats.window_size}）"
            f" → {stats.mode}；trace 里记了 {len(switches)} 条 mode_switch"
        )
    assert switches and "native" in switches[0].message  # type: ignore[union-attr]
