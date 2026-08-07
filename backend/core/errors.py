"""FTS 错误码与错误契约（v6 §9.3）。

v6 §9.3 定义了 13 个错误码，本模块一个不少地登记，并为每个码固化
「默认严重度 / 所属阶段 / 是否可重试」三个属性——这三者在 v6 的表格里是
散落在「行为」列的散文，落到代码里必须是可判定的字段。

注意 **FTS-2001 的口径在 v6 中已扩展**：由 v5.2 的「数据引用完整性失败」
扩展为「数据引用完整性失败，**或同一数据源内部的值冲突**」，以承载
§1.2.1 刘斌 C 类到期日这类源内冲突（§5.5 X1/X3）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["INFO", "WARN", "ERROR", "CRITICAL"]
Stage = Literal["ingest", "intent", "constraint", "solve", "validate", "export"]


class ErrorCode(StrEnum):
    """v6 §9.3 的 13 个错误码。枚举值即对外契约中的 `code` 字段字面量。"""

    # ── 1xxx 规则与摄取 ───────────────────────────────────────────────
    RULE_PARSE_FAILED = "FTS-1001"
    RULE_SEMANTICS_UNCONFIRMED = "FTS-1002"
    PDF_REPAIR_ASSERTION_FAILED = "FTS-1003"

    # ── 2xxx 数据一致性 ──────────────────────────────────────────────
    DATA_INTEGRITY_OR_CONFLICT = "FTS-2001"

    # ── 3xxx 求解与校验 ──────────────────────────────────────────────
    INFEASIBLE = "FTS-3001"
    SOLVE_TIMEOUT_UNKNOWN = "FTS-3002"
    VALIDATOR_SOLVER_DISAGREE = "FTS-3003"
    SNAPSHOT_STALE_ON_RESUME = "FTS-3004"
    REVISION_INFEASIBLE = "FTS-3005"

    # ── 4xxx LLM 侧 ──────────────────────────────────────────────────
    LLM_UNAVAILABLE = "FTS-4001"
    LLM_SCHEMA_VIOLATION = "FTS-4002"
    HARNESS_BUDGET_EXCEEDED = "FTS-4003"

    # ── 5xxx 产物 ────────────────────────────────────────────────────
    EXPORT_VERIFY_FAILED = "FTS-5001"


class ErrorSpec(BaseModel):
    """单个错误码的固化属性。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ErrorCode
    scenario: str
    behavior: str
    severity: Severity
    stage: Stage
    retryable: bool


#: v6 §9.3 表格的机器可读形态。键集合必须与 :class:`ErrorCode` 完全一致，
#: 由 ``tests/unit/test_errors.py`` 断言。
ERROR_REGISTRY: Final[dict[ErrorCode, ErrorSpec]] = {
    spec.code: spec
    for spec in (
        ErrorSpec(
            code=ErrorCode.RULE_PARSE_FAILED,
            scenario="规则文件解析失败",
            behavior="指出失败条文编号与原文片段；保留旧规则版本继续服务，拒绝启用新版本",
            severity="ERROR",
            stage="constraint",
            retryable=False,
        ),
        ErrorSpec(
            code=ErrorCode.RULE_SEMANTICS_UNCONFIRMED,
            scenario="规则语义歧义未确认",
            behavior=(
                "列出未确认的 semantics.yaml 条目，阻断排班。"
                "S-01~S-13 已全部裁定，本码在当前版本下不应触发；"
                "触发即意味着有人新增了未裁定的开关"
            ),
            severity="ERROR",
            stage="constraint",
            retryable=False,
        ),
        ErrorSpec(
            code=ErrorCode.PDF_REPAIR_ASSERTION_FAILED,
            scenario="PDF 抽取修复层后置断言失败",
            behavior="列出残缺 token 与所在页，阻断入库",
            severity="ERROR",
            stage="ingest",
            retryable=False,
        ),
        ErrorSpec(
            code=ErrorCode.DATA_INTEGRITY_OR_CONFLICT,
            scenario="数据引用完整性失败，或同一数据源内部的值冲突",
            behavior=(
                "指出孤立外键，或冲突的两侧取值（如 §5.5 的 X1/X3）。"
                "上报人工确认环节，按 §5.5 裁定表选定"
            ),
            severity="ERROR",
            stage="ingest",
            retryable=False,
        ),
        ErrorSpec(
            code=ErrorCode.INFEASIBLE,
            scenario="约束不可满足 INFEASIBLE",
            behavior="返回最小冲突集 + 归因 + 松弛提案（§3.9）",
            severity="ERROR",
            stage="solve",
            retryable=False,
        ),
        ErrorSpec(
            code=ErrorCode.SOLVE_TIMEOUT_UNKNOWN,
            scenario="求解超时 UNKNOWN",
            behavior=(
                "明确区分于 FTS-3001；返回当前可行解（若有）并标注非最优，提供「延长时限」选项"
            ),
            severity="WARN",
            stage="solve",
            retryable=True,
        ),
        ErrorSpec(
            code=ErrorCode.VALIDATOR_SOLVER_DISAGREE,
            scenario="校验器与求解器判定分歧",
            behavior=(
                "CRITICAL：不输出结果，附双方判定详情。按 CLAUDE.md §7 第 5 条立刻停下来报告"
            ),
            severity="CRITICAL",
            stage="validate",
            retryable=False,
        ),
        ErrorSpec(
            code=ErrorCode.SNAPSHOT_STALE_ON_RESUME,
            scenario="HITL 恢复时数据快照已变更且影响本方案",
            behavior="拒绝直接批准，强制基于新快照重解，展示前后差异",
            severity="WARN",
            stage="solve",
            retryable=True,
        ),
        ErrorSpec(
            code=ErrorCode.REVISION_INFEASIBLE,
            scenario="多轮修订使问题不可行",
            behavior="回滚至上一版方案，说明冲突项与原始语句，不静默丢弃修订",
            severity="WARN",
            stage="solve",
            retryable=True,
        ),
        ErrorSpec(
            code=ErrorCode.LLM_UNAVAILABLE,
            scenario="LLM 服务不可用",
            behavior=(
                "降级：意图解析退化为规则匹配 + 表单式追问；"
                "排班能力完全不受影响（求解链路不依赖 LLM）"
            ),
            severity="WARN",
            stage="intent",
            retryable=True,
        ),
        ErrorSpec(
            code=ErrorCode.LLM_SCHEMA_VIOLATION,
            scenario="LLM 输出不符 schema",
            behavior="自动重试 2 次（降温 + 强化 schema 提示），仍失败则转人工表单",
            severity="WARN",
            stage="intent",
            retryable=True,
        ),
        ErrorSpec(
            code=ErrorCode.HARNESS_BUDGET_EXCEEDED,
            scenario="单请求超出 Harness 预算（调用数/token/墙钟）",
            behavior="中断并返回已完成部分，提示缩小请求范围",
            severity="WARN",
            stage="intent",
            retryable=False,
        ),
        ErrorSpec(
            code=ErrorCode.EXPORT_VERIFY_FAILED,
            scenario="Excel 写出或回读校验失败",
            behavior="不交付文件，保留中间 JSON",
            severity="ERROR",
            stage="export",
            retryable=False,
        ),
    )
}


class ErrorResponse(BaseModel):
    """v6 §9.3 的对外错误契约。"""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str = Field(min_length=1, description="面向用户的中文说明")
    severity: Severity
    stage: Stage
    details: dict[str, Any] = Field(default_factory=dict, description="结构化上下文")
    suggestions: list[str] = Field(default_factory=list, description="可执行的下一步")
    trace_id: str = Field(min_length=1)
    retryable: bool


# ─────────────────────────────────────────────────────────────────────
# 异常层
# ─────────────────────────────────────────────────────────────────────


class FTSError(Exception):
    """全部 FTS 业务异常的基类。

    每个子类绑定一个 :class:`ErrorCode`，`severity` / `stage` / `retryable`
    默认取 :data:`ERROR_REGISTRY` 中的登记值，构造时可覆盖。
    """

    code: ErrorCode

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        suggestions: list[str] | None = None,
        severity: Severity | None = None,
        stage: Stage | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        spec = ERROR_REGISTRY[self.code]
        self.message = message
        self.details: dict[str, Any] = details or {}
        self.suggestions: list[str] = suggestions or []
        self.severity: Severity = severity or spec.severity
        self.stage: Stage = stage or spec.stage
        self.retryable: bool = spec.retryable if retryable is None else retryable

    def to_response(self, trace_id: str) -> ErrorResponse:
        return ErrorResponse(
            code=self.code,
            message=self.message,
            severity=self.severity,
            stage=self.stage,
            details=self.details,
            suggestions=self.suggestions,
            trace_id=trace_id,
            retryable=self.retryable,
        )


class RuleParseError(FTSError):
    code = ErrorCode.RULE_PARSE_FAILED


class SemanticsUnconfirmedError(FTSError):
    code = ErrorCode.RULE_SEMANTICS_UNCONFIRMED


class IngestionError(FTSError):
    """PDF 抽取修复层后置断言失败（铁律 7：抽取失败绝不静默降级）。"""

    code = ErrorCode.PDF_REPAIR_ASSERTION_FAILED


class DataConflictError(FTSError):
    """引用完整性失败，或同一数据源内部的值冲突（v6 扩展口径）。"""

    code = ErrorCode.DATA_INTEGRITY_OR_CONFLICT


class InfeasibleError(FTSError):
    code = ErrorCode.INFEASIBLE


class SolveTimeoutError(FTSError):
    """UNKNOWN ≠ INFEASIBLE（铁律 8），两者在类型上就分开。"""

    code = ErrorCode.SOLVE_TIMEOUT_UNKNOWN


class ValidatorSolverDisagreementError(FTSError):
    code = ErrorCode.VALIDATOR_SOLVER_DISAGREE


class SnapshotStaleError(FTSError):
    code = ErrorCode.SNAPSHOT_STALE_ON_RESUME


class RevisionInfeasibleError(FTSError):
    code = ErrorCode.REVISION_INFEASIBLE


class LLMUnavailableError(FTSError):
    code = ErrorCode.LLM_UNAVAILABLE


class LLMSchemaError(FTSError):
    code = ErrorCode.LLM_SCHEMA_VIOLATION


class BudgetExceededError(FTSError):
    code = ErrorCode.HARNESS_BUDGET_EXCEEDED


class ExportVerifyError(FTSError):
    code = ErrorCode.EXPORT_VERIFY_FAILED


class EgressDeniedError(FTSError):
    """出网被 `core/http.py` 的 allowlist 拒绝（v6 §11.5 / §12.5.4 E1）。

    egress 拦截属于安全护栏而非业务错误，v6 §9.3 未给它单独的 FTS 码；
    这里复用 FTS-4001（LLM/外部服务不可用）作为对外呈现口径，但保留
    独立的异常类型以便护栏测试精确断言。
    """

    code = ErrorCode.LLM_UNAVAILABLE


__all__ = [
    "ERROR_REGISTRY",
    "BudgetExceededError",
    "DataConflictError",
    "EgressDeniedError",
    "ErrorCode",
    "ErrorResponse",
    "ErrorSpec",
    "ExportVerifyError",
    "FTSError",
    "InfeasibleError",
    "IngestionError",
    "LLMSchemaError",
    "LLMUnavailableError",
    "RevisionInfeasibleError",
    "RuleParseError",
    "SemanticsUnconfirmedError",
    "Severity",
    "SnapshotStaleError",
    "SolveTimeoutError",
    "Stage",
    "ValidatorSolverDisagreementError",
]
