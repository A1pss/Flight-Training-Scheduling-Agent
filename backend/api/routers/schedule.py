"""排班相关的四个端点（v6 §9.1）。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/schedule` | 结构化排班入口，**也是 FTS-4001 的降级路径** |
| POST | `/schedule/{trace_id}/approve` | 人工确认 → 归档 + 推进进度 + 结算欠账 + 写锚点 |
| POST | `/schedule/{trace_id}/reject` | 驳回附意见 |
| GET | `/schedule/{trace_id}/export` | 下载 xlsx |

## `{id}` 是 `trace_id`，不是 `plan_id`

v6 §9.1 只写了 `{id}`。这里定为 **`trace_id`**，因为决策要走 LangGraph 的
`Command(resume=...)`，而它认的是 `thread_id`——本系统里 `thread_id == trace_id`
（见 `runtime.thread_config`）。`plan_id` 在方案被归档**之前**并不能唯一定位
一次运行（同一个 plan_id 可能被修订轮重算多次），拿它当 URL 段会在最需要
恢复的时候找不到 thread。

## `/schedule` 为什么不走 LLM

它是 FTS-4001 的降级入口：LLM 挂了，用户改用表单提交结构化参数照样能排班
（v6 §9.3 脚注「工程化 vs demo 的分水岭」）。所以 `use_llm=False` 是写死的，
不是配置——这条路径必须在模型完全不可用时仍然工作。

## approve 的三重把关

1. **角色**：`director` 及以上（归档是本系统唯一不可撤销的写）；
2. **档位**：`authorized_tiers` 里的每一档再按 `RELAX_TIER_AUTHORITY` 核一次
   （Tier 3 需训练主任本人）——角色够格进这个端点不等于够格批那一档；
3. **状态**：只有停在人工门禁的运行才能被决策，否则 409。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, status
from fastapi.responses import FileResponse

from backend.api.audit import AuditRecorder, CurrentAudit
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
from backend.api.idempotency import body_fingerprint
from backend.api.jobs import JobRecord, JobStore
from backend.api.locks import iso_week_of
from backend.api.runtime import read_run_state
from backend.api.security import AuthError, Principal, require_role
from backend.api.service import (
    SubmitContext,
    poll_url,
    require_snapshot,
    submit_run,
)
from backend.core.errors import RequiredInputMissingError, ScheduleLockedError
from backend.graph.state import FTSState
from backend.graph.state import get as state_get
from backend.schemas.api import (
    DecisionRequest,
    DecisionView,
    JobSubmitView,
    ScheduleRequest,
)
from backend.schemas.common import HumanDecision

router = APIRouter(tags=["排班"])


@router.post(
    "/schedule",
    response_model=JobSubmitView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="结构化排班入口（异步，零 LLM 依赖）",
)
def post_schedule(
    body: ScheduleRequest,
    principal: CurrentPrincipal,
    session: CurrentSession,
    jobs: CurrentJobs,
    locks: CurrentLocks,
    idempotency: CurrentIdempotency,
    runner: CurrentRunner,
    settings: CurrentSettings,
    today: CurrentToday,
    audit: CurrentAudit,
) -> JobSubmitView:
    require_role(principal, "scheduler", action="提交排班")
    if body.relaxation_tier and not principal.can_authorize_tier(body.relaxation_tier):
        raise AuthError(
            f"{principal.user_id}（{principal.role}）无权使用 Tier {body.relaxation_tier} 松弛",
            status_code=403,
        )
    snapshot = require_snapshot(session, body.snapshot_id)
    week_start = body.week_start
    if week_start.weekday() != 0:
        raise RequiredInputMissingError(
            f"week_start 必须是周一，实际 {week_start}（{week_start.strftime('%A')}）",
            details={"week_start": week_start.isoformat(), "resolution": "answer"},
            suggestions=["排班周恒为周一~周日，请给出该周的周一"],
        )
    token = body.client_request_id or body_fingerprint(body.model_dump(mode="json"))
    ctx = SubmitContext(
        jobs=jobs,
        locks=locks,
        idempotency=idempotency,
        runner=runner,
        settings=settings,
        today=today,
    )
    submitted = submit_run(
        ctx,
        scope="schedule",
        idem_token=token,
        principal=principal,
        tenant_id=settings.TENANT_ID,
        kind="schedule",
        snapshot_id=snapshot,
        message=_structured_message(body),
        week_start=week_start,
        iso_week=iso_week_of(week_start),
        relaxation_tier=body.relaxation_tier,
        # ★ FTS-4001 降级路径：这条路上一次 LLM 都不调
        use_llm=False,
    )
    audit.record(
        action="api.schedule.submit",
        resource_type="run",
        resource_id=submitted.trace_id,
        after={
            "job_id": submitted.job_id,
            "snapshot_id": snapshot,
            "week_start": week_start.isoformat(),
            "iso_week": iso_week_of(week_start),
            "relaxation_tier": body.relaxation_tier,
            "idempotent_hit": submitted.idempotent_hit,
        },
    )
    return submitted


def _structured_message(body: ScheduleRequest) -> str:
    """把结构化请求转成一句给 `route` 节点的话。

    **它不是给模型看的**（这条路 `use_llm=False`）：`route` 的一级规则表按
    关键词命中「排班」意图，一句规范的中文即可，且它会原样出现在运行记录里，
    让人看得出这次是表单提交的。
    """
    parts = [f"给 {body.week_start.isoformat()} 那一周排班"]
    if body.person_ids:
        parts.append("人员：" + "、".join(body.person_ids))
    if body.aircraft_ids:
        parts.append("飞机：" + "、".join(body.aircraft_ids))
    if body.relaxation_tier:
        parts.append(f"松弛档位 Tier {body.relaxation_tier}")
    return "；".join(parts)


def _record_or_404(jobs: JobStore, trace_id: str) -> JobRecord:
    record = jobs.get_by_trace(trace_id)
    if record is None:
        raise RequiredInputMissingError(
            f"找不到运行 {trace_id}",
            details={"trace_id": trace_id},
            suggestions=["确认 trace_id 是否正确，或该运行的状态是否已过期（7 天）"],
        )
    return record


def _decide(
    *,
    decision: Literal["APPROVE", "REJECT"],
    trace_id: str,
    body: DecisionRequest,
    principal: Principal,
    ctx: SubmitContext,
    audit: AuditRecorder,
) -> DecisionView:
    """决策一次运行。

    **快照取自这次运行当初用的那个**（`JobRecord.extra`），不是此刻 ACTIVE 的
    那个：数据在人工确认期间更新过的话，两者不是一回事，而「基于过期数据批准」
    这件事由图里的 `resume_guard` 判（FTS-3004，v6 §9.2 那段黑体字），
    不该在 API 层悄悄换成新快照。
    """
    record = _record_or_404(ctx.jobs, trace_id)
    snapshot_id = str(record.extra.get("snapshot_id", ""))
    state, awaiting = read_run_state(trace_id, today=ctx.today, snapshot_id=snapshot_id)
    if not awaiting:
        raise ScheduleLockedError(
            f"运行 {trace_id} 当前不在人工门禁上（状态 {record.status}），无法{decision}",
            details={"trace_id": trace_id, "status": str(record.status)},
            suggestions=["先轮询 GET /jobs/{job_id} 直到 AWAITING_HUMAN 再提交决策"],
        )
    human = HumanDecision(
        decision=decision,
        user_id=principal.user_id,
        role=principal.role,
        comment=body.comment,
        authorized_tiers=list(body.authorized_tiers),
        decided_at=datetime.now(),
    )
    token = body.client_request_id or body_fingerprint(
        {"trace_id": trace_id, "decision": decision, "comment": body.comment}
    )
    submitted = submit_run(
        ctx,
        scope=f"decision:{decision.lower()}",
        idem_token=token,
        principal=principal,
        tenant_id=record.tenant_id,
        kind=decision.lower(),
        snapshot_id=snapshot_id,
        trace_id=trace_id,
        iso_week=record.iso_week,
        decision=human.model_dump(mode="json"),
    )
    # ★ 审计（v6 §11.5）：`before` 是**决策前那次运行的状态**，`after` 是决策
    # 本身。这样 diff 里能直接读出「本来停在 AWAITING_HUMAN 的 14 架次方案，
    # 被 P01 从 10.x.x.x 批了」。归档本身的数据变更由 `commit_plan` 那一路负责。
    audit.record(
        action=f"api.schedule.{decision.lower()}",
        resource_type="run",
        resource_id=trace_id,
        before=_run_fingerprint(state, record, awaiting=awaiting),
        after={
            "status": "DECIDED",
            "awaiting_human": False,
            "decision": decision,
            "comment": body.comment,
            "authorized_tiers": list(body.authorized_tiers),
            "decided_by": principal.user_id,
            "job_id": submitted.job_id,
        },
    )
    return DecisionView(
        job_id=submitted.job_id,
        trace_id=trace_id,
        decision=decision,
        status=submitted.status,
        idempotent_hit=submitted.idempotent_hit,
        poll_url=poll_url(submitted.job_id),
    )


def _run_fingerprint(state: FTSState, record: JobRecord, *, awaiting: bool) -> dict[str, object]:
    """决策前那次运行的可审计指纹。

    **只取能一眼看懂的几个量**（状态、方案 id、架次数、求解状态、快照），
    不把整个 `SchedulePlan` 塞进审计行 —— 完整方案在 `data/plans/` 的归档里，
    审计表的职责是「谁在什么状态下做了什么决定」，不是再存一份方案。
    """
    plan = state_get(state, "plan", None)
    return {
        "status": str(record.status),
        "awaiting_human": awaiting,
        "snapshot_id": str(record.extra.get("snapshot_id", "")),
        "iso_week": record.iso_week,
        "plan_id": getattr(plan, "plan_id", "") if plan is not None else "",
        "sorties": len(getattr(plan, "sorties", ()) or ()) if plan is not None else 0,
        "solver_status": str(state_get(state, "solver_status", "") or ""),
    }


@router.post(
    "/schedule/{trace_id}/approve",
    response_model=DecisionView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="人工确认 → 归档 + 回写记忆（需训练主任）",
)
def post_approve(
    trace_id: str,
    body: DecisionRequest,
    principal: CurrentPrincipal,
    jobs: CurrentJobs,
    locks: CurrentLocks,
    idempotency: CurrentIdempotency,
    runner: CurrentRunner,
    settings: CurrentSettings,
    today: CurrentToday,
    audit: CurrentAudit,
) -> DecisionView:
    require_role(principal, "director", action="确认并归档排班方案")
    for tier in body.authorized_tiers:
        if not principal.can_authorize_tier(tier):
            raise AuthError(
                f"{principal.user_id}（{principal.role}）无权授权 Tier {tier} 松弛",
                status_code=403,
            )
    ctx = SubmitContext(
        jobs=jobs,
        locks=locks,
        idempotency=idempotency,
        runner=runner,
        settings=settings,
        today=today,
    )
    return _decide(
        decision="APPROVE",
        trace_id=trace_id,
        body=body,
        principal=principal,
        ctx=ctx,
        audit=audit,
    )


@router.post(
    "/schedule/{trace_id}/reject",
    response_model=DecisionView,
    status_code=status.HTTP_202_ACCEPTED,
    summary="驳回附意见（不自动重排）",
)
def post_reject(
    trace_id: str,
    body: DecisionRequest,
    principal: CurrentPrincipal,
    jobs: CurrentJobs,
    locks: CurrentLocks,
    idempotency: CurrentIdempotency,
    runner: CurrentRunner,
    settings: CurrentSettings,
    today: CurrentToday,
    audit: CurrentAudit,
) -> DecisionView:
    """驳回。**不自动重排**——驳回的理由要人来定（v6 §7.2.4 那张表）。"""
    require_role(principal, "scheduler", action="驳回排班方案")
    ctx = SubmitContext(
        jobs=jobs,
        locks=locks,
        idempotency=idempotency,
        runner=runner,
        settings=settings,
        today=today,
    )
    return _decide(
        decision="REJECT",
        trace_id=trace_id,
        body=body,
        principal=principal,
        ctx=ctx,
        audit=audit,
    )


@router.get(
    "/schedule/{trace_id}/export",
    summary="下载本次运行归档的 xlsx",
    response_class=FileResponse,
)
def get_export(
    trace_id: str,
    principal: CurrentPrincipal,
    today: CurrentToday,
) -> FileResponse:
    """取归档产物。**只在归档之后才有文件**（`commit_plan` 写的那个）。

    没有文件时报 FTS-1004 而不是 404 空响应：用户要的下一步很明确
    ——先去人工门禁确认，产物才会生成。
    """
    require_role(principal, "viewer", action="下载排班产物")
    state, _ = read_run_state(trace_id, today=today)
    workbook = state_get(state, "workbook_path", "")
    if not workbook or not Path(workbook).exists():
        raise RequiredInputMissingError(
            f"运行 {trace_id} 还没有可下载的产物",
            details={"trace_id": trace_id, "workbook_path": workbook},
            suggestions=["方案经人工确认归档后才会生成 xlsx（POST /schedule/{id}/approve）"],
        )
    path = Path(workbook)
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


__all__ = ["router"]
