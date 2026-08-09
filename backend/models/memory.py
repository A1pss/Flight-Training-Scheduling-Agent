"""长期记忆表（v6 §6.2 / §6.4）。

**§6.4 是这两张表存在的全部理由**：长期记忆最大的坑不是「召回不到」，而是
「召回到过期版本」—— 召回到过期资质会直接导致排班违规。所以每条记忆都带
`valid_from` / `valid_to` / `superseded_by`，检索默认加时间过滤。

刘斌的 C 类资质就是这条机制的活样本：2026-01-07 之前是「有效资质」，之后是
「到期待复训」，两个日期查同一个问题必须给出不同答案。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base

#: 情景记忆的事件类型
EPISODIC_KINDS = (
    "schedule_session",
    "user_revision",
    "user_rejection",
    "conflict_resolution",
    "relaxation_choice",
    "approval",
)


class EpisodicMemory(Base):
    """情景记忆：历次排班会话、用户修改与驳回、当时的冲突与所选松弛档。

    `chroma_doc_id` 指向 Chroma 中该条摘要的向量文档；PG 存权威内容，
    Chroma 只存摘要向量（v6 §6.2「PG + Chroma（摘要向量）」）。
    """

    __tablename__ = "episodic_memories"
    __table_args__ = (
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="episodic_validity_ordered"
        ),
        Index("ix_episodic_memories_validity", "valid_from", "valid_to"),
    )

    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("episodic_memories.memory_id", ondelete="SET NULL")
    )
    #: 超过 3 个训练周期后归档到冷表语义：置 True 后不参与默认召回（§6.4 遗忘策略）
    archived: Mapped[bool] = mapped_column(nullable=False, default=False)
    chroma_doc_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProceduralMemory(Base):
    """程序记忆：常用表述映射、偏好的松弛顺序、教员排班习惯。

    从情景记忆定期蒸馏而来，按 `namespace` + `key` 前缀检索。
    """

    __tablename__ = "procedural_memories"
    __table_args__ = (
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from", name="procedural_validity_ordered"
        ),
        UniqueConstraint(
            "namespace", "key", "valid_from", name="uq_procedural_memories_namespace_key_valid_from"
        ),
        Index("ix_procedural_memories_lookup", "namespace", "key", "valid_from"),
    )

    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: 来源可信度排序用（PG 事实 > 排班确认记录 > 对话推断，§6.4）
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="对话推断")

    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("procedural_memories.memory_id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["EPISODIC_KINDS", "EpisodicMemory", "ProceduralMemory"]
