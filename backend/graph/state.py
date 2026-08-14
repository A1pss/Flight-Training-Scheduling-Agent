"""黑板状态 `FTSState`（v6 §7.4，字段逐条对齐）。

## 为什么 `trace_events` 与 `errors` 必须带 `add` reducer

图里同一个 super-step 可以有多个节点写状态。默认的 reducer 是**后写覆盖**，
于是「求解节点记了一条 solver_stats，校验节点同一步记了一条 constraint_check」
会只剩后者——过程回放（v6 §8.2）从此是残缺的，而残缺的回放看起来和完整的
一模一样，没人会发现。`add` 让两条都留下（v6 §7.4 末句）。

反过来说，**其余字段刻意不加 reducer**：`solution` / `validation` 这些就该是
「最新一次的结果」，累加它们只会让下游分不清哪个是当前值。

## 为什么是 `total=False`

LangGraph 的节点返回的是**局部更新**（只带自己改的那几个键）。`total=True`
的 TypedDict 下，`{"intent": "schedule"}` 这种返回值在 mypy --strict 下不合法。
`total=False` 让局部更新自然成立；作为代价，读取时不能假设键一定在，
所以本模块配套给出 :func:`initial_state`（建一份键齐全的初值）与一组
`get_*` 读取器（带默认值，不抛 KeyError）。

## 与 `MessagesState` 的关系

`FTSState` 继承 `MessagesState`，因而自带 `messages: Annotated[list, add_messages]`。
路由节点读 `state["messages"][-1]` 拿用户这句话（v6 §7.5）。
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, TypeVar, cast

from langgraph.graph import MessagesState
from pydantic import BaseModel

from backend.schemas.common import ErrorItem, HumanDecision, TraceEvent
from backend.schemas.intent import (
    ConstraintSpec,
    IncrementalConstraint,
    Intent,
    QueryRequest,
    SchedulingRequest,
    SolveIntent,
    UserRole,
)
from backend.schemas.plan import BlockedItem, SchedulePlan
from backend.schemas.retrieval import GroundingReport
from backend.schemas.solver import ConflictItem, RelaxationProposal, SolverStats
from backend.schemas.validation import SchemaCheckReport, ValidationReport

_T = TypeVar("_T")
_M = TypeVar("_M", bound=BaseModel)


class FTSState(MessagesState, total=False):
    """v6 §7.4 的黑板。字段顺序与设计方案一致，便于逐行核对。"""

    # ── 身份 ──────────────────────────────────────────────────────────
    trace_id: str
    tenant_id: str
    user_id: str

    # ── 意图 ──────────────────────────────────────────────────────────
    intent: Intent | None
    request: SchedulingRequest | QueryRequest | None
    intent_confidence: float

    # ── 版本锚点（可复现性的三件套，铁律 9）──────────────────────────
    snapshot_id: str | None
    ruleset_version: str | None
    semantics_version: str | None

    # ── Planner ───────────────────────────────────────────────────────
    solve_intent: SolveIntent | None
    revision_stack: list[IncrementalConstraint]
    revision_round: int
    needs_clarification: bool
    user_role: UserRole

    # ── 求解 ──────────────────────────────────────────────────────────
    constraint_spec: ConstraintSpec | None
    relaxation_tier: int
    solve_attempts: int
    solution: SchedulePlan | None
    solver_stats: SolverStats | None

    # ── 校验 ──────────────────────────────────────────────────────────
    validation: ValidationReport | None
    schema_check: SchemaCheckReport | None

    # ── 诊断 ──────────────────────────────────────────────────────────
    conflict_set: list[ConflictItem]
    relaxation_proposals: list[RelaxationProposal]
    blocked_items: list[BlockedItem]

    # ── 输出 ──────────────────────────────────────────────────────────
    workbook_path: str | None
    explanation: str | None
    grounding_report: GroundingReport | None

    # ── 过程与人工 ────────────────────────────────────────────────────
    #: 只增不改（v6 §7.4 末句）——多个组件并发写入时不相互覆盖
    trace_events: Annotated[list[TraceEvent], add]
    errors: Annotated[list[ErrorItem], add]
    needs_human: bool
    human_decision: HumanDecision | None

    # ── 本窗口新增（v6 §7.2.1 / §7.3.3 的落地所需，非 §7.4 原表）──────
    #: 实体消解出的歧义，非空即触发反问（「何超 / 高超」同时命中，不自行选择）
    ambiguities: list[dict[str, Any]]
    #: 排班周起点（周一）。`compile_spec` 的必需输入，缺则 FTS-1004
    week_start: str | None
    #: 已归档计划的 ID，`commit_plan_node` 写入
    committed_plan_id: str | None


#: `initial_state` 里那些「空容器」字段，集中一处避免漏初始化。
_EMPTY_LISTS: tuple[str, ...] = (
    "revision_stack",
    "conflict_set",
    "relaxation_proposals",
    "blocked_items",
    "trace_events",
    "errors",
    "ambiguities",
)

_NULLABLE: tuple[str, ...] = (
    "intent",
    "request",
    "snapshot_id",
    "ruleset_version",
    "semantics_version",
    "solve_intent",
    "constraint_spec",
    "solution",
    "solver_stats",
    "validation",
    "schema_check",
    "workbook_path",
    "explanation",
    "grounding_report",
    "human_decision",
    "week_start",
    "committed_plan_id",
)


def initial_state(
    *,
    trace_id: str,
    user_id: str,
    tenant_id: str = "default",
    user_role: UserRole = "scheduler",
    messages: list[Any] | None = None,
    snapshot_id: str | None = None,
    week_start: str | None = None,
) -> FTSState:
    """建一份键齐全的初值。

    **键齐全是刻意的**：`total=False` 让局部更新合法，但也让「某个节点忘了写
    某个键」变成运行时的 `KeyError`。一次性铺平初值后，读取路径上只剩
    「值是不是 None」这一种情况要判，不再有「键在不在」。
    """
    state: dict[str, Any] = {
        "messages": list(messages or []),
        "trace_id": trace_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "intent_confidence": 0.0,
        "revision_round": 0,
        "needs_clarification": False,
        "user_role": user_role,
        "relaxation_tier": 0,
        "solve_attempts": 0,
        "needs_human": False,
    }
    for key in _EMPTY_LISTS:
        state[key] = []
    for key in _NULLABLE:
        state[key] = None
    state["snapshot_id"] = snapshot_id
    state["week_start"] = week_start
    return cast(FTSState, state)


def get(state: FTSState, key: str, default: _T) -> _T:
    """读一个可能缺席的键。

    比 `state.get(key, default)` 多做一件事：**把 `None` 也当缺席**。
    `initial_state` 把可空字段铺成 `None`，而调用方要的往往是「给我一个能直接
    用的值」；不折叠 `None` 的话每个调用点都要写一遍 `or default`，迟早漏一处。
    """
    value = cast(dict[str, Any], state).get(key)
    return default if value is None else cast(_T, value)


def model_get(state: FTSState, key: str, model: type[_M]) -> _M | None:
    """读一个 Pydantic 字段，**并把 checkpoint 往返造成的半吊子对象补回来**。

    ## 这不是防御性编程，是一个实测踩到的坑

    LangGraph 的 msgpack 序列化把 Pydantic 模型存成
    `(module, name, model_dump())`，反序列化时先试 `cls(**kwargs)`，**失败了才
    退到 `cls.model_construct(**kwargs)`**。而 `model_construct` 不做校验——
    嵌套字段原样留成 `dict`。

    什么情况下 `cls(**kwargs)` 会失败？**带 computed field 且 `extra="forbid"`
    的模型**：`model_dump()` 会把 `all_passed` / `total_checked_items` 这类计算
    字段一并吐出来，回构时它们成了「多余的字段」，`extra="forbid"` 当场拒绝。
    本仓库里 `ValidationReport` 与 `GroundingReport` 正是这一类。

    表现出来是这样的：

    ```
    AttributeError: 'dict' object has no attribute 'passed'
      backend/schemas/validation.py:71  all(r.passed for r in self.results)
    ```

    —— 图跑到 `human_gate` 才炸，而 `solve` / `validate` 一路都是绿的。

    所以**凡是从黑板上读 Pydantic 对象，一律走这个函数**：它拿 `__dict__` 里的
    原始字段重新校验一次，半吊子对象补成完整对象，本来就完整的对象照样通过。
    代价是每次读多一次校验（微秒级，相对一次求解可以忽略）。
    """
    raw = cast(dict[str, Any], state).get(key)
    return _coerce(raw, model)


def model_list(state: FTSState, key: str, model: type[_M]) -> list[_M]:
    """同 :func:`model_get`，但读的是一个列表字段。"""
    raw = cast(dict[str, Any], state).get(key) or []
    out: list[_M] = []
    for item in raw:
        coerced = _coerce(item, model)
        if coerced is not None:
            out.append(coerced)
    return out


def _coerce(raw: Any, model: type[_M]) -> _M | None:
    if raw is None:
        return None
    if isinstance(raw, BaseModel):
        return model.model_validate(dict(raw.__dict__))
    if isinstance(raw, dict):
        return model.model_validate(raw)
    raise TypeError(f"黑板上的值既不是 {model.__name__} 也不是 dict：{type(raw).__name__}")


def user_utterance(state: FTSState) -> str:
    """取用户最后一句话（v6 §7.5 `state.messages[-1].content`）。

    消息既可能是 LangChain 的 `BaseMessage`，也可能是 `{"role","content"}`
    字典（API 层直接透传时）。两种都认，取不到就返回空串——**不猜**。
    """
    messages = cast(dict[str, Any], state).get("messages") or []
    for message in reversed(messages):
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return ""


__all__ = [
    "FTSState",
    "get",
    "initial_state",
    "model_get",
    "model_list",
    "user_utterance",
]
