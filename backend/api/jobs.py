"""长任务的状态记录（v6 §9.2 / §8.1）。

## 状态存哪儿，为什么不是 PG

任务状态是**高频写、低价值、可丢**的：轮询 1.5 s 一次，一次排班要写六七次
阶段。把它写 PG 等于给每次排班加几十次事务，而丢了它的后果只是「进度条卡住」
——真正的结果在 checkpoint 与 `plans` 表里，两者都在 PG。所以状态进 Redis，
带 TTL。

## `stage` 与 `status` 分开的理由

见 `backend/schemas/api.py` 的模块注释：阶段说的是走到哪儿，状态说的是死活。
`AWAITING_HUMAN` 是「停在某个阶段等人」，合成一个枚举就没地方放。

## 阶段由节点名推出来，不是猜的

`NODE_STAGE` 是一张**全覆盖**的表：`backend/graph/graph.py::NODE_NAMES` 里的
每个节点都必须在表里有一行，`tests/unit/test_api_jobs.py` 直接拿两个集合比对。
漏一个的后果很具体——那个节点跑完时进度条不动，用户以为卡死了。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from backend.api.store import KEY_PREFIX, KeyValueStore
from backend.core.errors import ErrorCode
from backend.schemas.api import STAGE_PERCENT, JobStage, JobStatus, JobStatusView

#: 任务记录的键前缀。
JOB_KEY_PREFIX: Final[str] = f"{KEY_PREFIX}:job"
#: `trace_id → job_id` 的反查键（`/runs/{trace_id}` 要报出 job_id）。
TRACE_KEY_PREFIX: Final[str] = f"{KEY_PREFIX}:trace"
#: 任务状态保留 7 天。比人工确认「隔天再来」的窗口宽一截即可——真正跨天的
#: 状态在 PG 的 checkpoint 里，这里只是给进度条看的。
JOB_TTL_S: Final[int] = 7 * 24 * 3600

#: 图节点 → 该节点**结束时**所处的阶段（v6 §8.1 六阶段）。
#:
#: `compile_spec` 落在「建模」而不是「加载数据」：它做的两件事里，后一件
#: （把 14 条规则 + S-01~S-13 编成 `ConstraintSpec`）才是耗时的那件，
#: 用户看到「建模」时它确实在建模。数据加载归 `planner` 之后那一格。
NODE_STAGE: Final[dict[str, JobStage]] = {
    "route": JobStage.INTENT,
    "planner": JobStage.LOADING,
    "knowledge": JobStage.REPORTING,
    "compile_spec": JobStage.MODELING,
    "solve": JobStage.SOLVING,
    "diagnosis": JobStage.SOLVING,
    "validate": JobStage.VALIDATING,
    "explain": JobStage.REPORTING,
    "resume_guard": JobStage.REPORTING,
    "human_gate": JobStage.REPORTING,
    "commit_plan": JobStage.REPORTING,
}


@dataclass
class JobRecord:
    """一次提交的全部可轮询状态。"""

    job_id: str
    trace_id: str
    tenant_id: str = "default"
    user_id: str = ""
    kind: str = "schedule"
    status: JobStatus = JobStatus.QUEUED
    stage: JobStage = JobStage.INTENT
    percent: int = 0
    error_code: ErrorCode | None = None
    error_message: str = ""
    finished_at: datetime | None = None
    #: 排班周（ISO 周字符串），锁与历史查询都用它
    iso_week: str = ""
    lock_token: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "trace_id": self.trace_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "kind": self.kind,
            "status": str(self.status),
            "stage": str(self.stage),
            "percent": self.percent,
            "error_code": str(self.error_code) if self.error_code else None,
            "error_message": self.error_message,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "iso_week": self.iso_week,
            "lock_token": self.lock_token,
            "extra": self.extra,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> JobRecord:
        data = json.loads(raw)
        finished = data.get("finished_at")
        code = data.get("error_code")
        return cls(
            job_id=str(data["job_id"]),
            trace_id=str(data["trace_id"]),
            tenant_id=str(data.get("tenant_id", "default")),
            user_id=str(data.get("user_id", "")),
            kind=str(data.get("kind", "schedule")),
            status=JobStatus(data.get("status", JobStatus.QUEUED)),
            stage=JobStage(data.get("stage", JobStage.INTENT)),
            percent=int(data.get("percent", 0)),
            error_code=ErrorCode(code) if code else None,
            error_message=str(data.get("error_message", "")),
            finished_at=datetime.fromisoformat(finished) if finished else None,
            iso_week=str(data.get("iso_week", "")),
            lock_token=str(data.get("lock_token", "")),
            extra=dict(data.get("extra", {})),
        )

    def to_view(self) -> JobStatusView:
        return JobStatusView(
            job_id=self.job_id,
            trace_id=self.trace_id,
            status=self.status,
            stage=self.stage,
            percent=self.percent,
            finished_at=self.finished_at,
            error_code=self.error_code,
        )


class JobStore:
    """任务记录的读写门面。"""

    def __init__(self, store: KeyValueStore, *, ttl_s: int = JOB_TTL_S) -> None:
        self._store = store
        self._ttl = ttl_s

    @staticmethod
    def key(job_id: str) -> str:
        return f"{JOB_KEY_PREFIX}:{job_id}"

    @staticmethod
    def trace_key(trace_id: str) -> str:
        return f"{TRACE_KEY_PREFIX}:{trace_id}"

    def put(self, record: JobRecord) -> JobRecord:
        self._store.set(self.key(record.job_id), record.to_json(), self._ttl)
        self._store.set(self.trace_key(record.trace_id), record.job_id, self._ttl)
        return record

    def get(self, job_id: str) -> JobRecord | None:
        raw = self._store.get(self.key(job_id))
        return JobRecord.from_json(raw) if raw else None

    def job_id_for_trace(self, trace_id: str) -> str | None:
        return self._store.get(self.trace_key(trace_id))

    def get_by_trace(self, trace_id: str) -> JobRecord | None:
        job_id = self.job_id_for_trace(trace_id)
        return self.get(job_id) if job_id else None

    def advance(self, job_id: str, node: str) -> JobRecord | None:
        """按节点名推进阶段。**只前进不后退。**

        修订轮会把图带回 `solve`（阶段倒回「求解」），此时进度条若跟着回退，
        用户看到的是「怎么又退回去了」。倒回去的那一格由 `restart` 显式发起
        （新一轮修订是一次新的提交），常规推进一律单调。
        """
        record = self.get(job_id)
        if record is None:
            return None
        stage = NODE_STAGE.get(node)
        if stage is None:
            return record
        percent = STAGE_PERCENT[stage]
        if percent >= record.percent:
            record.stage = stage
            record.percent = percent
        record.status = JobStatus.RUNNING
        return self.put(record)

    def mark(
        self,
        job_id: str,
        status: JobStatus,
        *,
        percent: int | None = None,
        error_code: ErrorCode | None = None,
        error_message: str = "",
        finished_at: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> JobRecord | None:
        record = self.get(job_id)
        if record is None:
            return None
        record.status = status
        if percent is not None:
            record.percent = percent
        elif status is JobStatus.DONE:
            record.percent = 100
        if error_code is not None:
            record.error_code = error_code
            record.error_message = error_message
        if finished_at is not None:
            record.finished_at = finished_at
        if extra:
            record.extra.update(extra)
        return self.put(record)


__all__ = [
    "JOB_KEY_PREFIX",
    "JOB_TTL_S",
    "NODE_STAGE",
    "TRACE_KEY_PREFIX",
    "JobRecord",
    "JobStore",
]
