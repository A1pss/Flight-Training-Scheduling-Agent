"""跨周进度锚点（v6 §6.3）。

字段严格照 v6 §6.3 的 DDL，包括 S-11 新增的 `is_recurrent` / `recurrent_since`
两列，以及 v6 给出的主键 `(person_id, mission_id, cycle_start)`。

⚠️ **`last_done_date` 在原始 PDF 里根本不存在这个字段。** 首次排班时它为
NULL 是正常的，由 S-12 在求解侧处理为「窗口从本周周一起算、不计欠账」。
摄取侧既不编造日期填进去，也不因它缺失而报错 —— 写成 `gap=999` 会让基准周
假性不可行，是 `CLAUDE.md` §11 明列的反模式。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base

#: v6 §6.3 的 `status` 取值
PROGRESS_STATUSES = ("NOT_STARTED", "IN_PROGRESS", "COMPLETED")


class TrainingProgress(Base):
    """(人员, 课目, 周期) 三元组的训练进度。"""

    __tablename__ = "training_progress"
    __table_args__ = (
        ForeignKeyConstraint(
            ["person_id", "snapshot_id"],
            ["persons.person_id", "persons.snapshot_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["mission_id", "snapshot_id"],
            ["missions.mission_id", "missions.snapshot_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('NOT_STARTED', 'IN_PROGRESS', 'COMPLETED')", name="progress_status_enum"
        ),
        CheckConstraint("completed_count >= 0", name="progress_completed_count_nonneg"),
        CheckConstraint("debt_count >= 0", name="progress_debt_count_nonneg"),
        CheckConstraint("cycle_weeks > 0", name="progress_cycle_weeks_positive"),
        # S-11：进入复训周期必然有起始日；反之未复训不得留下起始日
        CheckConstraint(
            "(is_recurrent AND recurrent_since IS NOT NULL) "
            "OR (NOT is_recurrent AND recurrent_since IS NULL)",
            name="progress_recurrent_since_consistent",
        ),
    )

    person_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    mission_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    cycle_start: Mapped[date] = mapped_column(Date, primary_key=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: ★ 跨周频率约束的锚点；NULL 走 S-12（不是欠账，是「从本周周一起算」）
    last_done_date: Mapped[date | None] = mapped_column(Date)
    cycle_weeks: Mapped[int] = mapped_column(Integer, nullable=False)
    #: ★ 松弛产生的欠账，下周优先补
    debt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prereq_met: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: 先修未满足时的具体缺失项
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    #: ★ S-11：成熟飞行员的到期资质复训
    is_recurrent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: ★ S-11：进入复训周期的日期（到期次日）
    recurrent_since: Mapped[date | None] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)


__all__ = ["PROGRESS_STATUSES", "TrainingProgress"]
