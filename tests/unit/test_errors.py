"""错误码与错误契约单测（v6 §9.3）。

核心断言：**16 个码一个不少**，且 `UNKNOWN` 与 `INFEASIBLE` 在类型层就分开
（铁律 8）。

⚠️ **第 16 个码 `FTS-4005` 是 M6 新增的**（`Z-24`，业务方 2026-08-18 裁定）：
v6 §9.2 要求 `(tenant, week)` 加分布式锁，而 §9.3 原表里没有「锁被别人持有」
这一项。复用 `FTS-3004`（快照陈旧）会让前端提示成「数据变了，要重解」——
真实情况是「数据没变，有人在排」，下一步动作完全不同。
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
    ScheduleLockedError,
    SolveTimeoutError,
    ValidatorSolverDisagreementError,
)

#: v6 §9.3 表格逐字列出的 16 个码（15 条原表 + M6 新增的 FTS-4005）。
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
    "FTS-4004",
    "FTS-4005",
    "FTS-5001",
}


def test_all_sixteen_codes_present() -> None:
    assert {c.value for c in ErrorCode} == EXPECTED_CODES
    assert len(ErrorCode) == 16


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


def test_fts_4005_is_a_retryable_warning_not_a_stale_snapshot() -> None:
    """`Z-24`：锁冲突是「等一会儿再来」，不是「数据变了要重解」。

    两件事的下一步动作不同，所以不许合成一个码 —— 这条断言钉住的正是
    「别人图省事把它塞回 FTS-3004」。
    """
    spec = ERROR_REGISTRY[ErrorCode.SCHEDULE_LOCKED]
    assert spec.severity == "WARN"
    assert spec.retryable is True
    assert spec.code != ErrorCode.SNAPSHOT_STALE_ON_RESUME
    err = ScheduleLockedError(
        "2026W02 正在被 P01 排班",
        details={"lock_key": "default:2026W02", "holder": "P01", "ttl_s": 47},
    )
    assert err.to_response(trace_id="t").code == ErrorCode.SCHEDULE_LOCKED


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
