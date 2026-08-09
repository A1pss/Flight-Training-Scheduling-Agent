"""审计与追踪表。

`audit_log` 记录「谁在什么时候把什么改成了什么」——摄取的人工确认、松弛档位
授权、计划审批都往这里写。`trace_events` 是 v6 §8.2 过程回放的持久化形态，
与 `backend.schemas.common.TraceEvent` 契约同构。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base

#: 与 `backend.schemas.common.TraceKind` 同一份取值域
TRACE_KINDS = (
    "agent_start",
    "agent_end",
    "reasoning",
    "tool_call",
    "tool_result",
    "decision",
    "constraint_check",
    "solver_stats",
    "handoff",
    "negotiation",
    "error",
    "warning",
    "human_gate",
)


class AuditLog(Base):
    """不可变审计流水。只追加，不更新、不删除。"""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_resource", "resource_type", "resource_id"),)

    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class TraceEventRow(Base):
    """过程回放事件（v6 §8.2）。`(run_id, seq)` 唯一，保证回放顺序确定。"""

    __tablename__ = "trace_events"
    __table_args__ = (
        CheckConstraint("seq >= 0", name="trace_seq_nonneg"),
        CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="trace_duration_nonneg"),
        UniqueConstraint("run_id", "seq", name="uq_trace_events_run_id_seq"),
    )

    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    token_usage: Mapped[dict[str, int] | None] = mapped_column(JSONB)


__all__ = ["TRACE_KINDS", "AuditLog", "TraceEventRow"]
