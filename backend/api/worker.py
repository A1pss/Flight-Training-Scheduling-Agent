"""RQ worker 侧的图执行（v6 §9.2）。

```
POST /chat|/schedule ──► 入队 ──► 本模块 execute_run() ──► LangGraph 图
                                        │
                    每个节点结束时写一次 JobStore（阶段 + 百分比）
                                        │
                    走到 human_gate 的 interrupt() → AWAITING_HUMAN，进程可退出
                                        │
POST /approve|/reject ─► 入队 ──► 本模块 execute_run(kind="resume") ──► 从断点继续
```

## 三件必须在这里做、别处做不了的事

1. **快照级锁**（`Z-24`）。`compile_spec` 刷 `training_progress` 物化视图时按
   主键 DELETE+INSERT，而那张表的主键不含 `snapshot_id`（v6 §6.3.2）——
   同快照的两周并发排班会死锁（M5 §9.1 第 7 条有实测的 `DeadlockDetected`）。
   锁必须在**图执行的进程里**取，因为 API 进程提交完就返回了。

2. **排班锁的释放。** 锁在 API 进程取（提交时就要能立刻拒绝第二个人），在这里
   释放。释放的凭据是 `JobRecord.lock_token`，跨进程传的就是它。
   **走到人工门禁时就释放**——那之后可能停一整天等人确认，而锁的 TTL 是 1800 s，
   不主动放也会过期；主动放让「谁持有」这件事始终如实。

3. **TraceEvent 落库。** checkpoint 里那份随 thread 清理而去，而回放是审计能力，
   寿命该跟着计划走（见 `runtime.persist_trace_events` 的注释）。

## 失败了也要把状态写清楚

`finally` 里做三件事：落事件、放锁、写终态。**顺序不能反**——先放锁再写状态的话，
第二个人可能在状态还是 RUNNING 时就拿到锁，前端会同时看到两个「正在排」。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from langgraph.types import Command

from backend.api.jobs import JobRecord, JobStore
from backend.api.locks import LockHandle, LockManager, schedule_lock_key
from backend.api.runtime import (
    build_deps,
    checkpointer_scope,
    persist_trace_events,
    thread_config,
)
from backend.api.store import KeyValueStore, build_store
from backend.core.config import Settings, get_settings
from backend.core.db import get_session_factory
from backend.core.errors import FTSError
from backend.core.logging import get_logger
from backend.graph.graph import build_graph
from backend.graph.state import FTSState, initial_state, model_list
from backend.schemas.api import JobStatus
from backend.schemas.common import HumanDecision, TraceEvent

logger = get_logger(__name__)


@dataclass
class RunPayload:
    """入队的任务载荷。**必须是纯 JSON**（RQ 要序列化它）。"""

    job_id: str
    trace_id: str
    kind: str  # "start" | "resume"
    tenant_id: str = "default"
    user_id: str = ""
    user_role: str = "scheduler"
    message: str = ""
    snapshot_id: str = ""
    week_start: str = ""
    today: str = ""
    iso_week: str = ""
    lock_token: str = ""
    use_llm: bool = True
    relaxation_tier: int = 0
    decision: dict[str, Any] = field(default_factory=dict)
    plans_root: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "trace_id": self.trace_id,
            "kind": self.kind,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "user_role": self.user_role,
            "message": self.message,
            "snapshot_id": self.snapshot_id,
            "week_start": self.week_start,
            "today": self.today,
            "iso_week": self.iso_week,
            "lock_token": self.lock_token,
            "use_llm": self.use_llm,
            "relaxation_tier": self.relaxation_tier,
            "decision": self.decision,
            "plans_root": self.plans_root,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunPayload:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _initial(payload: RunPayload) -> FTSState:
    state = initial_state(
        trace_id=payload.trace_id,
        user_id=payload.user_id or "unknown",
        tenant_id=payload.tenant_id,
        user_role=cast(Any, payload.user_role),
        messages=[{"role": "user", "content": payload.message}] if payload.message else [],
        snapshot_id=payload.snapshot_id or None,
        week_start=payload.week_start or None,
    )
    if payload.relaxation_tier:
        cast(dict[str, Any], state)["relaxation_tier"] = payload.relaxation_tier
    return state


def _graph_input(payload: RunPayload) -> Any:
    if payload.kind == "resume":
        decision = HumanDecision.model_validate(payload.decision)
        return Command(resume=decision)
    return _initial(payload)


def _interrupted(chunk: dict[str, Any]) -> bool:
    return "__interrupt__" in chunk


def execute_run(raw: dict[str, Any], *, store: KeyValueStore | None = None) -> dict[str, Any]:
    """RQ 的任务函数。**同步、可重入、不抛到队列外**。

    返回一个小字典（`{"status": ..., "trace_id": ...}`），RQ 会把它存进
    job.result；真正的结果一律走 `GET /runs/{trace_id}`。

    `store` **必须能被注入**：RQ 路径下 worker 是独立进程，自己按配置连 Redis
    （默认分支）；而 inline 路径下它与 API 在同一个进程里，**必须用同一个 store**
    —— 否则 worker 写的是另一份状态，轮询永远停在 `QUEUED`，而**没有任何报错**。
    这是实测踩到的：第一版 inline 跑完，`GET /jobs` 仍然返回 QUEUED、
    `finished_at` 为空，看起来像求解卡死了。
    """
    payload = RunPayload.from_dict(raw)
    settings = get_settings()
    backend_store = store if store is not None else build_store(settings)
    jobs = JobStore(backend_store)
    locks = LockManager(backend_store)
    started = time.perf_counter()

    schedule_lock: LockHandle | None = None
    if payload.lock_token and payload.iso_week:
        schedule_lock = LockHandle(
            key=schedule_lock_key(payload.tenant_id, payload.iso_week),
            token=payload.lock_token,
            holder=payload.user_id,
            ttl_s=0,
        )

    session = get_session_factory()()
    snapshot_lock: LockHandle | None = None
    events: list[TraceEvent] = []
    status = JobStatus.FAILED
    try:
        jobs.mark(payload.job_id, JobStatus.RUNNING)
        if payload.snapshot_id:
            # ★ `Z-24`：同快照的排班全局串行，避开 training_progress 上的死锁
            snapshot_lock = locks.acquire_snapshot(
                payload.snapshot_id, holder=f"job:{payload.job_id}"
            )
        status = _run_graph(payload, settings, session, jobs, events)
        return {
            "trace_id": payload.trace_id,
            "job_id": payload.job_id,
            "status": str(status),
            "events": len(events),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except FTSError as exc:
        logger.warning(
            "排班任务失败",
            extra={"job_id": payload.job_id, "code": str(exc.code), "message": exc.message},
        )
        jobs.mark(
            payload.job_id,
            JobStatus.FAILED,
            error_code=exc.code,
            error_message=exc.message,
            finished_at=datetime.now(),
        )
        return {"trace_id": payload.trace_id, "status": "FAILED", "code": str(exc.code)}
    except Exception as exc:  # 队列里的任务不许把异常抛给 RQ 自己吞掉
        logger.error("排班任务异常", extra={"job_id": payload.job_id, "error": repr(exc)})
        jobs.mark(
            payload.job_id,
            JobStatus.FAILED,
            error_message=repr(exc),
            finished_at=datetime.now(),
        )
        return {"trace_id": payload.trace_id, "status": "FAILED", "error": repr(exc)}
    finally:
        # ① 事件落库 → ② 放锁 → ③ 终态已在上面写好。顺序见模块注释
        try:
            if events:
                persist_trace_events(session, payload.trace_id, events)
                session.commit()
        finally:
            session.close()
            locks.release(snapshot_lock)
            locks.release(schedule_lock)


def _run_graph(
    payload: RunPayload,
    settings: Settings,
    session: Any,
    jobs: JobStore,
    sink: list[TraceEvent],
) -> JobStatus:
    """跑图并按节点推进阶段。返回终态；**事件写进 `sink`**。

    事件用出参而不是返回值，是为了让**失败的运行也留下轨迹**：图中途抛异常时
    返回值根本回不来，而那种时候的轨迹恰恰最有用（能看出走到哪一步炸的）。
    `finally` 里无论成败都把已发生的事件填进 `sink`，由调用方落库。
    """
    today = date.fromisoformat(payload.today) if payload.today else date.today()
    plans_root = Path(payload.plans_root) if payload.plans_root else None
    awaiting = False
    with checkpointer_scope() as saver:
        deps = build_deps(
            session,
            snapshot_id=payload.snapshot_id,
            today=today,
            settings=settings,
            use_llm=payload.use_llm,
            plans_root=plans_root,
        )
        app = build_graph(deps, checkpointer=saver)
        config = cast(Any, thread_config(payload.trace_id))

        try:
            for chunk in app.stream(_graph_input(payload), config, stream_mode="updates"):
                if not isinstance(chunk, dict):
                    continue
                if _interrupted(chunk):
                    awaiting = True
                    continue
                for node in chunk:
                    jobs.advance(payload.job_id, str(node))
        finally:
            # 读状态**不许掩盖原异常**：这一步自己炸掉时（比如 checkpoint 连接已断），
            # 真正该报给用户的是图里那个错，不是「读状态失败」
            try:
                snapshot = app.get_state(config)
                state = cast(FTSState, snapshot.values)
                sink.extend(model_list(state, "trace_events", TraceEvent))
                awaiting = awaiting or bool(snapshot.next)
            except Exception as probe_error:  # pragma: no cover —— 只在连接已断时走到
                logger.warning(
                    "取运行状态失败，本次不落轨迹",
                    extra={"job_id": payload.job_id, "error": repr(probe_error)},
                )

    events = sink
    status = JobStatus.AWAITING_HUMAN if awaiting else JobStatus.DONE
    record = jobs.get(payload.job_id)
    jobs.mark(
        payload.job_id,
        status,
        percent=_percent_for(status, record),
        finished_at=None if awaiting else datetime.now(),
        extra={"events": len(events)},
    )
    return status


def _percent_for(status: JobStatus, record: JobRecord | None) -> int:
    """`AWAITING_HUMAN` 停在 95%，**不写 100%**。

    写 100 % 会让用户以为已经归档完了——而实际上东西还等着他按确认。
    """
    if status is JobStatus.DONE:
        return 100
    return max(95, record.percent if record else 0)


__all__ = ["RunPayload", "execute_run"]
