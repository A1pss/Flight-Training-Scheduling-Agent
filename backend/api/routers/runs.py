"""`GET /api/v1/runs/{trace_id}` —— 完整结果（v6 §9.1）。

方案 + 校验报告 + **TraceEvent 全量**。三样一次取回，前端此后不再打后端
（步进回放是纯前端计算，v6 §8.2）。

## 事件从哪读

优先读 `trace_events` 表（worker 落的那份，寿命跟着计划走），空的时候回落到
checkpoint 里的状态。**两处都读得到才叫回放完整**——只靠 checkpoint 的话，
thread 被清理后回放就没了；只靠表的话，还在跑的运行看不到已发生的事件。
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.deps import (
    CurrentJobs,
    CurrentPrincipal,
    CurrentSession,
    CurrentToday,
)
from backend.api.runtime import build_run_result, load_trace_events, read_run_state
from backend.api.security import require_role
from backend.core.errors import RequiredInputMissingError
from backend.graph.state import model_list
from backend.schemas.api import JobStage, JobStatus, RunResultView
from backend.schemas.common import TraceEvent

router = APIRouter(tags=["任务"])


@router.get(
    "/runs/{trace_id}",
    response_model=RunResultView,
    summary="取回完整结果：方案 + 校验报告 + TraceEvent 全量",
)
def get_run(
    trace_id: str,
    principal: CurrentPrincipal,
    session: CurrentSession,
    jobs: CurrentJobs,
    today: CurrentToday,
) -> RunResultView:
    require_role(principal, "viewer", action="查看运行结果")
    record = jobs.get_by_trace(trace_id)
    snapshot_id = ""
    if record is not None:
        snapshot_id = str(record.extra.get("snapshot_id", ""))

    state, awaiting = read_run_state(trace_id, today=today, snapshot_id=snapshot_id)
    if not state:
        raise RequiredInputMissingError(
            f"找不到运行 {trace_id}",
            details={"trace_id": trace_id},
            suggestions=["确认 trace_id 是否正确；未提交过的会话没有运行记录"],
        )

    events = load_trace_events(session, trace_id)
    if not events:
        events = model_list(state, "trace_events", TraceEvent)

    status = record.status if record else (JobStatus.AWAITING_HUMAN if awaiting else JobStatus.DONE)
    stage = record.stage if record else JobStage.REPORTING
    return build_run_result(
        state,
        trace_id=trace_id,
        status=status,
        stage=stage,
        awaiting=awaiting,
        events=events,
        job_id=record.job_id if record else None,
    )


__all__ = ["router"]
