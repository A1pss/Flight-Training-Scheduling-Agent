"""预算控制（v6 §7.7.1 第 4 行，超限即 FTS-4003）。

**每请求上限：LLM 调用 ≤10、工具调用 ≤20、墙钟 ≤180s、token ≤40k。**
四个量各记各的，任何一个越线立刻中断——`FTS-4003` 的行为是「中断并返回已完成
部分，提示缩小请求范围」（v6 §9.3），所以熔断点必须在**发出下一次调用之前**，
不是在调用回来之后：拦得晚，超的那一次已经花掉了。

`probe_solve` 走**独立预算池**（v6 §3.9.2）：诊断路径上探针可能跑掉几十秒，
拿常规工具预算去扛会把正常诊断挤死。独立池的三个上限——单次 30s、总计 120s、
最多 5 次——与主池并行计数。

> **一个刻意的选择**：探针调用同时计入主池的「工具调用数」。「独立预算池」独立
> 的是**时间与次数配额**，不是「这次调用不存在」——一次 tool call 就是一次
> tool call，不然 ≤20 这条上限会被探针绕开。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from backend.core.config import Settings, get_settings
from backend.core.errors import BudgetExceededError

#: v6 §7.7.1 的四条硬上限。配置可以调低（更严），不许调高。
SPEC_MAX_LLM_CALLS: Final[int] = 10
SPEC_MAX_TOOL_CALLS: Final[int] = 20
SPEC_WALL_CLOCK_S: Final[float] = 180.0
SPEC_MAX_TOKENS: Final[int] = 40_000


class BudgetLimits(BaseModel):
    """一次请求的预算上限。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_llm_calls: int = Field(default=SPEC_MAX_LLM_CALLS, ge=1, le=SPEC_MAX_LLM_CALLS)
    max_tool_calls: int = Field(default=SPEC_MAX_TOOL_CALLS, ge=1, le=SPEC_MAX_TOOL_CALLS)
    wall_clock_s: float = Field(default=SPEC_WALL_CLOCK_S, gt=0, le=SPEC_WALL_CLOCK_S)
    max_tokens: int = Field(default=SPEC_MAX_TOKENS, ge=1, le=SPEC_MAX_TOKENS)

    @classmethod
    def from_settings(cls, cfg: Settings | None = None) -> BudgetLimits:
        settings = cfg or get_settings()
        return cls(
            max_llm_calls=settings.HARNESS_MAX_LLM_CALLS,
            max_tool_calls=settings.HARNESS_MAX_TOOL_CALLS,
            wall_clock_s=settings.HARNESS_WALL_CLOCK_S,
            max_tokens=settings.HARNESS_MAX_TOKENS,
        )


class ProbeBudgetLimits(BaseModel):
    """探针预算池（v6 §3.9.2）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_calls: int = Field(default=5, ge=0)
    per_call_s: float = Field(default=30.0, gt=0)
    total_s: float = Field(default=120.0, gt=0)

    @classmethod
    def from_settings(cls, cfg: Settings | None = None) -> ProbeBudgetLimits:
        settings = cfg or get_settings()
        return cls(
            max_calls=settings.PROBE_MAX_CALLS,
            per_call_s=settings.PROBE_TIME_LIMIT_S,
            total_s=settings.PROBE_TOTAL_BUDGET_S,
        )


class BudgetUsage(BaseModel):
    """账本快照。进 trace、进降级时的错误 details。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    llm_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0
    elapsed_s: float = 0.0
    probe_calls: int = 0
    probe_seconds: float = 0.0
    #: token 里**是否掺了估算值**（mock / replay 两态没有实测计数）。
    #: 为 True 时这个数不许当实测指标往报告里写（铁律 6）。
    tokens_estimated: bool = False


class BudgetLedger:
    """单次请求的预算账本。**一个 trace 一本，不跨请求复用。**"""

    def __init__(
        self,
        limits: BudgetLimits | None = None,
        probe_limits: ProbeBudgetLimits | None = None,
        *,
        trace_id: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limits = limits or BudgetLimits()
        self.probe_limits = probe_limits or ProbeBudgetLimits()
        self.trace_id = trace_id
        self._clock = clock
        self._start = clock()
        self._llm_calls = 0
        self._tool_calls = 0
        self._tokens = 0
        self._probe_calls = 0
        self._probe_seconds = 0.0
        self._estimated_charges = 0

    # ── 读 ───────────────────────────────────────────────────────────
    @property
    def elapsed_s(self) -> float:
        return self._clock() - self._start

    def usage(self) -> BudgetUsage:
        return BudgetUsage(
            llm_calls=self._llm_calls,
            tool_calls=self._tool_calls,
            tokens=self._tokens,
            elapsed_s=round(self.elapsed_s, 6),
            probe_calls=self._probe_calls,
            probe_seconds=round(self._probe_seconds, 6),
            tokens_estimated=self._estimated_charges > 0,
        )

    # ── 闸门 ─────────────────────────────────────────────────────────
    #
    # 三个闸刻意分开，**不是一个 check() 走天下**：
    # LLM 额度用满了不该拦住「把已经拿到的那次响应里的工具跑完」——那次 LLM 调用
    # 已经花掉了，拦住工具只会丢掉已完成的工作，还会把熔断项报成 `llm_calls`，
    # 让排查的人去查一个根本没超的量。所以按调用类型各查各的额度，
    # 墙钟与 token 这两项是全局的，两边都查。
    def check(self, stage: str = "") -> None:
        """全局闸：墙钟与 token。任何一项越线即抛 FTS-4003。"""
        if self.elapsed_s > self.limits.wall_clock_s:
            self._raise("wall_clock_s", round(self.elapsed_s, 3), self.limits.wall_clock_s, stage)
        if self._tokens >= self.limits.max_tokens:
            self._raise("tokens", self._tokens, self.limits.max_tokens, stage)

    def check_llm(self, projected_tokens: int = 0, stage: str = "") -> None:
        """LLM 调用前的闸：额度 + 把**本次预计消耗**也算进去的 token。"""
        self.check(stage)
        if self._llm_calls >= self.limits.max_llm_calls:
            self._raise("llm_calls", self._llm_calls, self.limits.max_llm_calls, stage or "llm")
        if self._tokens + projected_tokens > self.limits.max_tokens:
            self._raise(
                "tokens", self._tokens + projected_tokens, self.limits.max_tokens, stage or "llm"
            )

    def check_tool(self, tool: str, budget_pool: str = "default") -> None:
        """工具调用前的闸。探针另外走独立池。"""
        self.check(stage=f"tool:{tool}")
        if self._tool_calls >= self.limits.max_tool_calls:
            self._raise("tool_calls", self._tool_calls, self.limits.max_tool_calls, f"tool:{tool}")
        if budget_pool == "probe":
            if self._probe_calls >= self.probe_limits.max_calls:
                self._raise(
                    "probe_calls", self._probe_calls, self.probe_limits.max_calls, f"tool:{tool}"
                )
            if self._probe_seconds >= self.probe_limits.total_s:
                self._raise(
                    "probe_seconds",
                    round(self._probe_seconds, 3),
                    self.probe_limits.total_s,
                    f"tool:{tool}",
                )

    # ── 记账 ─────────────────────────────────────────────────────────
    def charge_llm(self, tokens: int, *, measured: bool) -> None:
        """记一次 LLM 调用。

        `measured=True` 表示 token 数来自模型返回的实测计数（`OllamaProvider`
        的 `prompt_eval_count` / `eval_count`）；`False` 表示是估算
        （mock / replay 两态）。**这个标志一路传到 `BudgetUsage`**，
        免得报告里把估算数当实测数写（铁律 6）。
        """
        self._llm_calls += 1
        self._tokens += max(tokens, 0)
        if not measured:
            self._estimated_charges += 1

    def charge_tool(self, *, budget_pool: str = "default", seconds: float = 0.0) -> None:
        """记一次工具调用。探针另计入独立池（§3.9.2）。"""
        self._tool_calls += 1
        if budget_pool == "probe":
            self._probe_calls += 1
            self._probe_seconds += max(seconds, 0.0)

    # ── 内部 ─────────────────────────────────────────────────────────
    def _raise(self, item: str, actual: float | int, limit: float | int, stage: str) -> None:
        raise BudgetExceededError(
            f"单请求预算超限：{item} 已达 {actual}，上限 {limit}",
            details={
                "item": item,
                "actual": actual,
                "limit": limit,
                "stage": stage,
                "usage": self.usage().model_dump(mode="json"),
                "trace_id": self.trace_id,
            },
            suggestions=[
                "缩小请求范围（如只排某几名学员、只改某一天）后重试",
                "已完成的部分已返回，排班能力本身不受影响（求解链路不经 LLM）",
            ],
        )


__all__ = [
    "SPEC_MAX_LLM_CALLS",
    "SPEC_MAX_TOKENS",
    "SPEC_MAX_TOOL_CALLS",
    "SPEC_WALL_CLOCK_S",
    "BudgetLedger",
    "BudgetLimits",
    "BudgetUsage",
    "ProbeBudgetLimits",
]
