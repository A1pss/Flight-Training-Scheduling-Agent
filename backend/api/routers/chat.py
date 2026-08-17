"""`POST /api/v1/chat` —— 统一自然语言入口（v6 §9.1）。

幂等键是**客户端 UUID**，不是消息内容的哈希。理由很实在：同一句话用户可能真要
说两遍（「再排一次」「换个说法试试」），拿内容做键会把第二次当成重放。

## 它不做意图分类

分类是图里 `route` 节点的事（两级：规则匹配 + LLM 兜底）。这里只做三件事：
定快照、定周（能定就定，定不了不猜）、把话原样交给图。**API 层多解读一层，
就是多一处与 `routing/rules.py` 漂移的可能。**
"""

from __future__ import annotations

from fastapi import APIRouter, status

from backend.api.deps import (
    CurrentIdempotency,
    CurrentJobs,
    CurrentLocks,
    CurrentPrincipal,
    CurrentRunner,
    CurrentSession,
    CurrentSettings,
    CurrentToday,
)
from backend.api.security import require_role
from backend.api.service import (
    SubmitContext,
    require_snapshot,
    resolve_week_start,
    submit_run,
)
from backend.schemas.api import ChatRequest, JobSubmitView

router = APIRouter(tags=["会话"])


@router.post(
    "/chat",
    response_model=JobSubmitView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="自然语言入口（异步，返回 job_id）",
)
def post_chat(
    body: ChatRequest,
    principal: CurrentPrincipal,
    session: CurrentSession,
    jobs: CurrentJobs,
    locks: CurrentLocks,
    idempotency: CurrentIdempotency,
    runner: CurrentRunner,
    settings: CurrentSettings,
    today: CurrentToday,
) -> JobSubmitView:
    """提交一句话，立即返回 `job_id` + `trace_id`（v6 §8.1）。

    `thread_id` 非空时**续接**那次运行（修订轮走这条路）：trace_id 沿用，
    LangGraph 按同一个 thread 从 checkpoint 接着跑。
    """
    require_role(principal, "scheduler", action="发起会话")
    snapshot = require_snapshot(session, body.snapshot_id)
    week_start, iso_week = resolve_week_start(
        explicit=body.week_start, message=body.message, today=today
    )
    ctx = SubmitContext(
        jobs=jobs,
        locks=locks,
        idempotency=idempotency,
        runner=runner,
        settings=settings,
        today=today,
    )
    return submit_run(
        ctx,
        scope="chat",
        idem_token=body.client_request_id,
        principal=principal,
        tenant_id=settings.TENANT_ID,
        kind="chat",
        snapshot_id=snapshot,
        trace_id=body.thread_id,
        message=body.message,
        week_start=week_start,
        iso_week=iso_week,
        use_llm=True,
    )


__all__ = ["router"]
