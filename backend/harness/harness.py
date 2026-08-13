"""Harness 本体：把 v6 §7.7.1 的八项职责串成一次调用（§7.7 伪码的落地）。

```
acl.assert_exposable（装配期）→ ctx.assemble → budget.check_llm
  → mode_selector.pick → provider.chat → recorder.log_llm
  → acl 预检（逐个 tool call）→ parse_and_validate
        └─失败─→ 回灌错误 + 统计失败模式 + 重试（≤2）→ 仍失败则 FTS-4002 降级
  → acl.assert_allowed（执行前再核一次）→ budget.check_tool
  → cache.get_or_exec → recorder.log_tool
  → AgentOutput
```

八项职责与本模块的对应关系：

| # | 职责 | 落点 |
|---|---|---|
| 1 | 工具契约校验 | `validation.ToolCallValidator` + `_on_failures`（回灌） |
| 2 | 双模式调用 | `mode_selector.ModeSelector` + `_pick_mode` / `_build_request` |
| 3 | 权限矩阵强制 | `acl.ToolACL`（装配期 `assert_exposable` + 校验前 `_precheck_acl` + 执行前 `assert_allowed`） |
| 4 | 预算控制 | `budget.BudgetLedger`（LLM 前、工具前各一闸） |
| 5 | 上下文装配 | `context.ContextAssembler` |
| 6 | 结果缓存 | `cache.ToolResultCache` |
| 7 | 录制与重放 | `recorder.TraceRecorder` / `recorder.replay` |
| 8 | Prompt 版本治理 | `prompts.PromptRegistry`，版本随每条 LLM 事件落 trace |

**三种异常出口，处置刻意不同**：

- **契约失败** → 回灌重试 ≤2 次，仍失败则 FTS-4002 转人工表单（`degraded=True`）。
  模型犯错是日常，不是事故。
- **预算超限** → 不抛给调用方，返回带 `FTS-4003` 的 `AgentOutput`，**已完成的
  工具结果照常带回**（v6 §9.3：中断并返回已完成部分）。
- **越权** → **直接抛**。它不是「这次没答对」，是有人试图绕过架构禁令；
  吞掉它等于把 §12.5.1 的「越权拦截率 100%」变成一句空话。
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.core.config import Settings, get_settings
from backend.core.errors import (
    BudgetExceededError,
    ErrorCode,
    ToolPermissionDeniedError,
)
from backend.core.logging import get_logger, new_trace_id
from backend.harness.acl import DEFAULT_ACL, FORBIDDEN_NODES, ToolACL
from backend.harness.budget import BudgetLedger, BudgetLimits, BudgetUsage, ProbeBudgetLimits
from backend.harness.cache import ToolResultCache
from backend.harness.context import AssembledContext, ContextAssembler, ContextBlock
from backend.harness.mode_selector import ModeSelector
from backend.harness.prompts import PromptRegistry
from backend.harness.recorder import ToolReplayer, Trace, TraceRecorder
from backend.harness.registry import ToolRegistry
from backend.harness.tokens import estimate_messages, estimate_tokens
from backend.harness.tools import TOOL_CATALOG
from backend.harness.types import (
    AgentOutput,
    AgentSpec,
    AttemptRecord,
    ComponentName,
    FailureMode,
    ToolResult,
    ValidatedCall,
    ValidationFailure,
)
from backend.harness.validation import (
    EntityIndex,
    ToolCallValidator,
    build_error_feedback,
)
from backend.llm.provider import LLMProvider, build_provider
from backend.llm.types import CallMode, LLMRequest, LLMResponse, RawToolCall

_log = get_logger(__name__)


class HarnessStats(BaseModel):
    """跨调用累积的统计口径（v6 §12.5.1 的数据源）。

    **这里只累加事实，不算指标。** 通过率、重试系数、降级率怎么算是 W13 的事，
    本模块只保证分子分母都记全了：一次通过数、总调用数、每次尝试数、失败模式
    分布、降级次数、越权拦截次数。
    """

    model_config = ConfigDict(extra="forbid")

    tool_calls_validated: int = 0
    first_pass: int = 0
    llm_requests: int = 0
    degraded: int = 0
    acl_denials: int = 0
    budget_breaks: int = 0
    failure_modes: dict[str, int] = Field(default_factory=dict)

    def record_failure(self, mode: FailureMode) -> None:
        self.failure_modes[mode.value] = self.failure_modes.get(mode.value, 0) + 1

    @property
    def failure_mode_counter(self) -> Counter[str]:
        return Counter(self.failure_modes)


class Harness:
    """LLM 组件与外界之间的那层壳。"""

    def __init__(
        self,
        provider: LLMProvider | None = None,
        *,
        registry: ToolRegistry | None = None,
        acl: ToolACL | None = None,
        ledger: BudgetLedger | None = None,
        assembler: ContextAssembler | None = None,
        cache: ToolResultCache | None = None,
        recorder: TraceRecorder | None = None,
        mode_selector: ModeSelector | None = None,
        prompts: PromptRegistry | None = None,
        entity_index: EntityIndex | None = None,
        tool_replayer: ToolReplayer | None = None,
        settings: Settings | None = None,
        trace_id: str = "",
        snapshot_id: str = "",
    ) -> None:
        self._cfg = settings or get_settings()
        self.trace_id = trace_id or new_trace_id()
        self.snapshot_id = snapshot_id
        self._provider = provider or build_provider(self._cfg)
        self._registry = registry or ToolRegistry()
        self._acl = acl or DEFAULT_ACL
        self._ledger = ledger or BudgetLedger(
            BudgetLimits.from_settings(self._cfg),
            ProbeBudgetLimits.from_settings(self._cfg),
            trace_id=self.trace_id,
        )
        self._ctx = assembler or ContextAssembler(settings=self._cfg)
        self._cache = cache or ToolResultCache(settings=self._cfg)
        self._prompts = prompts or PromptRegistry.load(settings=self._cfg)
        self._recorder = recorder or TraceRecorder(
            self.trace_id,
            provider=self._cfg.LLM_PROVIDER,
            model=self._cfg.LLM_MODEL,
            snapshot_id=snapshot_id,
            prompt_versions=self._prompts.versions(),
        )
        self._modes = mode_selector or ModeSelector.from_settings(self._cfg)
        self._validator = ToolCallValidator(entity_index)
        self._replayer = tool_replayer
        self.stats = HarnessStats()

    # ── 装配 ─────────────────────────────────────────────────────────
    @classmethod
    def for_replay(
        cls,
        *,
        trace: Trace,
        settings: Settings,
        registry: ToolRegistry | None = None,
        entity_index: EntityIndex | None = None,
        replay_tools: bool = True,
    ) -> Harness:
        """构造一个「零 LLM 调用」的重放 Harness（v6 §12.5.2）。

        provider 走 `ReplayProvider`（严格按次序 + 核对指纹），工具走
        `ToolReplayer`。两者都查不到就抛，绝不回退到真机或真库。
        """
        from backend.llm.replay import ReplayProvider

        return cls(
            ReplayProvider(settings),
            registry=registry or ToolRegistry(),
            entity_index=entity_index,
            tool_replayer=ToolReplayer(trace.tool_events) if replay_tools else None,
            settings=settings,
            trace_id=trace.meta.trace_id,
            snapshot_id=trace.meta.snapshot_id,
        )

    # ── 读 ───────────────────────────────────────────────────────────
    @property
    def recorder(self) -> TraceRecorder:
        return self._recorder

    @property
    def modes(self) -> ModeSelector:
        return self._modes

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def cache(self) -> ToolResultCache:
        return self._cache

    def usage(self) -> BudgetUsage:
        return self._ledger.usage()

    # ── 主流程 ───────────────────────────────────────────────────────
    def call(
        self,
        agent: AgentSpec,
        blocks: Sequence[ContextBlock] = (),
        *,
        snapshot_id: str | None = None,
    ) -> AgentOutput:
        """跑一次组件调用。"""
        # ① 装配期权限核对：给模型看的工具表必须是该组件 ACL 行的子集
        self._acl.assert_exposable(agent.name, agent.tools)

        prompt = self._prompts.get(agent.name, agent.prompt_key)
        assembled = self._assemble(agent, blocks, prompt.body)
        messages = [dict(m) for m in assembled.messages]
        snap = self.snapshot_id if snapshot_id is None else snapshot_id

        attempts: list[AttemptRecord] = []
        llm_calls = 0
        mode: CallMode = self._pick_mode(agent)

        # 重试上限取「组件声明」与「全局配置」的**较小值**：v6 §7.7.1 写死 ≤2，
        # 配置只能往严里调（`HARNESS_MAX_RETRIES` 同样有 le=2 上界）。
        max_retries = min(agent.max_retries, self._cfg.HARNESS_MAX_RETRIES)

        for attempt in range(max_retries + 1):
            try:
                self._ledger.check_llm(estimate_messages(messages), stage=f"llm:{agent.name}")
            except BudgetExceededError as exc:
                return self._budget_output(agent, exc, attempts, llm_calls, mode, prompt.versioned)

            mode = self._pick_mode(agent)
            request = self._build_request(agent, messages, mode)
            response = self._invoke(agent, request, mode, prompt.versioned)
            llm_calls += 1

            calls, failures = self._parse_and_validate(agent, response, mode)
            attempts.append(AttemptRecord(attempt=attempt, mode=mode, failures=tuple(failures)))

            if failures:
                self._on_failures(agent, failures, response, messages, attempt)
                continue

            self._modes.report_success(agent.name)
            try:
                results = self._execute(agent, calls, snap)
            except BudgetExceededError as exc:
                return self._budget_output(
                    agent, exc, attempts, llm_calls, mode, prompt.versioned, calls=calls
                )

            self.stats.tool_calls_validated += len(calls)
            if attempt == 0:
                self.stats.first_pass += len(calls)
            return AgentOutput(
                component=agent.name,
                calls=tuple(calls),
                results=tuple(results),
                text=response.text,
                mode=mode,
                attempts=tuple(attempts),
                llm_calls=llm_calls,
                prompt_version=prompt.versioned,
            )

        return self._degrade(agent, attempts, llm_calls, mode, prompt.versioned)

    # ── 各步 ─────────────────────────────────────────────────────────
    def _assemble(
        self, agent: AgentSpec, blocks: Sequence[ContextBlock], prompt_body: str
    ) -> AssembledContext:
        full = [ContextBlock(kind="system", role="system", content=prompt_body, label=agent.name)]
        full.extend(blocks)
        assembled = self._ctx.assemble(full)
        if assembled.dropped:
            self._recorder.note(
                agent.name,
                "context_trim",
                f"上下文裁剪 {len(assembled.dropped)} 块",
                detail={
                    "dropped": list(assembled.dropped),
                    "tokens": assembled.tokens,
                    "budget": assembled.budget,
                },
            )
        return assembled

    def _pick_mode(self, agent: AgentSpec) -> CallMode:
        """取本次调用的模式。

        **纯生成型组件（不给工具、也不要求工具调用）永远走 `native`**：
        `constrained_json` 是把输出约束成 `{"tool": …, "arguments": …}` 的形状，
        对「写一段给人看的解释」既无意义，工具名的 enum 还会是空数组。
        """
        if not agent.tools and not agent.requires_tool_call:
            return "native"
        return self._modes.pick(agent.name)

    def _build_request(
        self, agent: AgentSpec, messages: list[dict[str, str]], mode: CallMode
    ) -> LLMRequest:
        if mode == "native":
            return LLMRequest(
                messages=list(messages),
                tools=self._registry.schemas_for(agent.tools),
                temperature=self._cfg.LLM_TEMPERATURE,
            )
        # 受约束 JSON 解码：不给 tools，改用 format=<schema> 约束输出形状
        return LLMRequest(
            messages=list(messages),
            format_schema=agent.output_schema or constrained_schema(agent.tools),
            temperature=self._cfg.LLM_TEMPERATURE,
        )

    def _invoke(
        self, agent: AgentSpec, request: LLMRequest, mode: CallMode, prompt_version: str
    ) -> LLMResponse:
        estimated = estimate_messages(request.messages)
        started = time.monotonic()
        response = self._provider.chat(request)
        latency_ms = (time.monotonic() - started) * 1000

        # 记账取实测优先：Ollama 会带回 prompt_eval_count / eval_count；
        # mock 与 replay 没有实测值，退回估算并如实标记（铁律 6）。
        measured = response.total_tokens
        if measured > 0:
            self._ledger.charge_llm(measured, measured=True)
        else:
            self._ledger.charge_llm(estimated + estimate_tokens(response.text), measured=False)

        self.stats.llm_requests += 1
        self._recorder.log_llm(
            agent.name,
            request,
            response,
            prompt_version=prompt_version,
            mode=mode,
            latency_ms=latency_ms,
        )
        return response

    def _parse_and_validate(
        self, agent: AgentSpec, response: LLMResponse, mode: CallMode
    ) -> tuple[list[ValidatedCall], list[ValidationFailure]]:
        raw_calls, parse_failure = extract_tool_calls(response, mode)
        if parse_failure is not None:
            return [], [parse_failure]

        if not raw_calls:
            if agent.requires_tool_call:
                return [], [
                    ValidationFailure(
                        mode=FailureMode.JSON_MALFORMED,
                        expected="至少一个工具调用",
                        actual=_preview(response.text),
                        message="本次要求以工具调用作答，但输出里没有任何工具调用",
                    )
                ]
            return [], []

        calls: list[ValidatedCall] = []
        failures: list[ValidationFailure] = []
        for raw in raw_calls:
            # ★ 越权先于契约。顺序反过来的话，「Planner 想调 solve」会被判成
            # 「工具名不在本次工具表里」这种可重试的契约失败，于是越权既没被抛、
            # 也没被计数，§12.5.1 的「越权拦截率 100%」就成了统计不到的空话。
            self._precheck_acl(agent, raw.name)
            call, failure = self._validator.validate(raw, agent.tools)
            if failure is not None:
                failures.append(failure)
            elif call is not None:
                calls.append(call)
        return calls, failures

    def _precheck_acl(self, agent: AgentSpec, tool: str) -> None:
        """模型点名了一个没给它的工具时，先判是不是越权。

        三种情况分得很清楚：

        - **确定性节点**（`solve` 等六个）→ `ArchitecturalBanError`，直接抛；
        - **权限矩阵不允许**该组件用的工具 → `ToolPermissionDeniedError`，直接抛；
        - **矩阵允许、只是本次没暴露**（比如 Planner 调了一个它有权但这轮没给的
          工具）→ 不抛，交给契约校验判成「工具名不在本次工具表里」并回灌重试。
          这不是越权，是模型记岔了一轮的工具表。
        """
        if tool in agent.tools:
            return
        if tool not in FORBIDDEN_NODES and tool not in TOOL_CATALOG:
            return  # 编出来的名字：属于契约失败，不是越权
        try:
            self._acl.assert_allowed(agent.name, tool)
        except ToolPermissionDeniedError as exc:
            self.stats.acl_denials += 1
            self._recorder.note(
                agent.name, "acl", exc.message, level=exc.severity, detail=exc.details
            )
            _log.error("acl_denied", component=agent.name, tool=tool, detail=exc.details)
            raise

    def _on_failures(
        self,
        agent: AgentSpec,
        failures: Sequence[ValidationFailure],
        response: LLMResponse,
        messages: list[dict[str, str]],
        attempt: int,
    ) -> None:
        for failure in failures:
            self.stats.record_failure(failure.mode)
        self.stats.tool_calls_validated += len(failures)

        before = self._modes.pick(agent.name)
        after = self._modes.report_failure(agent.name)
        self._recorder.note(
            agent.name,
            "contract_failure",
            f"第 {attempt + 1} 次尝试未通过契约校验",
            detail={
                "failures": [f.model_dump(mode="json") for f in failures],
                "modes": [f.mode.value for f in failures],
            },
        )
        if after != before:
            self._recorder.note(
                agent.name,
                "mode_switch",
                f"解析失败率超阈值，调用模式 {before} → {after}",
                level="INFO",
                detail=self._modes.stats(agent.name).model_dump(mode="json"),
            )

        # 回灌：先把模型的原话放回去（让它看见自己写了什么），再给出具体错误
        messages.append({"role": "assistant", "content": _echo(response)})
        messages.append(
            {
                "role": "user",
                "content": build_error_feedback(
                    failures,
                    [s.model_dump(mode="json") for s in self._registry.schemas_for(agent.tools)],
                ),
            }
        )

    def _execute(
        self, agent: AgentSpec, calls: Sequence[ValidatedCall], snapshot_id: str
    ) -> list[ToolResult]:
        results: list[ToolResult] = []
        for call in calls:
            # ② 调用期权限核对 —— 越权即抛，不吞不降级
            try:
                self._acl.assert_allowed(agent.name, call.name)
            except ToolPermissionDeniedError as exc:
                self.stats.acl_denials += 1
                self._recorder.note(
                    agent.name,
                    "acl",
                    exc.message,
                    level=exc.severity,
                    detail=exc.details,
                )
                _log.error("acl_denied", component=agent.name, tool=call.name, detail=exc.details)
                raise

            spec = TOOL_CATALOG[call.name]
            self._ledger.check_tool(call.name, budget_pool=spec.budget_pool)

            started = time.monotonic()
            result = self._run_one(agent.name, spec.name, call.arguments, snapshot_id)
            elapsed = time.monotonic() - started

            self._ledger.charge_tool(budget_pool=spec.budget_pool, seconds=elapsed)
            self._recorder.log_tool(
                agent.name,
                call.name,
                call.arguments,
                ok=result.ok,
                value=result.value,
                error=result.error,
                cached=result.cached,
                latency_ms=elapsed * 1000,
            )
            results.append(result)
        return results

    def _run_one(
        self,
        component: ComponentName,  # noqa: ARG002 —— 保留给将来的按组件路由
        tool: str,
        arguments: dict[str, Any],
        snapshot_id: str,
    ) -> ToolResult:
        if self._replayer is not None:
            event = self._replayer.next_result(tool, arguments)
            return ToolResult(
                tool=tool, ok=event.ok, value=event.value, error=event.error, cached=event.cached
            )

        spec = self._registry.spec(tool)
        handler = self._registry.handler(tool)
        try:
            return self._cache.get_or_exec(spec, arguments, snapshot_id, lambda: handler(arguments))
        except Exception as exc:  # 工具自身失败不该炸掉整次请求
            _log.warning("tool_failed", tool=tool, error=str(exc))
            return ToolResult(tool=tool, ok=False, error=f"{type(exc).__name__}: {exc}")

    # ── 出口 ─────────────────────────────────────────────────────────
    def _degrade(
        self,
        agent: AgentSpec,
        attempts: Sequence[AttemptRecord],
        llm_calls: int,
        mode: CallMode,
        prompt_version: str,
    ) -> AgentOutput:
        """重试耗尽 → FTS-4002 转人工表单（v6 §7.7 伪码 `degrade_to_form`）。"""
        self.stats.degraded += 1
        modes = [f.mode.value for a in attempts for f in a.failures]
        self._recorder.note(
            agent.name,
            "degrade",
            "契约重试耗尽，转人工表单（FTS-4002）",
            level="ERROR",
            detail={"attempts": len(attempts), "failure_modes": modes},
        )
        return AgentOutput(
            component=agent.name,
            mode=mode,
            attempts=tuple(attempts),
            degraded=True,
            error_code=ErrorCode.LLM_SCHEMA_VIOLATION.value,
            error_message="工具调用连续未通过契约校验，已转人工表单；排班能力不受影响",
            llm_calls=llm_calls,
            prompt_version=prompt_version,
        )

    def _budget_output(
        self,
        agent: AgentSpec,
        exc: BudgetExceededError,
        attempts: Sequence[AttemptRecord],
        llm_calls: int,
        mode: CallMode,
        prompt_version: str,
        calls: Sequence[ValidatedCall] = (),
    ) -> AgentOutput:
        """预算熔断 → FTS-4003，带回已完成部分（v6 §9.3）。"""
        self.stats.budget_breaks += 1
        self._recorder.note(
            agent.name,
            "budget",
            exc.message,
            level="ERROR",
            detail=exc.details,
        )
        _log.warning("budget_exceeded", component=agent.name, detail=exc.details)
        return AgentOutput(
            component=agent.name,
            calls=tuple(calls),
            mode=mode,
            attempts=tuple(attempts),
            degraded=True,
            error_code=ErrorCode.HARNESS_BUDGET_EXCEEDED.value,
            error_message=exc.message,
            llm_calls=llm_calls,
            prompt_version=prompt_version,
        )


# ─────────────────────────────────────────────────────────────────────
# 解析
# ─────────────────────────────────────────────────────────────────────


def constrained_schema(tools: Sequence[str]) -> dict[str, Any]:
    """`constrained_json` 模式的输出 schema。

    形状固定为 `{"tool": <枚举>, "arguments": {...}}`——工具名做成 enum，模型就
    编不出不存在的工具名（这一类失败在 14B 上不罕见）。参数的细粒度校验仍由
    Pydantic 在事后做：把 30 个工具的入参 schema 合成一个 oneOf 交给受约束解码，
    实测会显著拖慢生成，收益却与事后校验重叠。
    """
    return {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "enum": list(tools)},
            "arguments": {"type": "object"},
        },
        "required": ["tool", "arguments"],
    }


def extract_tool_calls(
    response: LLMResponse, mode: CallMode
) -> tuple[tuple[RawToolCall, ...], ValidationFailure | None]:
    """从响应里取出工具调用。两种模式两条路径。"""
    if mode == "native":
        return response.tool_calls, None

    text = response.text.strip()
    if not text:
        return (), None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return (), ValidationFailure(
            mode=FailureMode.JSON_MALFORMED,
            expected="受约束解码要求的 JSON 对象",
            actual=_preview(text),
            message=f"输出不是合法 JSON：{exc}",
        )

    items = payload if isinstance(payload, list) else [payload]
    calls: list[RawToolCall] = []
    for item in items:
        if not isinstance(item, dict):
            return (), ValidationFailure(
                mode=FailureMode.JSON_MALFORMED,
                expected='{"tool": ..., "arguments": {...}}',
                actual=_preview(repr(item)),
                message="工具调用必须是 JSON 对象",
            )
        arguments = item.get("arguments", {})
        calls.append(
            RawToolCall(
                name=str(item.get("tool", item.get("name", ""))),
                arguments=arguments
                if isinstance(arguments, (dict, str))
                else json.dumps(arguments),
            )
        )
    return tuple(calls), None


def _echo(response: LLMResponse) -> str:
    """把模型上一轮的输出复述回去，让它看得见自己写了什么。"""
    if response.tool_calls:
        return json.dumps(
            [c.model_dump(mode="json") for c in response.tool_calls],
            ensure_ascii=False,
        )
    return response.text


def _preview(value: str, limit: int = 160) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


__all__ = ["Harness", "HarnessStats", "constrained_schema", "extract_tool_calls"]
