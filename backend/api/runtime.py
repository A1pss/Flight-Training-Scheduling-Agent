"""图运行时的装配与读取：API 进程与 RQ worker 共用这一份。

## 为什么装配要单独一个模块

API 进程（读运行结果）与 worker 进程（跑图）需要**完全一样的一套依赖**：同一个
`EntityDirectory`、同一份知识层、同一个 `today`。装配散在两处的话，
「worker 用了新快照的名录、API 用了旧的」这类问题不会报错，只会让两边看到的
实体名对不上。

## `today` 从哪来

`GraphDeps.today` 默认是 `date.today()`，而 M4-B §8 第 3 条明确要求由外部传入
（重放时它必须是同一个值）。这里的口径：**API 层显式传 `date.today()` 一次**，
之后整条链路（含 worker、含恢复）都用请求提交那一刻的日期——恢复隔天发生时，
用的仍是当初那天，因为它已经写进 checkpoint 了。

## checkpoint 的开关边界

`PostgresSaver.from_conn_string` 是上下文管理器，连接随 `with` 结束而关闭。
所以每个需要读写 checkpoint 的操作都自己开一次（一次请求一个连接），
**不做进程级长连接**——长连接在 uvicorn 多 worker + RQ 多进程下要自己管重连，
而这里的调用频率（轮询走的是 Redis，不碰 checkpoint）根本用不上连接池。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from backend.core.config import Settings, get_settings
from backend.core.db import session_scope
from backend.graph.checkpointer import checkpoint_dsn
from backend.graph.graph import GraphDeps, build_graph
from backend.graph.state import FTSState, model_get, model_list
from backend.graph.state import get as state_get
from backend.harness import Harness, PromptRegistry
from backend.models.audit import TraceEventRow
from backend.routing.entities import directory_from_session
from backend.schemas.api import (
    ErrorView,
    GatePayloadView,
    JobStage,
    JobStatus,
    RunResultView,
    SolverPanelView,
)
from backend.schemas.common import ErrorItem, TraceEvent
from backend.schemas.intent import SolveIntent
from backend.schemas.plan import SchedulePlan
from backend.schemas.retrieval import GroundingReport
from backend.schemas.solver import ConflictItem, RelaxationProposal, SolverStats
from backend.schemas.validation import SchemaCheckReport, ValidationReport
from backend.skills_loader import load_library


def build_deps(
    session: Session,
    *,
    snapshot_id: str,
    today: date,
    settings: Settings | None = None,
    use_llm: bool = True,
    plans_root: Path | None = None,
) -> GraphDeps:
    """装一份图依赖。

    `use_llm=False` 时 `harness_factory` 恒返回 None —— 那正是 v6 §9.3 FTS-4001
    的降级路径（LLM 挂了，排班能力完全不受影响），也是 CI 的跑法。
    """
    cfg = settings or get_settings()
    harness: Harness | None = None
    if use_llm:
        harness = Harness(snapshot_id=snapshot_id, settings=cfg)

    @contextmanager
    def _shared() -> Iterator[Session]:
        """整个图共用调用方给的会话。

        与 `tests/integration/_hitl_worker.py` 同一手法：worker 进程里一次运行
        就是一个事务边界，`commit_plan` 自己 `commit()`。
        """
        yield session

    return GraphDeps(
        session_factory=_shared,
        harness_factory=lambda _state: harness,
        directory=directory_from_session(session, snapshot_id),
        library=load_library(),
        settings=cfg,
        today=today,
        plans_root=plans_root or cfg.PLANS_DIR,
        prompt_versions=dict(PromptRegistry.load(settings=cfg).versions()),
    )


@contextmanager
def checkpointer_scope() -> Iterator[PostgresSaver]:
    """开一个 `PostgresSaver`（建表幂等）。"""
    with PostgresSaver.from_conn_string(checkpoint_dsn()) as saver:
        saver.setup()
        yield saver


def thread_config(trace_id: str) -> dict[str, Any]:
    """LangGraph 的 `configurable`。**`thread_id` 就是 `trace_id`。**

    一次运行 = 一个 thread。这样 `/runs/{trace_id}` 不需要额外的映射表，
    HITL 恢复也是拿同一个 id（v6 §9.2：人工确认可以隔天再来）。
    """
    return {"configurable": {"thread_id": trace_id}}


# ─────────────────────────────────────────────────────────────────────
# TraceEvent 落库（回放完整性的持久化那一半）
# ─────────────────────────────────────────────────────────────────────
def persist_trace_events(session: Session, run_id: str, events: list[TraceEvent]) -> int:
    """把本次运行的事件写进 `trace_events`，返回新写入的条数。

    **按 `(run_id, seq)` 去重**（表上就有这个唯一约束）：HITL 恢复时状态里带着
    上一段的全部事件，不去重就会撞唯一约束把整个恢复搞挂。用
    `ON CONFLICT DO NOTHING` 而不是先查后插，是因为两个 worker 理论上可能同时
    写同一个 run（恢复与超时重试撞车），先查后插挡不住。

    为什么要落库：checkpoint 里已经有一份，但那是 LangGraph 的内部表，
    随 thread 清理而去。回放是**审计**能力（v6 §8.2），它的寿命应该跟着计划走，
    不跟着 checkpoint 走。
    """
    if not events:
        return 0
    rows = [
        {
            "run_id": run_id,
            "seq": e.seq,
            "ts": e.ts,
            "agent": e.agent,
            "kind": e.kind,
            "payload": e.payload,
            "duration_ms": e.duration_ms,
            "token_usage": e.token_usage,
        }
        for e in events
    ]
    stmt = (
        pg_insert(TraceEventRow)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["run_id", "seq"])
    )
    result = session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


def load_trace_events(session: Session, run_id: str) -> list[TraceEvent]:
    """按 `seq` 读回全量事件。"""
    rows = session.execute(
        select(TraceEventRow).where(TraceEventRow.run_id == run_id).order_by(TraceEventRow.seq)
    ).scalars()
    return [
        TraceEvent(
            seq=row.seq,
            ts=row.ts,
            agent=row.agent,
            kind=cast(Any, row.kind),
            payload=dict(row.payload or {}),
            duration_ms=row.duration_ms,
            token_usage=row.token_usage,
        )
        for row in rows
    ]


# ─────────────────────────────────────────────────────────────────────
# 状态 → 对外结果
# ─────────────────────────────────────────────────────────────────────
def runway_allocation(plan: SchedulePlan | None) -> dict[str, int]:
    """跑道 → 架次数（v6 §8.2 求解面板最后一格）。"""
    if plan is None:
        return {}
    counts: dict[str, int] = {}
    for sortie in plan.sorties:
        counts[sortie.runway_id] = counts.get(sortie.runway_id, 0) + 1
    return dict(sorted(counts.items()))


def _errors_view(state: FTSState, trace_id: str) -> list[ErrorView]:
    return [
        ErrorView(
            code=item.code,
            message=item.message,
            severity=item.severity,
            stage=item.stage,
            details=item.details,
            suggestions=item.suggestions,
            trace_id=trace_id,
            retryable=item.retryable,
        )
        for item in model_list(state, "errors", ErrorItem)
    ]


def _gate_view(state: FTSState, *, awaiting: bool) -> GatePayloadView:
    intent = model_get(state, "solve_intent", SolveIntent)
    return GatePayloadView(
        awaiting=awaiting,
        pending_revision=bool(state_get(state, "pending_revision", False)),
        revision_echo=str(state_get(state, "revision_echo", "")),
        relaxation_tier=int(state_get(state, "relaxation_tier", 0)),
        open_questions=list(getattr(intent, "open_questions", []) or []),
        ambiguities=list(state_get(state, "ambiguities", cast(list[dict[str, Any]], []))),
    )


def build_run_result(
    state: FTSState,
    *,
    trace_id: str,
    status: JobStatus,
    stage: JobStage,
    awaiting: bool,
    events: list[TraceEvent],
    job_id: str | None = None,
) -> RunResultView:
    """黑板状态 → `RunResultView`（`GET /runs/{trace_id}` 的响应）。

    **一次性把全部东西装进去**（v6 §8.1：轮询只取阶段，完成后一次取回完整结果）。
    """
    plan = model_get(state, "solution", SchedulePlan)
    intent_value = state_get(state, "intent", cast(Any, None))
    return RunResultView(
        trace_id=trace_id,
        job_id=job_id,
        status=status,
        stage=stage,
        intent=str(intent_value) if intent_value else None,
        snapshot_id=state_get(state, "snapshot_id", cast(str | None, None)),
        ruleset_version=state_get(state, "ruleset_version", cast(str | None, None)),
        semantics_version=state_get(state, "semantics_version", cast(str | None, None)),
        plan=plan,
        validation=model_get(state, "validation", ValidationReport),
        schema_check=model_get(state, "schema_check", SchemaCheckReport),
        solver=SolverPanelView(
            stats=model_get(state, "solver_stats", SolverStats),
            runway_allocation=runway_allocation(plan),
        ),
        explanation=state_get(state, "explanation", cast(str | None, None)),
        grounding=model_get(state, "grounding_report", GroundingReport),
        conflicts=model_list(state, "conflict_set", ConflictItem),
        relaxation_proposals=model_list(state, "relaxation_proposals", RelaxationProposal),
        errors=_errors_view(state, trace_id),
        gate=_gate_view(state, awaiting=awaiting),
        trace_events=sorted(events, key=lambda e: e.seq),
        workbook_path=state_get(state, "workbook_path", cast(str | None, None)),
        committed_plan_id=state_get(state, "committed_plan_id", cast(str | None, None)),
    )


@contextmanager
def graph_for_reading(
    snapshot_id: str, today: date
) -> Iterator[CompiledStateGraph[Any, Any, Any, Any]]:
    """只读地拿一个挂着 checkpointer 的图（用于 `get_state`）。

    `use_llm=False`：读状态不该有能力去调模型。
    """
    with session_scope() as session, checkpointer_scope() as saver:
        deps = build_deps(session, snapshot_id=snapshot_id, today=today, use_llm=False)
        yield build_graph(deps, checkpointer=saver)


def read_run_state(trace_id: str, *, today: date, snapshot_id: str = "") -> tuple[FTSState, bool]:
    """读一次运行的状态，返回 `(黑板, 是否停在人工门禁)`。

    **走 `graph.get_state()` 而不是直接读 checkpoint 表**：`next` 这个「还有哪个
    节点没跑」的判断由 LangGraph 自己算，抄一遍它的规则迟早对不上——
    而对不上的后果是前端显示「已完成」，实际上还等着人按确认。
    """
    with graph_for_reading(snapshot_id, today) as app:
        snapshot = app.get_state(cast(Any, thread_config(trace_id)))
        return cast(FTSState, snapshot.values or {}), bool(snapshot.next)


__all__ = [
    "build_deps",
    "build_run_result",
    "checkpointer_scope",
    "graph_for_reading",
    "load_trace_events",
    "persist_trace_events",
    "runway_allocation",
    "thread_config",
]
