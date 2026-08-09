"""错误码与错误契约单测（v6 §9.3）。

核心断言：**14 个码一个不少**，且 `UNKNOWN` 与 `INFEASIBLE` 在类型层就分开
（铁律 8）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.core.errors import (
    ERROR_REGISTRY,
    DataConflictError,
    ErrorCode,
    ErrorResponse,
    FTSError,
    InfeasibleError,
    IngestionError,
    SolveTimeoutError,
    ValidatorSolverDisagreementError,
)

#: v6 §9.3 表格逐字列出的 14 个码。
EXPECTED_CODES = {
    "FTS-1001",
    "FTS-1002",
    "FTS-1003",
    "FTS-1004",
    "FTS-2001",
    "FTS-3001",
    "FTS-3002",
    "FTS-3003",
    "FTS-3004",
    "FTS-3005",
    "FTS-4001",
    "FTS-4002",
    "FTS-4003",
    "FTS-5001",
}


def test_all_thirteen_codes_present() -> None:
    assert {c.value for c in ErrorCode} == EXPECTED_CODES
    assert len(ErrorCode) == 14


def test_registry_covers_every_code() -> None:
    """登记表不许有缺口——漏一个码，错误响应就会 KeyError 崩在生产上。"""
    assert set(ERROR_REGISTRY) == set(ErrorCode)


def test_fts_2001_scope_extended_in_v6() -> None:
    """FTS-2001 在 v6 中扩展为「引用完整性失败**或源内值冲突**」。"""
    spec = ERROR_REGISTRY[ErrorCode.DATA_INTEGRITY_OR_CONFLICT]
    assert "值冲突" in spec.scenario


def test_fts_3003_is_critical() -> None:
    """求解器与校验器分歧是 CRITICAL，必须停下来报告（CLAUDE.md §7 第 5 条）。"""
    assert ERROR_REGISTRY[ErrorCode.VALIDATOR_SOLVER_DISAGREE].severity == "CRITICAL"


def test_unknown_is_not_infeasible() -> None:
    """铁律 8：三态分离，在异常类型与错误码上都不得混同。"""
    assert InfeasibleError.code is ErrorCode.INFEASIBLE
    assert SolveTimeoutError.code is ErrorCode.SOLVE_TIMEOUT_UNKNOWN
    assert InfeasibleError.code != SolveTimeoutError.code
    assert not issubclass(SolveTimeoutError, InfeasibleError)
    # UNKNOWN 可重试（延长时限），INFEASIBLE 不可
    assert ERROR_REGISTRY[ErrorCode.SOLVE_TIMEOUT_UNKNOWN].retryable is True
    assert ERROR_REGISTRY[ErrorCode.INFEASIBLE].retryable is False


def test_error_defaults_from_registry() -> None:
    err = IngestionError("检出脏 token: sionB-1", details={"page": 3})
    assert err.code == ErrorCode.PDF_REPAIR_ASSERTION_FAILED
    assert err.severity == "ERROR"
    assert err.stage == "ingest"
    assert err.retryable is False
    assert err.details == {"page": 3}


def test_to_response_roundtrip() -> None:
    err = DataConflictError(
        "刘斌 C 类到期日冲突：总表 2026-01-07 / 明细表 2026-02-07",
        details={"person_id": "P04", "total": "2026-01-07", "detail": "2026-02-07"},
        suggestions=["按 SPEC_DECISIONS §C.1 取总表 2026-01-07"],
    )
    resp = err.to_response(trace_id="abc123")
    assert isinstance(resp, ErrorResponse)
    assert resp.code == ErrorCode.DATA_INTEGRITY_OR_CONFLICT
    assert resp.trace_id == "abc123"
    assert resp.severity == "ERROR"
    assert resp.suggestions


def test_error_response_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        ErrorResponse(
            code=ErrorCode.INFEASIBLE,
            message="x",
            severity="ERROR",
            stage="solve",
            trace_id="t",
            retryable=False,
            bogus=1,  # type: ignore[call-arg]
        )


def test_error_response_requires_nonempty_message() -> None:
    with pytest.raises(ValidationError):
        ErrorResponse(
            code=ErrorCode.INFEASIBLE,
            message="",
            severity="ERROR",
            stage="solve",
            trace_id="t",
            retryable=False,
        )


def test_severity_override() -> None:
    err = ValidatorSolverDisagreementError("分歧", severity="CRITICAL")
    assert err.severity == "CRITICAL"


def test_all_errors_subclass_ftserror() -> None:
    for cls in (
        IngestionError,
        DataConflictError,
        InfeasibleError,
        SolveTimeoutError,
        ValidatorSolverDisagreementError,
    ):
        assert issubclass(cls, FTSError)
        assert cls.code in ERROR_REGISTRY
