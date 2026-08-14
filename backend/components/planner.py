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
from backend.planner.revision import RevisionStack, for_solver, translate_revision
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

    update: dict[str, Any] = {
        "revision_stack": list(stack.items),
        # ★ 回显确认（v6 §7.3.4 第 4 条）：UI 先展示「我理解为：……」，
        #   用户确认后才重解。这一步不能省。
        "explanation": _echo_with_warnings(translation.echo, translation.warnings),
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
    return Command(goto="compile_spec" if updated_spec is None else "solve", update=update)


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
