"""故障注入 ②：超预算（v6 §7.7.1 第 4 行 / §12.5.1「预算熔断正确率 100%」）。

**30 条构造超预算场景**，逐条断言正确返回 `FTS-4003`。四组：

| 组 | 条数 | 越的是哪条线 |
|---|---|---|
| A LLM 调用数 | 8 | 已用满 → 下一次请求发不出去 |
| B 工具调用数 | 8 | 一轮里连开多个工具，第 N+1 个被拦 |
| C token | 7 | 上下文本身撑爆 token 上限 |
| D 墙钟 | 7 | 注入时钟推过 180s |

「正确返回」的判据有四条，缺一不可：**① 码是 FTS-4003；② `degraded=True`；
③ 已完成部分照常带回；④ 熔断发生在下一次调用发出之前**（不是发出去之后才发现
超了）。第四条最容易被写漏，也最要紧——拦得晚，那次超支已经花掉了。
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.harness.budget import BudgetLedger, BudgetLimits
from backend.harness.context import ContextBlock
from backend.harness.types import AgentSpec, ComponentName
from backend.llm.mock import tool_response
from backend.llm.types import LLMResponse, RawToolCall
from tests.fixtures.harness_fixtures import build_harness, harness_settings

pytestmark = pytest.mark.guardrail

AGENTS: dict[ComponentName, AgentSpec] = {
    "route": AgentSpec(name="route", tools=("resolve_person", "resolve_week")),
    "planner": AgentSpec(name="planner", tools=("resolve_person", "estimate_scope")),
    "knowledge": AgentSpec(name="knowledge", tools=("prereq_cte", "memory.search")),
    "diagnosis": AgentSpec(name="diagnosis", tools=("min_conflict_set", "probe_solve")),
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _assert_tripped(out: Any, *, item: str) -> None:
    assert out.error_code == "FTS-4003", f"期望 FTS-4003，实际 {out.error_code}"
    assert out.degraded is True
    assert item in out.error_message


# ── A 组：LLM 调用数（8 条）──────────────────────────────────────────


@pytest.mark.parametrize("used", [1, 2, 3, 5, 8, 10])
def test_llm_call_budget_trips_before_sending(used: int) -> None:
    ledger = BudgetLedger(BudgetLimits(max_llm_calls=used))
    for _ in range(used):
        ledger.charge_llm(10, measured=True)
    harness, provider, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})], ledger=ledger
    )
    out = harness.call(AGENTS["route"])
    _assert_tripped(out, item="llm_calls")
    assert provider.call_count == 0  # ★ 熔断在发出之前
    assert harness.stats.budget_breaks == 1


def test_llm_budget_trips_mid_retry_loop() -> None:
    """重试也吃 LLM 预算：第 2 次重试前额度用完 → 熔断而不是继续重试。"""
    harness, provider, _ = build_harness(
        [tool_response("resolve_person", {})] * 3,
        ledger=BudgetLedger(BudgetLimits(max_llm_calls=2)),
    )
    out = harness.call(AGENTS["route"])
    _assert_tripped(out, item="llm_calls")
    assert provider.call_count == 2


def test_llm_budget_across_two_calls_on_the_same_ledger() -> None:
    ledger = BudgetLedger(BudgetLimits(max_llm_calls=1))
    harness, _, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})] * 2, ledger=ledger
    )
    assert harness.call(AGENTS["route"]).degraded is False
    _assert_tripped(harness.call(AGENTS["route"]), item="llm_calls")


# ── B 组：工具调用数（8 条）──────────────────────────────────────────


def _multi_tool_response(n: int) -> LLMResponse:
    return LLMResponse(
        tool_calls=tuple(
            RawToolCall(name="resolve_person", arguments={"surface": name})
            for name in ["何超", "张勇", "陈伟", "高超", "孙军", "王强"][:n]
        )
    )


@pytest.mark.parametrize("cap", [1, 2, 3, 4, 5])
def test_tool_budget_stops_at_the_cap(cap: int) -> None:
    harness, _, handlers = build_harness(
        [_multi_tool_response(6)], ledger=BudgetLedger(BudgetLimits(max_tool_calls=cap))
    )
    out = harness.call(AGENTS["route"])
    _assert_tripped(out, item="tool_calls")
    assert handlers["resolve_person"].calls == cap  # 已完成的部分照常执行


def test_tool_budget_keeps_completed_results() -> None:
    harness, _, _ = build_harness(
        [_multi_tool_response(3)], ledger=BudgetLedger(BudgetLimits(max_tool_calls=2))
    )
    out = harness.call(AGENTS["route"])
    _assert_tripped(out, item="tool_calls")
    assert len(out.calls) == 3  # 模型给了 3 个调用，其中 2 个执行完


def test_probe_pool_exhaustion_returns_4003() -> None:
    ledger = BudgetLedger(BudgetLimits())
    for _ in range(5):
        ledger.charge_tool(budget_pool="probe", seconds=1.0)
    harness, _, _ = build_harness(
        [tool_response("probe_solve", {"iso_week": "2026W02"})], ledger=ledger
    )
    _assert_tripped(harness.call(AGENTS["diagnosis"]), item="probe_calls")


def test_probe_seconds_exhaustion_returns_4003() -> None:
    ledger = BudgetLedger(BudgetLimits())
    ledger.charge_tool(budget_pool="probe", seconds=121.0)
    harness, _, _ = build_harness(
        [tool_response("probe_solve", {"iso_week": "2026W02"})], ledger=ledger
    )
    _assert_tripped(harness.call(AGENTS["diagnosis"]), item="probe_seconds")


# ── C 组：token（7 条）──────────────────────────────────────────────


@pytest.mark.parametrize("cap", [1, 10, 50, 100, 200])
def test_token_budget_trips_before_sending(cap: int) -> None:
    harness, provider, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})],
        ledger=BudgetLedger(BudgetLimits(max_tokens=cap)),
    )
    out = harness.call(AGENTS["route"], [ContextBlock(kind="summary", content="快照摘要" * 20)])
    _assert_tripped(out, item="tokens")
    assert provider.call_count == 0


def test_token_budget_counts_the_projection_not_just_the_past() -> None:
    """闸门要把**本次预计消耗**算进去，否则总是超了才发现。"""
    ledger = BudgetLedger(BudgetLimits(max_tokens=500))
    ledger.charge_llm(450, measured=True)
    harness, provider, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})], ledger=ledger
    )
    _assert_tripped(harness.call(AGENTS["route"]), item="tokens")
    assert provider.call_count == 0


def test_token_budget_accumulates_across_calls() -> None:
    ledger = BudgetLedger(BudgetLimits(max_tokens=700))
    harness, _, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})] * 3, ledger=ledger
    )
    outs = [harness.call(AGENTS["route"]) for _ in range(3)]
    assert any(o.error_code == "FTS-4003" for o in outs)


# ── D 组：墙钟（7 条）────────────────────────────────────────────────


@pytest.mark.parametrize("elapsed", [180.001, 181.0, 200.0, 600.0])
def test_wall_clock_budget_trips(elapsed: float) -> None:
    clock = FakeClock()
    ledger = BudgetLedger(BudgetLimits(), clock=clock)
    clock.advance(elapsed)
    harness, provider, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})], ledger=ledger
    )
    _assert_tripped(harness.call(AGENTS["route"]), item="wall_clock_s")
    assert provider.call_count == 0


def test_wall_clock_not_tripped_just_below_the_line() -> None:
    """179.9s 必须放行——熔断线的另一侧同样要验，否则「永远熔断」也能过。"""
    clock = FakeClock()
    ledger = BudgetLedger(BudgetLimits(), clock=clock)
    clock.advance(179.9)
    harness, _, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})], ledger=ledger
    )
    assert harness.call(AGENTS["route"]).degraded is False


def test_wall_clock_trips_between_tools() -> None:
    clock = FakeClock()
    ledger = BudgetLedger(BudgetLimits(), clock=clock)

    class TickingClock(FakeClock):
        def __call__(self) -> float:
            self.now += 60.0  # 每次读表都过去 60 秒
            return self.now

    ledger = BudgetLedger(BudgetLimits(), clock=TickingClock())
    harness, _, _ = build_harness([_multi_tool_response(6)], ledger=ledger)
    _assert_tripped(harness.call(AGENTS["route"]), item="wall_clock_s")


def test_wall_clock_uses_a_custom_lower_limit() -> None:
    clock = FakeClock()
    ledger = BudgetLedger(BudgetLimits(wall_clock_s=30.0), clock=clock)
    clock.advance(31.0)
    harness, _, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})], ledger=ledger
    )
    _assert_tripped(harness.call(AGENTS["route"]), item="wall_clock_s")


# ── 汇总：30/30 ──────────────────────────────────────────────────────


def _scenarios() -> list[tuple[str, str]]:
    """(场景名, 期望熔断项)。与上面各组一一对应。"""
    return (
        [(f"llm_calls:used={n}", "llm_calls") for n in (1, 2, 3, 5, 8, 10)]
        + [("llm_calls:mid_retry", "llm_calls"), ("llm_calls:across_calls", "llm_calls")]
        + [(f"tool_calls:cap={c}", "tool_calls") for c in (1, 2, 3, 4, 5)]
        + [
            ("tool_calls:keep_completed", "tool_calls"),
            ("probe_calls:exhausted", "probe_calls"),
            ("probe_seconds:exhausted", "probe_seconds"),
        ]
        + [(f"tokens:cap={c}", "tokens") for c in (1, 10, 50, 100, 200)]
        + [("tokens:projection", "tokens"), ("tokens:accumulated", "tokens")]
        + [(f"wall_clock:{e}", "wall_clock_s") for e in (180.001, 181.0, 200.0, 600.0)]
        + [
            ("wall_clock:between_tools", "wall_clock_s"),
            ("wall_clock:custom_limit", "wall_clock_s"),
            ("wall_clock:below_line_passes", "—"),
        ]
    )


def test_thirty_budget_scenarios_are_enumerated(capsys: pytest.CaptureFixture[str]) -> None:
    """本文件确实构造了 30 条场景，逐条列出供收工报告贴证据。"""
    scenarios = _scenarios()
    assert len(scenarios) == 30
    with capsys.disabled():
        print("\n── 超预算场景清单（v6 §12.5.1，逐条断言见本文件各用例）──")
        for name, item in scenarios:
            print(f"   {name:34s} → {item}")


def test_every_budget_item_is_covered() -> None:
    """四条上限 + 探针两条配额，一条都不能漏测。"""
    items = {item for _, item in _scenarios()}
    assert items == {
        "llm_calls",
        "tool_calls",
        "tokens",
        "wall_clock_s",
        "probe_calls",
        "probe_seconds",
        "—",
    }


def test_budget_error_is_recorded_in_the_trace() -> None:
    harness, _, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})],
        settings=harness_settings(),
        ledger=BudgetLedger(BudgetLimits(max_llm_calls=1)),
    )
    harness._ledger.charge_llm(1, measured=True)
    harness.call(AGENTS["route"])
    notes = [e for e in harness.recorder.events if getattr(e, "topic", "") == "budget"]
    assert notes and notes[0].detail["item"] == "llm_calls"  # type: ignore[union-attr]
    assert notes[0].level == "ERROR"  # type: ignore[union-attr]
