"""`GET /api/v1/jobs/{job_id}` —— 轮询端点（v6 §8.1）。

**这个端点的全部设计约束就是「小」**：前端 1.5 s 打一次，响应体只有阶段枚举、
百分比、状态、可选的错误码。没有方案、没有校验结果、没有事件明细——那些走
`GET /runs/{trace_id}` 一次性取。

它也是唯一一个**不碰 PG** 的端点：状态在 Redis，轮询再密也压不到数据库。
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.api.deps import CurrentJobs, CurrentPrincipal
from backend.api.security import require_role
from backend.core.errors import RequiredInputMissingError
from backend.schemas.api import JobStatusView

router = APIRouter(tags=["任务"])


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusView,
    summary="轮询任务状态（阶段 + 百分比 + 状态）",
)
def get_job(job_id: str, principal: CurrentPrincipal, jobs: CurrentJobs) -> JobStatusView:
    require_role(principal, "viewer", action="查看任务状态")
    record = jobs.get(job_id)
    if record is None:
        raise RequiredInputMissingError(
            f"找不到任务 {job_id}",
            details={"job_id": job_id},
            suggestions=["确认 job_id 是否正确；任务状态保留 7 天"],
        )
    return record.to_view()


__all__ = ["router"]
