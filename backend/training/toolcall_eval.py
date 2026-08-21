"""§12.5.1 的工具调用契约通过率实测（M7 第一步）。

## 这个模块测什么

`datasets/tool_calls_200`（260 条 = 200 valid + 30 越权 + 30 超预算）×
三种提示词配置（`prompt_configs.py`）× N 轮，产出：

| 指标 | 口径 | 来源 |
|---|---|---|
| **一次通过率** | 调用级 | 首次输出即过 Pydantic 契约（§12.5.1 定义，**准入门禁**） |
| 最终通过率 | 调用级 | ≤2 次重试后通过 |
| 平均重试系数 | 调用级 | 每次工具调用实际发出的 LLM 请求数 |
| 降级触发率 | 请求级 | 重试耗尽转人工表单（FTS-4002） |
| **失败模式分布** | 调用级 | 五类归因 —— §15.2 ⑥ 难负例挖掘的**直接输入** |
| 工具选择正确率 | 调用级 | 诊断指标，**不作门禁** |
| 参数精确匹配率 | 调用级 | 诊断指标，**不作门禁** |
| 越权拦截率 / 预算熔断正确率 | — | 确定性，不调 LLM |

## 三条实现口径

**① 走真 Harness，不另写一套循环。** 契约校验、失败归因、回灌重试、ACL 三层
拦截、预算闸全部是生产代码（`backend/harness/`）。另写一套「评测用的简化循环」
测出来的是那套简化循环，不是上线的东西。

**② 工具表给该组件 ACL 行的全集，不是只给目标工具。** 只给一个工具等于把
「选哪个工具」这一步免掉，一次通过率会虚高，而生产里模型面对的就是全集。

**③ 工具执行接的是桩。** 本模块测的是**契约**，不是工具实现。桩固定返回
`{"stub": true}`，让 `_execute` 走完整条路（ACL 二次核对 + 预算闸 + 缓存），
但不碰库、不碰求解器。**没有桩的话 `ToolNotBoundError` 会从执行器里抛出来，
把一次「模型答对了」记成崩溃。**

## 断点续跑

每条结果**立刻**追加进 JSONL。200 条 × 3 配置 × 3 轮在降级显存下要两三个小时，
中途 Ollama 掉一次就得从头跑是不可接受的。重跑时已在文件里的 `(config, round,
item_id)` 直接跳过 —— 所以中断后原样再跑一次命令即可续上。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from backend.core.errors import LLMSchemaError, LLMUnavailableError, ToolPermissionDeniedError
from backend.core.logging import get_logger
from backend.datasets.entities import AIRCRAFT, MISSIONS, PERSONS, RUNWAYS
from backend.datasets.loader import load_eval_dataset
from backend.harness.acl import DEFAULT_ACL
from backend.harness.budget import BudgetLedger, BudgetLimits, ProbeBudgetLimits
from backend.harness.context import ContextBlock
from backend.harness.harness import Harness
from backend.harness.prompts import PromptRegistry
from backend.harness.registry import ToolRegistry
from backend.harness.tools import TOOL_CATALOG
from backend.harness.types import AgentSpec, ComponentName
from backend.harness.validation import StaticEntityIndex
from backend.llm.mock import MockProvider, tool_response
from backend.llm.provider import LLMProvider, build_provider
from backend.solver.diagnose import ProbeBudget
from backend.training.prompt_configs import PromptConfigName, registry_for
from backend.training.rendering import RenderingName, params_match, render_item

_log = get_logger(__name__)

#: 数据集名与版本。
DATASET: Final[str] = "tool_calls_200"

#: 工具执行桩的固定返回。**不参与任何指标** —— 本模块测契约，不测工具实现。
STUB_RESULT: Final[dict[str, bool]] = {"stub": True}


def _entity_index() -> StaticEntityIndex:
    """基准实体索引 —— `entity_hallucination` 的判据（§12.5.1 硬地板的观测口）。

    没有这份索引，`_check_entities` 会整段跳过（`known()` 返回空集即「这一类没有
    索引」），于是「格式合法但库里没这个人」的编造**一条都统计不到**，
    而那正是硬地板 x 的主战场。
    """
    return StaticEntityIndex(
        {
            "person": tuple(PERSONS),
            "aircraft": tuple(AIRCRAFT),
            "mission": tuple(MISSIONS),
            "runway": tuple(RUNWAYS),
        }
    )


def _stub_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_many({name: lambda _args: STUB_RESULT for name in TOOL_CATALOG})
    return registry


class ToolCallOutcome(BaseModel):
    """一条场景在一种配置、一轮下的结果。**只记事实，不算指标。**"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: str
    #: 场景渲染口径（`task` = 口径 A / `context` = 口径 B）。**默认 `task` 是为了
    #: 让口径 B 引入之前写下的结果文件照常读得回来** —— 那些行里没有这个字段。
    rendering: RenderingName = "task"
    round_index: int = Field(ge=0)
    item_id: str
    stratum: str
    component: str
    tool: str

    #: §12.5.1 的一次通过率分子
    first_pass: bool = False
    final_pass: bool = False
    degraded: bool = False
    llm_calls: int = 0
    attempts: int = 0
    wall_s: float = 0.0

    #: 模型这次点名的工具（可能不止一个）
    chosen_tools: tuple[str, ...] = ()
    tool_correct: bool = False
    params_exact: bool = False

    #: 失败模式：首次尝试的（进分布表）与全程的
    first_failure_modes: tuple[str, ...] = ()
    all_failure_modes: tuple[str, ...] = ()

    #: **模型点了本组件 ACL 行之外的工具**，被调用期 `assert_allowed` 拦下。
    #: 口径 B 下才会出现（口径 A 的任务陈述把该调哪个工具挑明了）。
    #: 它算一次「首次未通过」，且**不进五类失败模式** —— 那五类是**契约**失败，
    #: 越权是权限失败，两件事混一起 §15.2 ⑥ 会照着一张错的分布表去挖难负例。
    acl_attempt: bool = False

    #: 确定性两层的判定结果
    error_code: str = ""
    expected_error_code: str | None = None
    intercepted: bool = False

    #: 本条消耗的**实测** token 总数（Ollama 的 `prompt_eval_count + eval_count`
    #: 之和，含重试）。`mock` 态下账本走估算，此处为估算值 —— 报告里只取
    #: `ollama` 态的数（铁律 6）。提示词侧的门禁数由 `measure_prompt_tokens()` 单独测。
    measured_tokens: int = 0
    #: 运行时异常（Ollama 掉线之类）。非空即该条未跑成，**不计入分母**
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def run_valid_item(
    item: Mapping[str, Any],
    *,
    prompts: PromptRegistry,
    config: PromptConfigName,
    round_index: int,
    registry: ToolRegistry,
    provider: LLMProvider,
    rendering: RenderingName = "task",
) -> ToolCallOutcome:
    """跑一条 `valid` 场景（唯一一层要调 LLM 的）。"""
    component: ComponentName = item["component"]
    expected_tool = str(item["tool"])
    tools = tuple(sorted(DEFAULT_ACL.allowed_tools(component)))
    agent = AgentSpec(name=component, tools=tools, requires_tool_call=True)

    harness = Harness(
        provider,
        registry=registry,
        prompts=prompts,
        entity_index=_entity_index(),
        snapshot_id="m7-toolcall-eval",
    )
    block = ContextBlock(
        kind="history", role="user", content=render_item(item, rendering), label="task"
    )

    started = time.monotonic()
    try:
        output = harness.call(agent, [block])
    except ToolPermissionDeniedError as exc:
        # ★ **模型自己点了本组件 ACL 行之外的工具**（口径 B 实测：Diagnosis 去调
        #   `resolve_person`）。Harness 按 §7.7.2 直接抛是对的，但对评测来说这是一个
        #   **要计数的模型行为**，不是崩溃理由：它算一次「首次未通过」，同时是
        #   §12.5.1「越权拦截率 100%」的一个真实样本。
        #
        #   ⚠️ 别把它与「数据集自己越权」混起来 —— 后者（条目的 (组件, 工具) 落在
        #   ACL 补集里）由 `ToolCallItem._consistency` 在**加载期**就挡住了，
        #   走不到这里。本窗口一开始把两者混为一谈，于是口径 B 跑到第 89 条崩了。
        return ToolCallOutcome(
            config=config,
            rendering=rendering,
            round_index=round_index,
            item_id=str(item["item_id"]),
            stratum=str(item["stratum"]),
            component=component,
            tool=expected_tool,
            wall_s=round(time.monotonic() - started, 3),
            acl_attempt=True,
            error_code=exc.code.value,
        )
    except (LLMUnavailableError, LLMSchemaError) as exc:
        # 只吞「真机侧不可用」—— 这类是运维事故，不进指标的分母。
        return ToolCallOutcome(
            config=config,
            rendering=rendering,
            round_index=round_index,
            item_id=str(item["item_id"]),
            stratum=str(item["stratum"]),
            component=component,
            tool=expected_tool,
            wall_s=round(time.monotonic() - started, 3),
            error=f"{type(exc).__name__}: {exc}",
        )
    elapsed = time.monotonic() - started

    chosen = tuple(c.name for c in output.calls)
    tool_correct = chosen == (expected_tool,)
    exact = tool_correct and params_match(
        item.get("expected_params") or {}, output.calls[0].arguments
    )

    return ToolCallOutcome(
        config=config,
        rendering=rendering,
        round_index=round_index,
        item_id=str(item["item_id"]),
        stratum=str(item["stratum"]),
        component=component,
        tool=expected_tool,
        first_pass=output.first_pass,
        final_pass=bool(output.calls) and not output.degraded,
        degraded=output.degraded,
        llm_calls=output.llm_calls,
        attempts=len(output.attempts),
        wall_s=round(elapsed, 3),
        chosen_tools=chosen,
        tool_correct=tool_correct,
        params_exact=exact,
        first_failure_modes=(
            tuple(f.mode.value for f in output.attempts[0].failures) if output.attempts else ()
        ),
        all_failure_modes=tuple(f.mode.value for a in output.attempts for f in a.failures),
        error_code=output.error_code,
        measured_tokens=harness.usage().tokens,
    )


def run_acl_item(
    item: Mapping[str, Any],
    *,
    config: PromptConfigName,
    round_index: int,
    rendering: RenderingName = "task",
) -> ToolCallOutcome:
    """越权层：**故障注入，不调 LLM**（§12.5.1「用故障注入构造」）。

    判据是「调用期 `assert_allowed` 抛不抛」。装配期与注册期也各有一道闸，
    但 §7.7.2 要的是**运行时**拦截 —— 前两道都可能被将来的重构绕开。
    """
    component: ComponentName = item["component"]
    tool = str(item["tool"])
    code = ""
    intercepted = False
    try:
        DEFAULT_ACL.assert_allowed(component, tool)
    except ToolPermissionDeniedError as exc:
        intercepted = True
        code = exc.code.value
    return ToolCallOutcome(
        config=config,
        rendering=rendering,
        round_index=round_index,
        item_id=str(item["item_id"]),
        stratum=str(item["stratum"]),
        component=component,
        tool=tool,
        intercepted=intercepted,
        error_code=code,
        expected_error_code=item.get("expected_error_code"),
    )


def run_budget_item(
    item: Mapping[str, Any],
    *,
    prompts: PromptRegistry,
    config: PromptConfigName,
    round_index: int,
    registry: ToolRegistry,
    rendering: RenderingName = "task",
) -> ToolCallOutcome:
    """超预算层：**故障注入，不调真模型**。两个池两种行为（§3.9.2）。

    - **Harness 预算池**（24 条）：`max_tool_calls` 预扣满，再走一次完整的
      `Harness.call` —— 模型侧用 `MockProvider` 的场景桩直接吐出那次工具调用
      （零真机调用、确定性），契约过了之后 `_execute` 的闸抛
      `BudgetExceededError`，组件返回带 `FTS-4003` 的 `AgentOutput`。
      **走公开 API**：私自去调 `_execute` 测出来的是我挑的那一段，不是生产路径。
    - **探针独立预算池**（6 条）：`expected_error_code` 是 `None` —— 池子空了
      **不抛**，`agents/diagnosis.py::run_probe` 优雅返回 `BUDGET_EXHAUSTED` 载荷。
      这里判的是 `ProbeBudget` 的耗尽契约本身（`is_exhausted()` 为真、
      `next_limit()` 归零、**且全程不抛**）。端到端那条载荷路径要一份完整的
      `SpecBundle`（等于先跑一次 `compile_spec`），不在本 runner 的范围内 ——
      已写进收工报告的「已知限制」。
    """
    component: ComponentName = item["component"]
    tool = str(item["tool"])
    expected = item.get("expected_error_code")

    def result(*, intercepted: bool, error_code: str = "", llm_calls: int = 0) -> ToolCallOutcome:
        return ToolCallOutcome(
            config=config,
            rendering=rendering,
            round_index=round_index,
            item_id=str(item["item_id"]),
            stratum=str(item["stratum"]),
            component=component,
            tool=tool,
            expected_error_code=expected,
            intercepted=intercepted,
            error_code=error_code,
            llm_calls=llm_calls,
        )

    if expected is None:
        budget = ProbeBudget(per_call_s=30.0, max_calls=0, total_s=120.0, calls=0, spent_s=120.0)
        return result(intercepted=budget.is_exhausted() and budget.next_limit() == 0.0)

    limits = BudgetLimits.from_settings()
    ledger = BudgetLedger(limits, ProbeBudgetLimits.from_settings(), trace_id="m7-budget")
    for _ in range(limits.max_tool_calls):
        ledger.charge_tool()

    scripted = MockProvider()
    scripted.register_scenario(
        "budget", [tool_response(tool, dict(item.get("expected_params") or {}))]
    )
    scripted.activate("budget")

    harness = Harness(
        scripted,
        registry=registry,
        prompts=prompts,
        ledger=ledger,
        entity_index=_entity_index(),
        snapshot_id="m7-toolcall-eval",
    )
    agent = AgentSpec(name=component, tools=(tool,), requires_tool_call=True)
    output = harness.call(agent, [ContextBlock(kind="history", role="user", content=tool)])
    return result(
        intercepted=output.error_code == expected,
        error_code=output.error_code,
        llm_calls=output.llm_calls,
    )


# ─────────────────────────────────────────────────────────────────────
# 批跑
# ─────────────────────────────────────────────────────────────────────


def _done_keys(path: Path) -> set[tuple[str, str, int, str]]:
    """已跑完的 `(config, rendering, round, item_id)`，用于断点续跑。

    `rendering` 缺省成 `task`：口径 B 引入之前写下的行没有这个字段，
    补一个默认值就能与新行落在同一个键空间里，**不用重跑已经花掉的那两小时**。
    """
    if not path.exists():
        return set()
    done: set[tuple[str, str, int, str]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        done.add(
            (
                str(row["config"]),
                str(row.get("rendering", "task")),
                int(row["round_index"]),
                str(row["item_id"]),
            )
        )
    return done


def run_config(
    config: PromptConfigName,
    *,
    out_path: Path,
    rounds: int = 3,
    limit: int | None = None,
    strata: Sequence[str] = ("valid", "acl_violation", "budget_exhaustion"),
    rendering: RenderingName = "task",
) -> int:
    """跑一种配置的全部轮次，逐条落盘。返回新写入的条数。"""
    _manifest, items = load_eval_dataset(DATASET, require_approved=True)
    # 条目是 `ToolCallItem`（Pydantic），转成 dict 再用 —— 本模块的三个 runner
    # 都按映射取字段，这样单测可以直接喂手工构造的 dict，不必造整个契约对象。
    rows = [i.model_dump(mode="json") for i in items]
    rows = [r for r in rows if r["stratum"] in strata]
    if limit is not None:
        rows = rows[:limit]

    prompts = registry_for(config)
    registry = _stub_registry()
    provider = build_provider()
    done = _done_keys(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with out_path.open("a", encoding="utf-8") as fh:
        for round_index in range(rounds):
            for item in rows:
                key = (config, rendering, round_index, str(item["item_id"]))
                if key in done:
                    continue
                outcome = _dispatch(
                    item,
                    prompts=prompts,
                    config=config,
                    round_index=round_index,
                    registry=registry,
                    provider=provider,
                    rendering=rendering,
                )
                fh.write(outcome.model_dump_json() + "\n")
                fh.flush()
                written += 1
                if written % 20 == 0:
                    _log.info(
                        "toolcall_eval_progress",
                        config=config,
                        rendering=rendering,
                        round=round_index,
                        written=written,
                    )
    return written


def _dispatch(
    item: Mapping[str, Any],
    *,
    prompts: PromptRegistry,
    config: PromptConfigName,
    round_index: int,
    registry: ToolRegistry,
    provider: LLMProvider,
    rendering: RenderingName,
) -> ToolCallOutcome:
    stratum = str(item["stratum"])
    if stratum == "valid":
        return run_valid_item(
            item,
            prompts=prompts,
            config=config,
            round_index=round_index,
            registry=registry,
            provider=provider,
            rendering=rendering,
        )
    if stratum == "acl_violation":
        return run_acl_item(item, config=config, round_index=round_index, rendering=rendering)
    return run_budget_item(
        item,
        prompts=prompts,
        config=config,
        round_index=round_index,
        registry=registry,
        rendering=rendering,
    )


def load_outcomes(path: Path) -> tuple[ToolCallOutcome, ...]:
    """读回结果文件。"""
    if not path.exists():
        return ()
    return tuple(
        ToolCallOutcome.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def iter_outcomes(paths: Sequence[Path]) -> Iterator[ToolCallOutcome]:
    for path in paths:
        yield from load_outcomes(path)


__all__ = [
    "DATASET",
    "STUB_RESULT",
    "ToolCallOutcome",
    "iter_outcomes",
    "load_outcomes",
    "run_acl_item",
    "run_budget_item",
    "run_config",
    "run_valid_item",
]
