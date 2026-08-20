"""预算控制与上下文装配（v6 §7.7.1 第 4、5 行）。

预算这块的用例用**可注入时钟**，不用 `sleep`：墙钟熔断要验的是「到点就断」，
不是「等得够久」。真去 sleep 180 秒的用例没人会跑第二次。
"""

from __future__ import annotations

import pytest

from backend.core.config import Settings
from backend.core.errors import BudgetExceededError
from backend.harness.budget import (
    SPEC_MAX_LLM_CALLS,
    SPEC_MAX_TOKENS,
    SPEC_MAX_TOOL_CALLS,
    SPEC_WALL_CLOCK_S,
    BudgetLedger,
    BudgetLimits,
    ProbeBudgetLimits,
)
from backend.harness.context import (
    ContextAssembler,
    ContextBlock,
    structured_summary,
)
from backend.harness.tokens import estimate_messages, estimate_tokens


class FakeClock:
    """手动推进的时钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ─── 预算上限就是 v6 写的那四个数 ───────────────────────────────────


def test_spec_limits_match_v6() -> None:
    """★ 这四个数就是 v6 §7.7.1 那一行。改它们等于改设计方案。

    LLM 调用上限 2026-08-20 由 10 提到 **14**（`Z-34`）：不是「10 不够用」，
    而是 §7.6 的预算表**漏了 KnowledgeAgent 检索循环**（改写 1 + 自主循环 ≤6 步
    + 带引用生成 1 = 8，只剩 2 次给契约重试）。M9-A 实测 320 条探针里 71 条顶到
    旧上限被截断 —— 那是量具装小了，不是模型不行。
    """
    assert (SPEC_MAX_LLM_CALLS, SPEC_MAX_TOOL_CALLS, SPEC_WALL_CLOCK_S, SPEC_MAX_TOKENS) == (
        14,
        20,
        180.0,
        40_000,
    )


def test_defaults_come_from_spec() -> None:
    limits = BudgetLimits()
    assert limits.max_llm_calls == 14
    assert limits.max_tool_calls == 20
    assert limits.wall_clock_s == 180.0
    assert limits.max_tokens == 40_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_llm_calls", 15),
        ("max_tool_calls", 21),
        ("wall_clock_s", 181.0),
        ("max_tokens", 40_001),
    ],
)
def test_limits_cannot_be_loosened_beyond_spec(field: str, value: float) -> None:
    """配置只能往严里调。放宽 = 悄悄改设计方案。"""
    with pytest.raises(ValueError, match="less than or equal"):
        BudgetLimits(**{field: value})  # type: ignore[arg-type]


def test_settings_defaults_come_from_spec_too() -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    limits = BudgetLimits.from_settings(cfg)
    assert (limits.max_llm_calls, limits.max_tool_calls, limits.max_tokens) == (14, 20, 40_000)


# ─── 四条闸各自熔断 ─────────────────────────────────────────────────


def test_llm_cap_does_not_block_running_the_tools_of_the_last_response() -> None:
    """LLM 额度用满**不该**拦住已拿到的那轮响应里的工具。

    拦住它只会丢掉已完成的工作，还会把熔断项报成 `llm_calls` —— 让排查的人
    去查一个根本没超的量。
    """
    ledger = BudgetLedger(BudgetLimits(max_llm_calls=1))
    ledger.charge_llm(10, measured=True)
    ledger.check_tool("resolve_person")  # 不抛
    with pytest.raises(BudgetExceededError):
        ledger.check_llm(10)


def test_llm_call_cap() -> None:
    ledger = BudgetLedger(BudgetLimits(max_llm_calls=3))
    for _ in range(3):
        ledger.check_llm(10)
        ledger.charge_llm(10, measured=True)
    with pytest.raises(BudgetExceededError) as exc:
        ledger.check_llm(10)
    assert exc.value.code.value == "FTS-4003"
    assert exc.value.details["item"] == "llm_calls"


def test_tool_call_cap() -> None:
    ledger = BudgetLedger(BudgetLimits(max_tool_calls=2))
    for _ in range(2):
        ledger.check_tool("resolve_person")
        ledger.charge_tool()
    with pytest.raises(BudgetExceededError) as exc:
        ledger.check_tool("resolve_person")
    assert exc.value.details["item"] == "tool_calls"


def test_token_cap_counts_projection() -> None:
    """token 闸要把**本次预计消耗**算进去——拦得晚，超的那次已经花掉了。"""
    ledger = BudgetLedger(BudgetLimits(max_tokens=1000))
    ledger.charge_llm(900, measured=True)
    with pytest.raises(BudgetExceededError) as exc:
        ledger.check_llm(200)
    assert exc.value.details["item"] == "tokens"
    assert exc.value.details["actual"] == 1100


def test_wall_clock_cap() -> None:
    clock = FakeClock()
    ledger = BudgetLedger(BudgetLimits(wall_clock_s=180.0), clock=clock)
    ledger.check()
    clock.advance(180.5)
    with pytest.raises(BudgetExceededError) as exc:
        ledger.check()
    assert exc.value.details["item"] == "wall_clock_s"


def test_error_carries_usage_and_suggestions() -> None:
    ledger = BudgetLedger(BudgetLimits(max_llm_calls=1), trace_id="t1")
    ledger.charge_llm(5, measured=True)
    with pytest.raises(BudgetExceededError) as exc:
        ledger.check_llm()
    assert exc.value.details["usage"]["llm_calls"] == 1
    assert exc.value.details["trace_id"] == "t1"
    assert exc.value.suggestions


# ─── 探针独立池（§3.9.2）────────────────────────────────────────────


def test_probe_pool_is_separate_from_tool_budget() -> None:
    ledger = BudgetLedger(BudgetLimits(), ProbeBudgetLimits(max_calls=2, total_s=120.0))
    for _ in range(2):
        ledger.check_tool("probe_solve", budget_pool="probe")
        ledger.charge_tool(budget_pool="probe", seconds=10.0)
    with pytest.raises(BudgetExceededError) as exc:
        ledger.check_tool("probe_solve", budget_pool="probe")
    assert exc.value.details["item"] == "probe_calls"
    # 主池没被这两次探针耗尽（但确实各记了一次工具调用）
    assert ledger.usage().tool_calls == 2


def test_probe_seconds_cap() -> None:
    ledger = BudgetLedger(BudgetLimits(), ProbeBudgetLimits(max_calls=99, total_s=30.0))
    ledger.charge_tool(budget_pool="probe", seconds=31.0)
    with pytest.raises(BudgetExceededError) as exc:
        ledger.check_tool("probe_solve", budget_pool="probe")
    assert exc.value.details["item"] == "probe_seconds"


def test_probe_calls_still_count_as_tool_calls() -> None:
    """独立的是配额，不是「这次调用不存在」——否则 ≤20 会被探针绕开。"""
    ledger = BudgetLedger(BudgetLimits(max_tool_calls=1))
    ledger.charge_tool(budget_pool="probe", seconds=1.0)
    with pytest.raises(BudgetExceededError):
        ledger.check_tool("probe_solve", budget_pool="probe")


# ─── 实测 vs 估算的标记（铁律 6）────────────────────────────────────


def test_measured_tokens_are_not_marked_estimated() -> None:
    ledger = BudgetLedger()
    ledger.charge_llm(120, measured=True)
    assert ledger.usage().tokens_estimated is False


def test_one_estimated_charge_taints_the_whole_ledger() -> None:
    ledger = BudgetLedger()
    ledger.charge_llm(120, measured=True)
    ledger.charge_llm(80, measured=False)
    assert ledger.usage().tokens_estimated is True
    assert ledger.usage().tokens == 200


# ─── token 估算器 ───────────────────────────────────────────────────


def test_estimator_is_deterministic() -> None:
    text = "给何超排本周的训练计划，avoid Wednesday"
    assert estimate_tokens(text) == estimate_tokens(text)


def test_estimator_counts_cjk_per_char() -> None:
    assert estimate_tokens("排班") == 2
    assert estimate_tokens("") == 0


def test_estimate_messages_includes_overhead() -> None:
    msgs = [{"role": "user", "content": "排班"}]
    assert estimate_messages(msgs) > estimate_tokens("排班")


# ─── 上下文装配 ─────────────────────────────────────────────────────


def _blocks(n_history: int = 0, n_evidence: int = 0, big: int = 1) -> list[ContextBlock]:
    blocks = [
        ContextBlock(kind="system", role="system", content="系统提示词" * big, label="sys"),
        ContextBlock(kind="decision", content="用户要求：周三不排何超" * big, label="d1"),
        ContextBlock(kind="summary", content="快照摘要" * big, label="snap"),
    ]
    blocks.extend(
        ContextBlock(kind="history", content=f"第{i}轮对话" * big, label=f"h{i}")
        for i in range(n_history)
    )
    blocks.extend(
        ContextBlock(kind="evidence", content=f"检索片段{i}" * big, label=f"e{i}")
        for i in range(n_evidence)
    )
    return blocks


def test_nothing_dropped_when_it_fits() -> None:
    result = ContextAssembler(num_ctx=8192, reserve_output_tokens=1024).assemble(_blocks(2, 2))
    assert result.dropped == ()
    assert result.warnings == ()
    assert len(result.messages) == 7


def test_history_sliding_window() -> None:
    result = ContextAssembler(num_ctx=8192, reserve_output_tokens=1024, history_window=3).assemble(
        _blocks(n_history=6)
    )
    kept = [m["content"] for m in result.messages]
    assert sum("轮对话" in c for c in kept) == 3
    assert any("h0" in d for d in result.dropped)
    assert "滑窗" in result.dropped[0]


def test_trims_lowest_priority_first_and_keeps_pinned() -> None:
    assembler = ContextAssembler(num_ctx=200, reserve_output_tokens=50, history_window=10)
    result = assembler.assemble(_blocks(n_history=2, n_evidence=2, big=8))
    contents = [m["content"] for m in result.messages]
    # 钉住的两块必须还在
    assert any("系统提示词" in c for c in contents)
    assert any("周三不排何超" in c for c in contents)
    # 证据是最先被裁的
    assert any(d.startswith("evidence") for d in result.dropped)
    assert result.warnings and "裁剪" in result.warnings[0]


def test_pinned_overflow_is_reported_not_silently_truncated() -> None:
    assembler = ContextAssembler(num_ctx=30, reserve_output_tokens=10)
    result = assembler.assemble(
        [ContextBlock(kind="system", role="system", content="很长的系统提示词" * 50)]
    )
    assert any("PINNED_OVERFLOW" in w for w in result.warnings)
    assert len(result.messages) == 1  # 没截断，如实返回


def test_budget_is_ctx_minus_reserve() -> None:
    assert ContextAssembler(num_ctx=8192, reserve_output_tokens=1024).budget == 7168


def test_structured_summary_reports_scale_and_points_at_tools() -> None:
    """结构化数据只入摘要：明细由工具按需取（§7.7.1 第 5 行）。"""
    payload = {
        "人员": [f"P{i:02d}" for i in range(8)],
        "飞机": [f"AC{i}" for i in range(8)],
        "扰动": ["吴鹏 01-05 不可用", "AC73 01-09 定检", "刘斌 C 类 01-07 到期"],
    }
    text = structured_summary("基准周快照", payload)
    assert "8 项" in text
    assert "明细请调用相应工具" in text


def test_structured_summary_size_does_not_grow_with_the_data() -> None:
    """这才是「只入摘要」的实质：**摘要长度与数据规模脱钩**。

    小快照上摘要不见得比原始 JSON 短（表头和那句提示要占几十 token）；
    真正要保证的是数据涨 100 倍时摘要**不涨**——8K 窗口就是这么守住的。
    """
    small = {"人员": [f"P{i}" for i in range(8)], "架次": [f"S{i:06d}" for i in range(14)]}
    large = {"人员": [f"P{i}" for i in range(800)], "架次": [f"S{i:06d}" for i in range(1400)]}

    small_summary = estimate_tokens(structured_summary("快照", small))
    large_summary = estimate_tokens(structured_summary("快照", large))

    assert abs(large_summary - small_summary) <= 5
    assert large_summary < estimate_tokens(str(large)) / 50
