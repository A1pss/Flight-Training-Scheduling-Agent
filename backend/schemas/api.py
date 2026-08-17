"""对外 API 契约（v6 §9.1 的 11 个端点）。

放在 `schemas/` 而不是 `api/` 是刻意的：**契约对外冻结**，而路由实现会随时间变。
前端（`frontend/`）与契约测试（`tests/contract/`）都直接 import 本模块，于是
「后端改了字段、前端不知道」这件事在 mypy 与契约测试两侧同时暴露。

## 三个必须一眼看清的形状

1. **`JobStatusView` 必须小。** v6 §8.1：轮询 1.5 s 一次，只取「阶段枚举 +
   百分比 + 状态」，响应体保持在**几百字节**。所以它里面没有方案、没有校验
   报告、没有事件明细——那些一律走 `GET /runs/{trace_id}` 一次性取回。
   `tests/unit/test_api_contracts.py` 有一条断言直接量它的字节数。

2. **`RunResultView` 必须全。** 出口标准「回放完整性 = 100%」的判据就在这里：
   `trace_events` 是**全量**，`seq` 必须是 `0..n-1` 的连续整数，前端的步进
   slider 才能覆盖到每一步。缺一条就不是 100%。

3. **六个阶段枚举与 v6 §8.1 那张图逐字对应**（解析意图 / 加载数据 / 建模 /
   求解 / 校验 / 生成报表），**状态另算一个字段**——「阶段」说的是走到哪儿了，
   「状态」说的是这次运行是死是活，把它们并成一个枚举会让 `AWAITING_HUMAN`
   这种「停在某个阶段等人」的情况无处安放。
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.core.errors import ErrorCode, Severity, Stage
from backend.schemas.common import TraceEvent
from backend.schemas.plan import SchedulePlan
from backend.schemas.retrieval import GroundingReport
from backend.schemas.solver import ConflictItem, RelaxationProposal, SolverStats
from backend.schemas.validation import SchemaCheckReport, ValidationReport

#: 人工决策授权所需的最低角色。`viewer` 只能看。
Role = Literal["viewer", "scheduler", "director", "admin"]


class JobStage(StrEnum):
    """v6 §8.1 的六个粗粒度阶段。**只有六个，不许加第七个。**

    加一个阶段就意味着前端的进度条要跟着改，而 v6 §8.1 把「轮询只返回粗粒度
    阶段」当成设计约束（它换来的是几百字节的响应体与可忽略的 rerun 开销）。
    要看细节的地方是 `GET /runs/{trace_id}` 的 `trace_events`。
    """

    INTENT = "解析意图"
    LOADING = "加载数据"
    MODELING = "建模"
    SOLVING = "求解"
    VALIDATING = "校验"
    REPORTING = "生成报表"


class JobStatus(StrEnum):
    """运行状态。与阶段正交：`AWAITING_HUMAN` 是「停在某个阶段等人」。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    DONE = "DONE"
    FAILED = "FAILED"


#: 阶段 → 完成百分比。**这不是猜的进度条**：每个值就是「走完这个阶段时的位置」，
#: 由 `backend/api/worker.py` 在节点结束时按名字查表写入，不做插值动画。
STAGE_PERCENT: dict[JobStage, int] = {
    JobStage.INTENT: 10,
    JobStage.LOADING: 25,
    JobStage.MODELING: 40,
    JobStage.SOLVING: 65,
    JobStage.VALIDATING: 80,
    JobStage.REPORTING: 92,
}


class ErrorView(BaseModel):
    """v6 §9.3 `ErrorResponse` 的对外形态。

    与 `backend.core.errors.ErrorResponse` 同形，但**独立定义**：那个是异常层
    的产物（`FTSError.to_response`），这个是 HTTP 层的契约。两者由
    `tests/unit/test_api_contracts.py::test_error_view_matches_error_response`
    钉住字段一致，谁改了另一边就红。
    """

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str = Field(min_length=1)
    severity: Severity
    stage: Stage
    details: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)
    trace_id: str = Field(min_length=1)
    retryable: bool


# ─────────────────────────────────────────────────────────────────────
# 摄取（3 个端点）
# ─────────────────────────────────────────────────────────────────────


class IngestSubmitView(BaseModel):
    """`POST /api/v1/ingest` 的响应。幂等键 = 文件 SHA256。"""

    model_config = ConfigDict(extra="forbid")

    ingest_job_id: str = Field(min_length=1)
    #: 上传文件的 SHA256（幂等键本身）。同一份文件重传拿到同一个 id
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: True = 命中幂等键，本次没有重新解析
    idempotent_hit: bool = False
    filenames: list[str] = Field(default_factory=list)
    trace_id: str = Field(min_length=1)


class ConflictView(BaseModel):
    """§5.5 的源内数据冲突条目（X1/X3），交人工裁定。"""

    model_config = ConfigDict(extra="forbid")

    conflict_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    options: dict[str, str] = Field(default_factory=dict, description="取值 → 出处")
    adjudication: str | None = Field(default=None, description="§5.5 裁定表给出的取值")
    blocking: bool = True


class OpenQuestionView(BaseModel):
    """§5.1.1「缺输入即提问」的待澄清问题（FTS-1004）。"""

    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    #: `answer` = 给一个值即可；`upload` = 必须补传整份文件。**没有第三种**
    resolution: Literal["answer", "upload"]
    detail: str = ""


class ChangeItemView(BaseModel):
    """Diff 里的一条变更。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["added", "modified", "removed"]
    table: str = Field(min_length=1)
    key: str = Field(min_length=1)
    fields: dict[str, Any] = Field(default_factory=dict)


class ChangeSetView(BaseModel):
    """`GET /api/v1/ingest/{id}/changeset` 的响应（含 §5.5 冲突项）。"""

    model_config = ConfigDict(extra="forbid")

    ingest_job_id: str = Field(min_length=1)
    base_snapshot_id: str | None = None
    summary: dict[str, int] = Field(default_factory=dict)
    changes: list[ChangeItemView] = Field(default_factory=list)
    conflicts: list[ConflictView] = Field(default_factory=list)
    open_questions: list[OpenQuestionView] = Field(default_factory=list)
    #: 三层格式校验之前的一层：抽取期后置断言的结果
    extraction_ok: bool = True


class IngestConfirmRequest(BaseModel):
    """`POST /api/v1/ingest/{id}/confirm`。

    `resolutions` / `answers` **不给默认值**：§5.1.1 明令「缺什么问什么，不设
    静默默认值」。冲突没裁完、问题没答完就确认，服务端按 FTS-1004 / FTS-2001 挡回。
    """

    model_config = ConfigDict(extra="forbid")

    approver: str = Field(min_length=1)
    resolutions: dict[str, str] = Field(default_factory=dict, description="冲突 id → 选定取值")
    answers: dict[str, str] = Field(default_factory=dict, description="问题 id → 回答")
    comment: str = ""


class IngestConfirmView(BaseModel):
    """确认后的落库结果。"""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1)
    table_counts: dict[str, int] = Field(default_factory=dict)
    vector_counts: dict[str, int] = Field(default_factory=dict)
    applied_resolutions: dict[str, str] = Field(default_factory=dict)
    idempotent_hit: bool = False


# ─────────────────────────────────────────────────────────────────────
# 提交（chat / schedule）
# ─────────────────────────────────────────────────────────────────────


class ChatRequest(BaseModel):
    """`POST /api/v1/chat` —— 统一自然语言入口。幂等键 = 客户端 UUID。"""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    #: 客户端生成的 UUID。**同一个 UUID 重复提交返回同一个 job_id**
    client_request_id: str = Field(min_length=1, max_length=64)
    #: 续接某次运行（修订轮）。为空即开新会话
    thread_id: str | None = None
    snapshot_id: str | None = None
    week_start: date | None = None


class ScheduleRequest(BaseModel):
    """`POST /api/v1/schedule` —— 结构化排班入口。

    **它同时是 FTS-4001 的降级路径**（v6 §9.3 脚注：LLM 挂了，排班能力必须还在）。
    所以这里的字段全是结构化的，一个自然语言字段都没有——走这条路不碰 LLM。
    """

    model_config = ConfigDict(extra="forbid")

    week_start: date = Field(description="排班周周一")
    client_request_id: str | None = Field(
        default=None, max_length=64, description="不给就按请求体内容算幂等键"
    )
    snapshot_id: str | None = None
    relaxation_tier: int = Field(default=0, ge=0, le=3)
    #: 只排这些人/机（空 = 全量）。**不是过滤器，是求解范围**
    person_ids: list[str] = Field(default_factory=list)
    aircraft_ids: list[str] = Field(default_factory=list)


class JobSubmitView(BaseModel):
    """提交类端点的统一响应：立即返回 `job_id`（v6 §9.2）。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    status: JobStatus
    idempotent_hit: bool = False
    poll_url: str = Field(min_length=1)


class JobStatusView(BaseModel):
    """`GET /api/v1/jobs/{job_id}` —— **轮询专用，必须小**（v6 §8.1）。

    没有方案、没有校验报告、没有事件明细。要那些就去 `GET /runs/{trace_id}`。
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    status: JobStatus
    stage: JobStage
    percent: int = Field(ge=0, le=100)
    #: 完成/失败的时刻，用于前端显示耗时。未完成时为 None
    finished_at: datetime | None = None
    #: 失败时的错误码（详情去 `/runs`）。**不塞完整 ErrorView**，那会撑大响应体
    error_code: ErrorCode | None = None


# ─────────────────────────────────────────────────────────────────────
# 完整结果
# ─────────────────────────────────────────────────────────────────────


class SolverPanelView(BaseModel):
    """v6 §8.2 求解面板的八个数 + 跑道分配统计。"""

    model_config = ConfigDict(extra="forbid")

    stats: SolverStats | None = None
    #: 跑道 → 架次数。§8.2 求解面板最后一格
    runway_allocation: dict[str, int] = Field(default_factory=dict)


class GatePayloadView(BaseModel):
    """人工门禁这一屏在问什么（v6 §7.2.4 两种问法，`Z-19`）。

    `pending_revision=True` 时问的是「我把你那句话理解成这样，对不对」，
    `APPROVE` 的含义是**去重解**而不是去归档。前端据这两个字段换文案与按钮语义。
    """

    model_config = ConfigDict(extra="forbid")

    awaiting: bool = False
    pending_revision: bool = False
    revision_echo: str = ""
    relaxation_tier: int = Field(default=0, ge=0, le=3)
    open_questions: list[str] = Field(default_factory=list)
    ambiguities: list[dict[str, Any]] = Field(default_factory=list)


#: 带 computed field 的嵌套模型 —— 回构前要把那几个派生字段剔掉，
#: 见 `RunResultView.from_payload` 的说明。
_COMPUTED_NESTED: dict[str, type[BaseModel]] = {
    "validation": ValidationReport,
    "grounding": GroundingReport,
}


class RunResultView(BaseModel):
    """`GET /api/v1/runs/{trace_id}` —— 方案 + 校验报告 + TraceEvent 全量。

    **`trace_events` 是全量且 `seq` 连续**，这是「回放完整性 = 100%」的定义。
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(min_length=1)
    job_id: str | None = None
    status: JobStatus
    stage: JobStage
    intent: str | None = None
    snapshot_id: str | None = None
    ruleset_version: str | None = None
    semantics_version: str | None = None
    plan: SchedulePlan | None = None
    validation: ValidationReport | None = None
    schema_check: SchemaCheckReport | None = None
    solver: SolverPanelView = Field(default_factory=SolverPanelView)
    explanation: str | None = None
    grounding: GroundingReport | None = None
    conflicts: list[ConflictItem] = Field(default_factory=list)
    relaxation_proposals: list[RelaxationProposal] = Field(default_factory=list)
    errors: list[ErrorView] = Field(default_factory=list)
    gate: GatePayloadView = Field(default_factory=GatePayloadView)
    trace_events: list[TraceEvent] = Field(default_factory=list)
    workbook_path: str | None = None
    committed_plan_id: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> RunResultView:
        """从 `GET /runs/{trace_id}` 的 JSON 回构。**别直接 `model_validate`。**

        ## 这不是防御性编程，是一个实测踩到的坑（第二次）

        `ValidationReport` / `GroundingReport` 带 **computed field**（`all_passed`、
        `total_checked_items`、`supported_ratio`、`unsupported_claims`），
        序列化时它们会被吐出来；而两个模型都是 `extra="forbid"`，回构时那几个
        字段就成了「多余的字段」，Pydantic 当场拒绝：

        ```
        ValidationError: 2 validation errors for RunResultView
        validation.all_passed        Extra inputs are not permitted
        validation.total_checked_items  Extra inputs are not permitted
        ```

        `backend/graph/state.py::model_get` 的 docstring 记的是**同一族**问题
        （那次是 checkpoint 往返）。这次是 HTTP 往返：前端把响应回构成
        `RunResultView` 时炸在页面上 —— E2E 探路时照出来的。

        计算字段是**派生**的，扔掉再重算即可，不会丢信息。
        """
        data = dict(payload)
        for key, model in _COMPUTED_NESTED.items():
            value = data.get(key)
            if isinstance(value, dict):
                data[key] = {
                    field: item
                    for field, item in value.items()
                    if field not in model.model_computed_fields
                }
        return cls.model_validate(data)

    def replay_complete(self) -> bool:
        """回放完整性：`seq` 恰好是 `0..n-1`，一条不缺、不重。

        **空事件列表返回 False** —— 那是「没跑」，不是「跑完了没事件」。
        """
        seqs = [e.seq for e in self.trace_events]
        return bool(seqs) and sorted(seqs) == list(range(len(seqs)))


# ─────────────────────────────────────────────────────────────────────
# 人工决策与产物
# ─────────────────────────────────────────────────────────────────────


class DecisionRequest(BaseModel):
    """`approve` / `reject` 的请求体。

    `authorized_tiers` 在这里是**真的授权**（v6 §7.2.4）：Planner 那一步只做了
    角色够不够格的预筛，这一步是训练主任本人按下的确认。
    """

    model_config = ConfigDict(extra="forbid")

    comment: str = ""
    authorized_tiers: list[int] = Field(default_factory=list)
    client_request_id: str | None = Field(default=None, max_length=64)


class DecisionView(BaseModel):
    """决策提交后的响应：图从断点继续，仍然是异步的。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    decision: Literal["APPROVE", "REJECT", "REVISE"]
    status: JobStatus
    idempotent_hit: bool = False
    poll_url: str = Field(min_length=1)


class PlanSummaryView(BaseModel):
    """`GET /api/v1/plans?week=...` 的一行。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1)
    iso_week: str = Field(pattern=r"^\d{4}W\d{2}$")
    plan_version: int = Field(ge=1)
    week_start: date
    week_end: date
    status: str = Field(min_length=1)
    relax_tier: int = Field(ge=0, le=3)
    sorties: int = Field(ge=0)
    snapshot_id: str = Field(min_length=1)
    ruleset_version: str = Field(min_length=1)
    semantics_version: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    approved_by: str | None = None


class PlanListView(BaseModel):
    """历史计划查询结果。"""

    model_config = ConfigDict(extra="forbid")

    week: str | None = None
    plans: list[PlanSummaryView] = Field(default_factory=list)


class HealthView(BaseModel):
    """存活探针。**不做外部依赖检查**——健康检查连库会让一次库抖动放大成服务不可用。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    app_env: str = Field(min_length=1)
    ruleset_version: str = Field(min_length=1)
    semantics_version: str = Field(min_length=1)
    #: 全离线运行标识，前端顶栏那个 ● 就是它
    offline: bool = True


__all__ = [
    "STAGE_PERCENT",
    "ChangeItemView",
    "ChangeSetView",
    "ChatRequest",
    "ConflictView",
    "DecisionRequest",
    "DecisionView",
    "ErrorView",
    "GatePayloadView",
    "HealthView",
    "IngestConfirmRequest",
    "IngestConfirmView",
    "IngestSubmitView",
    "JobStage",
    "JobStatus",
    "JobStatusView",
    "JobSubmitView",
    "OpenQuestionView",
    "PlanListView",
    "PlanSummaryView",
    "Role",
    "RunResultView",
    "ScheduleRequest",
    "SolverPanelView",
]
