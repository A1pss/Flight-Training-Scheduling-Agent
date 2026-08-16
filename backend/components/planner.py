"""LLM 节点 ②：`Planner` 求解规划器（v6 §7.2.3 / §7.3.3 / §7.3.4）。

**同一个组件的两种调用，计为 1 个**（v6 §7.1.4）：

| 调用 | 何时 | 产物 | 下一跳 |
|---|---|---|---|
| 求解意图规划 | 首轮（`revision_round == 0`） | `SolveIntent` | `compile_spec` 或回 `route` |
| 修订意图翻译 | 人工门禁选了 `REVISE` 之后 | `IncrementalConstraint` 入栈 | `compile_spec` |

两种都是**单次调用、不自主循环、下一跳恒定**——所以它是 LLM 节点，不是 Agent。
修订的轮数由**用户**决定而非模型（v6 §7.1.2）。

## 修订轮的三件事，一件都不能省

1. **翻译**（LLM 优先、规则降级），产物是增量约束而不是架次改动；
2. **回显确认**：`echo` 写进 `state["explanation"]`，UI 先展示「我理解为：……」；
3. **入栈**：`revision_stack` 支持 `undo`，`origin_utterance` 留着原话。

不可行时的回滚**不在这里**，在图的边上（`graph.py`）：翻译完还要走完整的
`solve → validate`，判不可行了才回滚上一版并给 FTS-3005。在翻译阶段就替用户
判断「这条大概不行所以我不加了」，是最不该做的事。
"""

from __future__ import annotations

from typing import Any, cast

from langgraph.types import Command

from backend.core.config import Settings, get_settings
from backend.core.errors import ErrorCode, FTSError
from backend.graph.events import emit, error
from backend.graph.state import FTSState, model_get, model_list, user_utterance
from backend.graph.state import get as state_get
from backend.harness import Harness
from backend.planner.intent import plan_solve_intent
from backend.planner.revision import (
    RevisionStack,
    for_solver,
    translate_revision,
    undo_echo,
    undo_times,
)
from backend.routing.entities import EntityDirectory
from backend.schemas.common import HumanDecision
from backend.schemas.intent import (
    ConstraintSpec,
    IncrementalConstraint,
    QueryRequest,
    SchedulingRequest,
    SolveIntent,
    UserRole,
)
from backend.schemas.plan import SchedulePlan


def planner_node(
    state: FTSState,
    *,
    directory: EntityDirectory | None = None,
    harness: Harness | None = None,
    settings: Settings | None = None,
    window_start: Any = None,
    horizon_minutes: int = 720,
) -> Command[str]:
    """LLM 节点 ②。按 `revision_round` 分流到两种调用之一。"""
    # ★ 用户在回显确认页上按了 REJECT：「你理解错了，这条不要了」。
    #   弹栈撤回，**不重解** —— 方案还是上一版，一个字都没动过。
    if bool(state_get(state, "revision_cancelled", False)):
        return _cancel_pending_revision(state)
    if int(state_get(state, "revision_round", 0)) > 0:
        return _revision_round(
            state,
            directory=directory,
            harness=harness,
            window_start=window_start,
            horizon_minutes=horizon_minutes,
        )
    return _first_round(state, harness=harness, settings=settings)


# ─────────────────────────────────────────────────────────────────────
# 首轮：SolveIntent 生成
# ─────────────────────────────────────────────────────────────────────
def _first_round(
    state: FTSState,
    *,
    harness: Harness | None,
    settings: Settings | None,
) -> Command[str]:
    cfg = settings or get_settings()
    decision = plan_solve_intent(
        _request_of(state),
        user_role=cast(UserRole, state_get(state, "user_role", "scheduler")),
        prev_plan=model_get(state, "solution", SchedulePlan),
        harness=harness,
        settings=cfg,
    )

    update: dict[str, Any] = {
        "solve_intent": decision.intent,
        "needs_clarification": decision.needs_clarification,
        "relaxation_tier": max(decision.intent.pre_authorized_tiers or [0]),
        "trace_events": emit(
            state,
            "planner",
            "decision",
            {
                "scope_persons": decision.intent.scope_persons,
                "scope_missions": decision.intent.scope_missions,
                "freeze_policy": decision.intent.freeze_policy,
                "blast_radius": decision.intent.estimated_blast_radius,
                "pre_authorized_tiers": list(decision.intent.pre_authorized_tiers),
                "open_questions": list(decision.open_questions),
                "llm_calls": decision.llm_calls,
                "degraded": decision.degraded,
                "notes": list(decision.notes),
            },
        ),
    }
    if decision.degraded:
        update["errors"] = error(
            ErrorCode.LLM_UNAVAILABLE,
            "Planner 未能经 LLM 规划，已改用中性默认 SolveIntent（排班能力不受影响）",
            severity="WARN",
            stage="intent",
            details={"notes": list(decision.notes)},
            suggestions=["如需精细控制范围与冻结档，请改用结构化入口 POST /api/v1/schedule"],
            retryable=True,
        )
    # ③ 有未决问题 → 回路由组织追问，而非自行猜测（v6 §7.3.3）
    return Command(goto=decision.next_node, update=update)


# ─────────────────────────────────────────────────────────────────────
# 修订轮：NL → 增量约束
# ─────────────────────────────────────────────────────────────────────
def _revision_round(
    state: FTSState,
    *,
    directory: EntityDirectory | None,
    harness: Harness | None,
    window_start: Any,
    horizon_minutes: int,
) -> Command[str]:
    from datetime import time as time_type

    spec = model_get(state, "constraint_spec", ConstraintSpec)
    plan = model_get(state, "solution", SchedulePlan)
    stack = RevisionStack.from_state(model_list(state, "revision_stack", IncrementalConstraint))
    utterance = _revision_utterance(state)

    # ★ 用户发起的 undo（v6 §7.3.4 第 2 条）：弹栈后**照常走完整 solve → validate**。
    #   撤销不是「把上一版方案取回来」，是「去掉那条约束再解一次」——
    #   前者会在快照变了的时候给出一版早已失效的方案。
    times = undo_times(utterance)
    if times:
        return _undo_round(state, stack=stack, spec=spec, utterance=utterance, times=times)

    try:
        translation = translate_revision(
            utterance,
            round_no=stack.round_no,
            harness=harness,
            plan=plan,
            directory=directory,
            spec=spec,
        )
    except FTSError as exc:
        # 翻译不出来就问，不猜。修订轮的失败不该把已有方案弄丢。
        return Command(
            goto="human_gate",
            update={
                "needs_human": True,
                "explanation": exc.message,
                "errors": error(
                    exc.code,
                    exc.message,
                    severity="WARN",
                    stage="intent",
                    details=exc.details,
                    suggestions=exc.suggestions,
                    retryable=True,
                ),
                "trace_events": emit(
                    state, "planner", "negotiation", {"utterance": utterance, "translated": False}
                ),
            },
        )

    human = translation.constraint
    wire = for_solver(
        human,
        window_start=cast(time_type, window_start) if window_start is not None else time_type(6, 0),
        plan=plan,
        horizon_minutes=horizon_minutes,
    )
    stack.push(human)

    updated_spec = (
        spec.model_copy(update={"incremental_constraints": [*spec.incremental_constraints, wire]})
        if spec is not None
        else None
    )

    echo = _echo_with_warnings(translation.echo, translation.warnings)
    update: dict[str, Any] = {
        "revision_stack": list(stack.items),
        # ★ 回显确认（v6 §7.3.4 第 4 条）：先展示「我理解为：……」，
        #   **用户确认后才重解**。落点是 `human_gate` 的一次往返，见下方 goto。
        "revision_echo": echo,
        "pending_revision": True,
        "needs_human": True,
        "trace_events": emit(
            state,
            "planner",
            "negotiation",
            {
                "utterance": utterance,
                "kind": human.kind,
                "targets": list(human.targets),
                "human_params": human.params,
                "solver_params": wire.params,
                "round_no": human.round_no,
                "source": translation.source,
                "warnings": list(translation.warnings),
                "llm_calls": translation.llm_calls,
            },
        ),
    }
    if updated_spec is not None:
        update["constraint_spec"] = updated_spec
    # ★ **不直接去 solve**：先回人工门禁把回显摆出来，APPROVE 之后才重解。
    #   顺序是规格要求的（§7.3.4 第 4 条「用户确认后才重解」），不是可选项 ——
    #   先解再问等于「翻译错了也已经排了一版」，而修订翻译恰恰是高风险的语义映射。
    return Command(goto="human_gate", update=update)


def _cancel_pending_revision(state: FTSState) -> Command[str]:
    """回显确认被否掉 → 弹栈撤回那条修订，回门禁（v6 §7.3.4 第 4 条的反面）。

    **与不可行回滚（FTS-3005）是两回事**：那边是「解出来发现不行」，这边是
    「用户说我压根没听懂」。后者一次求解都不该发生 —— 这正是把回显放在
    `solve` 之前的全部意义。
    """
    stack = RevisionStack.from_state(model_list(state, "revision_stack", IncrementalConstraint))
    spec = model_get(state, "constraint_spec", ConstraintSpec)
    popped = stack.undo()
    update: dict[str, Any] = {
        "revision_stack": list(stack.items),
        "revision_cancelled": False,
        "pending_revision": False,
        "needs_human": True,
        "revision_echo": (
            f"已撤回这条修订（您的原话：「{popped.origin_utterance}」），方案保持不变。"
            if popped is not None
            else "没有待确认的修订，方案保持不变。"
        ),
        "trace_events": emit(
            state,
            "planner",
            "negotiation",
            {
                "action": "cancel_pending",
                "cancelled_round": popped.round_no if popped is not None else None,
                "remaining": len(stack.items),
            },
        ),
    }
    if spec is not None and popped is not None:
        kept = [c for c in spec.incremental_constraints if c.round_no != popped.round_no]
        update["constraint_spec"] = spec.model_copy(update={"incremental_constraints": kept})
    return Command(goto="human_gate", update=update)


def _undo_round(
    state: FTSState,
    *,
    stack: RevisionStack,
    spec: ConstraintSpec | None,
    utterance: str,
    times: int,
) -> Command[str]:
    """撤销 N 轮修订并重解（v6 §7.3.4 第 2 条）。

    两件事必须同步做，缺一就是静默失效：

    1. `revision_stack` 弹出 N 条（人话形状，进审计与回显）；
    2. `constraint_spec.incremental_constraints` 按 `round_no` 同步剔除
       （线格式，进求解器）。

    只弹前者的话，栈上看不见那条修订了，**求解器里它还在** —— 用户会看到
    「已撤销」然后拿到一版没变的方案，而日志上什么异常都没有。
    """
    popped = stack.undo_many(times)
    dropped_rounds = {c.round_no for c in popped}
    update: dict[str, Any] = {
        "revision_stack": list(stack.items),
        # 撤销同样要回显确认再重解 —— 「撤销两次」听错成「撤销一次」的后果
        # 与翻译错一条约束一样大
        "revision_echo": undo_echo(popped, stack),
        "pending_revision": bool(popped),
        "needs_human": True,
        "trace_events": emit(
            state,
            "planner",
            "negotiation",
            {
                "utterance": utterance,
                "action": "undo",
                "requested": times,
                "undone": len(popped),
                "dropped_rounds": sorted(dropped_rounds),
                "remaining": len(stack.items),
                "version_no": stack.version_no(),
            },
        ),
    }
    if spec is not None:
        kept = [c for c in spec.incremental_constraints if c.round_no not in dropped_rounds]
        update["constraint_spec"] = spec.model_copy(update={"incremental_constraints": kept})
    # 没得撤销时 `pending_revision` 为 False —— 方案一个字都不会变，
    # 用户在门禁上确认了也不该触发一次求解。
    return Command(goto="human_gate", update=update)


def _request_of(state: FTSState) -> SchedulingRequest | QueryRequest | None:
    """读 `state["request"]`。两支联合类型，按 `kind` 分派后各自校验。"""
    raw = cast(dict[str, Any], state).get("request")
    if raw is None:
        return None
    payload = dict(raw.__dict__) if hasattr(raw, "__dict__") else dict(raw)
    kind = str(payload.get("kind", ""))
    if kind in ("schedule", "reschedule"):
        return SchedulingRequest.model_validate(payload)
    return QueryRequest.model_validate(payload)


def _revision_utterance(state: FTSState) -> str:
    """取本轮修订的原话。

    优先用人工门禁带回来的 `comment`——那是用户在确认页上打的字；没有就退回
    最后一条消息。两者都空时给一句空串，交由 `translate_revision` 抛「翻译不出来」。
    """
    decision = model_get(state, "human_decision", HumanDecision)
    comment = getattr(decision, "comment", "") or ""
    return comment.strip() or user_utterance(state)


def _echo_with_warnings(echo: str, warnings: tuple[str, ...]) -> str:
    if not warnings:
        return echo
    return echo + "\n⚠️ " + "\n⚠️ ".join(warnings)


def rollback_revision(
    state: FTSState,
    *,
    reason: str,
) -> dict[str, Any]:
    """修订使问题不可行 → 回滚上一版并解释（v6 §7.3.4 第 3 条 / FTS-3005）。

    **不静默丢弃**：弹出的那条约束连同原话一起写进错误详情，用户看得到
    「您说的『XX』与 YY 冲突」。
    """
    stack = RevisionStack.from_state(model_list(state, "revision_stack", IncrementalConstraint))
    popped = stack.undo()
    spec = model_get(state, "constraint_spec", ConstraintSpec)
    update: dict[str, Any] = {
        "revision_stack": list(stack.items),
        "errors": error(
            ErrorCode.REVISION_INFEASIBLE,
            f"这一轮修订使问题不可行，已回滚到上一版方案。{reason}"
            + (f"（您的原话：「{popped.origin_utterance}」）" if popped is not None else ""),
            severity="WARN",
            stage="solve",
            details={
                "rolled_back": popped.model_dump(mode="json") if popped is not None else None,
                "remaining_rounds": len(stack.items),
            },
            suggestions=["换一种说法，或先撤销更早的一条修订"],
            retryable=True,
        ),
    }
    if spec is not None:
        kept: list[IncrementalConstraint] = [
            c
            for c in spec.incremental_constraints
            if c.round_no != (popped.round_no if popped else -1)
        ]
        update["constraint_spec"] = spec.model_copy(update={"incremental_constraints": kept})
    return update


def apply_intent_tier(intent: SolveIntent) -> int:
    """预授权档位里最高的那一档就是本次的 `relaxation_tier`。"""
    return max(intent.pre_authorized_tiers or [0])


__all__ = ["apply_intent_tier", "planner_node", "rollback_revision"]
