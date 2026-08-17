"""提交流程的公共部分：`/chat`、`/schedule`、`/approve`、`/reject` 共用。

## 一次提交做了哪五件事（顺序不能换）

```
① 幂等键查表 ── 命中就原样回放上次的响应，不再跑第二次
② 定周      ── 拿不到周就不加锁（问答类请求本来就不碰 training_progress）
③ 加排班锁  ── 拿不到就 FTS-4005 / HTTP 409，**立即拒绝，不排队**
④ 写任务记录 ── 先写 QUEUED，再入队；反过来的话 worker 可能先跑起来、
                 而 JobStore 里还没有这条记录，`advance()` 会静默丢掉进度
⑤ 入队      ── 失败要把 ③ 的锁放掉，否则那一周被一把没人持有的锁锁死到 TTL
```

## 幂等记录为什么写在最后

见 `idempotency.py` 的模块注释：先占位后处理的话，一次失败的提交会把键占死。
这里的顺序是「跑完 → 记住」，所以重试拿到的是**成功那次**的响应。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from backend.api.idempotency import IdempotencyStore
from backend.api.jobs import JobRecord, JobStore
from backend.api.locks import LockHandle, LockManager, iso_week_of
from backend.api.runner import JobRunner
from backend.api.security import Principal
from backend.api.worker import RunPayload
from backend.core.config import Settings
from backend.core.errors import RequiredInputMissingError
from backend.ingestion.loader import active_snapshot_id
from backend.routing.entities import monday_of, resolve_week, week_start_of
from backend.schemas.api import JobStatus, JobSubmitView

API_PREFIX = "/api/v1"

#: 句子里可能出现的「周表述」形态。`resolve_week` 要的是**一个表述**，
#: 而用户给的是一整句话 —— 先把候选切出来，再交给它逐个试。
#: 相对表述（「下周」「上上周」）走整句匹配，`resolve_week` 内部是子串命中。
_WEEK_TOKEN = re.compile(r"\d{4}-?[Ww]\d{1,2}|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日")


def _week_surfaces(message: str) -> list[str]:
    """整句 + 句中所有像「周/日期」的片段。整句放最后，让显式编号优先。"""
    return [*_WEEK_TOKEN.findall(message), message]


def poll_url(job_id: str) -> str:
    return f"{API_PREFIX}/jobs/{job_id}"


def require_snapshot(session: Session, requested: str | None) -> str:
    """定快照：请求指定的优先，否则取库里 ACTIVE 的。

    **两个都没有就抛 FTS-1004**，不静默造一个（§5.1.1「缺输入即提问」）。
    """
    snapshot = requested or active_snapshot_id(session)
    if not snapshot:
        raise RequiredInputMissingError(
            "库里没有 ACTIVE 数据快照，也没有在请求里指定 snapshot_id",
            details={"resolution": "upload"},
            suggestions=["先上传人员/飞机/课目/规则四份数据并确认入库（POST /api/v1/ingest）"],
        )
    return snapshot


def resolve_week_start(
    *, explicit: date | None, message: str, today: date
) -> tuple[date | None, str]:
    """定排班周。返回 `(周一, ISO 周)`；定不了就 `(None, "")`。

    三条来源，**没有第四条**（不猜「大概是下周吧」）：

    1. 请求里显式给的 `week_start`（取其所在周的周一）；
    2. 自然语言里能确定性解析出来的周表述（`resolve_week`，规则代码，不走 LLM）；
    3. 都没有 → `(None, "")`。此时**不加锁**：问答类请求不碰排班资源，
       而真要排班却没说哪一周的，`compile_spec` 会按 FTS-1004 挡回来并追问。
    """
    if explicit is not None:
        monday = monday_of(explicit)
        return monday, iso_week_of(monday)
    for surface in _week_surfaces(message):
        resolution = resolve_week(surface, today=today)
        if resolution.entity_id:
            return week_start_of(resolution.entity_id), resolution.entity_id
    return None, ""


@dataclass
class SubmitContext:
    """一次提交要用到的全部依赖（避免十个位置参数）。"""

    jobs: JobStore
    locks: LockManager
    idempotency: IdempotencyStore
    runner: JobRunner
    settings: Settings
    today: date


def submit_run(
    ctx: SubmitContext,
    *,
    scope: str,
    idem_token: str,
    principal: Principal,
    tenant_id: str,
    kind: str,
    snapshot_id: str,
    trace_id: str | None = None,
    message: str = "",
    week_start: date | None = None,
    iso_week: str = "",
    relaxation_tier: int = 0,
    use_llm: bool = True,
    decision: dict[str, Any] | None = None,
) -> JobSubmitView:
    """把一次运行交出去。见模块注释的五步。"""
    cached = ctx.idempotency.lookup(scope, tenant_id, idem_token)
    if cached is not None:
        return JobSubmitView.model_validate({**cached, "idempotent_hit": True})

    run_trace_id = trace_id or uuid.uuid4().hex
    job_id = uuid.uuid4().hex

    lock: LockHandle | None = None
    if iso_week:
        lock = ctx.locks.acquire_schedule(tenant_id, iso_week, holder=principal.user_id)

    record = JobRecord(
        job_id=job_id,
        trace_id=run_trace_id,
        tenant_id=tenant_id,
        user_id=principal.user_id,
        kind=kind,
        status=JobStatus.QUEUED,
        iso_week=iso_week,
        lock_token=lock.token if lock else "",
        # 快照记在任务上：`/approve` 与 `/runs` 要按**这次运行当初用的那个快照**
        # 装名录，而不是「此刻 ACTIVE 的那个」——两者在数据更新之后不是一回事，
        # 而快照是否已过期由图里的 `resume_guard` 判（FTS-3004），不是 API 层
        extra={"snapshot_id": snapshot_id},
    )
    ctx.jobs.put(record)

    payload = RunPayload(
        job_id=job_id,
        trace_id=run_trace_id,
        kind="resume" if decision else "start",
        tenant_id=tenant_id,
        user_id=principal.user_id,
        user_role=principal.role,
        message=message,
        snapshot_id=snapshot_id,
        week_start=week_start.isoformat() if week_start else "",
        today=ctx.today.isoformat(),
        iso_week=iso_week,
        lock_token=lock.token if lock else "",
        use_llm=use_llm,
        relaxation_tier=relaxation_tier,
        decision=decision or {},
        plans_root=str(ctx.settings.PLANS_DIR),
    )
    try:
        ctx.runner.submit(payload)
    except Exception:
        # 入队失败 → 把锁放掉。不放的话那一周被一把没有主人的锁锁死到 TTL 过期
        ctx.locks.release(lock)
        ctx.jobs.mark(job_id, JobStatus.FAILED, error_message="任务入队失败")
        raise

    current = ctx.jobs.get(job_id) or record
    view = JobSubmitView(
        job_id=job_id,
        trace_id=run_trace_id,
        status=current.status,
        idempotent_hit=False,
        poll_url=poll_url(job_id),
    )
    ctx.idempotency.remember(scope, tenant_id, idem_token, view.model_dump(mode="json"))
    return view


__all__ = [
    "API_PREFIX",
    "SubmitContext",
    "poll_url",
    "require_snapshot",
    "resolve_week_start",
    "submit_run",
]
