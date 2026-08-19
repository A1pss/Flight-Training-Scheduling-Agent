"""审计与追踪表。

`audit_log` 记录「谁在什么时候从哪台机器把什么改成了什么」——摄取的人工确认、
松弛档位授权、计划审批、以及全部 POST 端点都往这里写（v6 §11.5「审计」）。
写入口统一在 :mod:`backend.core.audit`。`trace_events` 是 v6 §8.2 过程回放的持久化形态，
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
    #: 操作来源 IP（v6 §11.5「审计」四要素之一，M8 补）。取 `request.client.host`，
    #: **不采信 `X-Forwarded-For`** —— 那个头客户端能随便写，采信它等于让审计可伪造。
    #: 非 HTTP 入口（CLI 摄取、worker 内部）写空串，不写 `"local"` 之类的假值。
    actor_ip: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: `before` → `after` 的顶层键差异，写入当时算好（`backend.core.audit.value_diff`）。
    #: 形式上冗余于前两列，存它是为了「管理员不必自己对着两坨 JSON 找不同」，
    #: 且日后算法改了也不会改写历史结论。
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
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
