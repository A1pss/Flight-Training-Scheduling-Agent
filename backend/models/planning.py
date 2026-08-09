"""排班产物表：计划 / 架次 / 机组 / 欠账 / 阻塞项。

M1 只建表 —— 写入方是 M2 的 `commit_plan` 节点与 M3 的报告层。表结构与
v6 附录 B 的 `SchedulePlan` / `Sortie` / `CrewMember` / `TrainingDebt` /
`BlockedItem` 契约一一对应，字段取值域用 CHECK 约束钉死，避免契约与库
两头漂移。

**铁律 8 在库层的落点**：`plans.status` 的取值域里 `UNKNOWN` 与 `INFEASIBLE`
是两个不同的字面量，任何把两者归一的写法都过不了 CHECK。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base

#: 与 `backend.schemas.solver.SolveStatus` 同一份取值域
SOLVE_STATUSES = ("OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN", "MODEL_INVALID")
#: 与 `backend.schemas.plan.CrewRole` 同一份取值域（★「复训」是 S-11 新增）
CREW_ROLES = ("教员", "学员", "单飞", "复训")
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class Plan(Base):
    """一次排班的计划头。"""

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('OPTIMAL', 'FEASIBLE', 'INFEASIBLE', 'UNKNOWN', 'MODEL_INVALID')",
            name="plan_status_enum",
        ),
        CheckConstraint("relax_tier BETWEEN 0 AND 3", name="plan_relax_tier_range"),
        CheckConstraint("week_start <= week_end", name="plan_week_ordered"),
        UniqueConstraint("iso_week", "plan_version", name="uq_plans_iso_week_plan_version"),
    )

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    iso_week: Mapped[str] = mapped_column(String(8), nullable=False)
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    week_end: Mapped[date] = mapped_column(Date, nullable=False)

    snapshot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("data_snapshots.snapshot_id", ondelete="RESTRICT"), nullable=False
    )
    ruleset_version: Mapped[str] = mapped_column(
        String(32), ForeignKey("rulesets.ruleset_version", ondelete="RESTRICT"), nullable=False
    )
    semantics_version: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("semantics_versions.semantics_version", ondelete="RESTRICT"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    relax_tier: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seed: Mapped[int] = mapped_column(Integer, nullable=False, default=42)
    solver_stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: 计划内容的 sha256，铁律 9 的逐字节可复现由它验证
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Sortie(Base):
    """单个架次。"""

    __tablename__ = "sorties"
    __table_args__ = (
        CheckConstraint(
            "weekday IN ('周一', '周二', '周三', '周四', '周五', '周六', '周日')",
            name="sortie_weekday_enum",
        ),
        CheckConstraint("takeoff < landing", name="sortie_time_ordered"),
        CheckConstraint("runway_id IN ('RWY-1', 'RWY-2')", name="sortie_runway_enum"),
        # 同一飞机在同一时刻只能起飞一次（约束7 的一个必要条件，库层兜一道）
        UniqueConstraint(
            "plan_id", "aircraft_id", "flight_date", "takeoff", name="uq_sorties_aircraft_slot"
        ),
    )

    sortie_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("plans.plan_id", ondelete="CASCADE"), nullable=False, index=True
    )
    flight_date: Mapped[date] = mapped_column(Date, nullable=False)
    weekday: Mapped[str] = mapped_column(String(4), nullable=False)
    takeoff: Mapped[time] = mapped_column(Time, nullable=False)
    landing: Mapped[time] = mapped_column(Time, nullable=False)
    mission_id: Mapped[str] = mapped_column(String(16), nullable=False)
    mission_name: Mapped[str] = mapped_column(String(32), nullable=False)
    airspace_id: Mapped[str] = mapped_column(String(8), nullable=False)
    aircraft_id: Mapped[str] = mapped_column(String(8), nullable=False)
    runway_id: Mapped[str] = mapped_column(String(8), nullable=False)
    #: S-11 复训架次标记，决定机组人数为 1 与 Sheet 4 的措辞
    is_recurrent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class SortieCrew(Base):
    """架次机组成员。

    `UniqueConstraint(sortie_id, role)` 是约束5「带飞架次机组人数为 2（1 名教员
    与 1 名学员）」在库层的必要条件 —— 同一架次不可能出现两个教员岗。
    """

    __tablename__ = "sortie_crew"
    __table_args__ = (
        CheckConstraint("role IN ('教员', '学员', '单飞', '复训')", name="sortie_crew_role_enum"),
        UniqueConstraint("sortie_id", "role", name="uq_sortie_crew_sortie_id_role"),
    )

    sortie_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("sorties.sortie_id", ondelete="CASCADE"), primary_key=True
    )
    person_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(8), nullable=False)


class TrainingDebt(Base):
    """本周欠账（松弛 Tier 1/2 产生，下周优先补）。"""

    __tablename__ = "training_debts"
    __table_args__ = (
        CheckConstraint("required_count >= 0", name="debt_required_nonneg"),
        CheckConstraint("scheduled_count >= 0", name="debt_scheduled_nonneg"),
        CheckConstraint("debt_count >= 0", name="debt_count_nonneg"),
        CheckConstraint("relaxed_by IN ('TIER1', 'TIER2', 'TIER3')", name="debt_relaxed_by_enum"),
        UniqueConstraint("plan_id", "person_id", "mission_id", name="uq_training_debts_key"),
    )

    debt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("plans.plan_id", ondelete="CASCADE"), nullable=False, index=True
    )
    person_id: Mapped[str] = mapped_column(String(8), nullable=False)
    mission_id: Mapped[str] = mapped_column(String(16), nullable=False)
    required_count: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_count: Mapped[int] = mapped_column(Integer, nullable=False)
    debt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    relaxed_by: Mapped[str] = mapped_column(String(8), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")


class BlockedItem(Base):
    """先修未满足而无法安排的 (人员, 课目)（v6 §3.6 / §10.4 区块4）。

    **BLOCKED ≠ INFEASIBLE**：这里记录的是「按规则本就不该排」，不是「排不出来」。
    """

    __tablename__ = "blocked_items"
    __table_args__ = (
        UniqueConstraint("plan_id", "person_id", "mission_id", name="uq_blocked_items_key"),
    )

    blocked_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("plans.plan_id", ondelete="CASCADE"), nullable=False, index=True
    )
    person_id: Mapped[str] = mapped_column(String(8), nullable=False)
    mission_id: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    #: 具体缺失的先修项，例如 ["missionA-2"]
    missing_prereqs: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)


__all__ = [
    "CREW_ROLES",
    "SOLVE_STATUSES",
    "WEEKDAYS",
    "BlockedItem",
    "Plan",
    "Sortie",
    "SortieCrew",
    "TrainingDebt",
]
