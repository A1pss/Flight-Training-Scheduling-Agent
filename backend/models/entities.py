"""事实表：人员 / 飞机 / 空域 / 课目 / 跑道，及其多值属性的从表。

全部按 `(自然主键, snapshot_id)` 复合主键建模，外键一并复合 —— 理由见
:mod:`backend.models.base` 的模块文档。

**与 v6 §1.3 的对照**（M1 的落库对照表）：
8 人（3 教员 + 1 成熟飞行员 + 4 学员）· 8 机（JL-8 六架 AC10/27/34/49/61/73；
JL-9 两架 AC84/AC95）· 12 课目 · 6 空域 · 2 跑道。
"""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base

#: 人员身份（`personnel.pdf` 总表「身份」列的全部取值）
IDENTITIES = ("教员", "成熟飞行员", "学员")
#: 类别资质等级（`personnel.pdf` 课目级资质明细的「等级」）
QUAL_LEVELS = ("教员", "单飞", "带飞")
#: 机型（`aircraft.pdf`）
AIRCRAFT_TYPES = ("JL-8", "JL-9")
#: 课目类别 A~H（由课目编号 `mission<X>-<n>` 的 `<X>` 决定）
MISSION_CLASSES = ("A", "B", "C", "D", "E", "F", "G", "H")
#: `mission_prereq.ref_kind` —— 先修引用可以是课目编号，也可以是类别（S-01）
PREREQ_REF_KINDS = ("mission", "class")


# ─────────────────────────────────────────────────────────────────────
# 人员
# ─────────────────────────────────────────────────────────────────────
class Person(Base):
    """飞行人员（`personnel.pdf` 一、人员资质总表）。"""

    __tablename__ = "persons"
    __table_args__ = (
        CheckConstraint("identity IN ('教员', '成熟飞行员', '学员')", name="person_identity_enum"),
        CheckConstraint("person_id ~ '^P[0-9]{2}$'", name="person_id_format"),
    )

    person_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("data_snapshots.snapshot_id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    identity: Mapped[str] = mapped_column(String(16), nullable=False)


class PersonAircraftType(Base):
    """人员机型资质（总表「机型资质」列，形如 `JL-8、JL-9`）。"""

    __tablename__ = "person_aircraft_types"
    __table_args__ = (
        ForeignKeyConstraint(
            ["person_id", "snapshot_id"],
            ["persons.person_id", "persons.snapshot_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("aircraft_type IN ('JL-8', 'JL-9')", name="person_aircraft_type_enum"),
    )

    person_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aircraft_type: Mapped[str] = mapped_column(String(8), primary_key=True)


class PersonQualification(Base):
    """课目类别资质（`personnel.pdf` 二、课目级资质明细）。

    `expiry_date` 即「到期日」，仅刘斌 C 类有值。**X1 冲突就发生在这一列**：
    总表写 2026-01-07、明细表写 2026-02-07，由 :mod:`backend.ingestion.conflicts`
    检出并上报人工确认，落库值取裁定后的 2026-01-07。
    """

    __tablename__ = "person_qualifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["person_id", "snapshot_id"],
            ["persons.person_id", "persons.snapshot_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("level IN ('教员', '单飞', '带飞')", name="qualification_level_enum"),
        CheckConstraint(
            "mission_class IN ('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H')",
            name="qualification_class_enum",
        ),
    )

    person_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_class: Mapped[str] = mapped_column(String(2), primary_key=True)
    level: Mapped[str] = mapped_column(String(8), nullable=False)
    expiry_date: Mapped[date | None] = mapped_column(Date)


class PersonUnavailability(Base):
    """人员不可用日期（总表「不可用日期」列；基准周内只有吴鹏 2026-01-05）。

    单独建表而不是在 `persons` 上加一列：该列在 PDF 里是多值的，一个标量列
    存不下第二个日期。
    """

    __tablename__ = "person_unavailability"
    __table_args__ = (
        ForeignKeyConstraint(
            ["person_id", "snapshot_id"],
            ["persons.person_id", "persons.snapshot_id"],
            ondelete="CASCADE",
        ),
    )

    person_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    unavailable_date: Mapped[date] = mapped_column(Date, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")


class PersonCompletedMission(Base):
    """已完成课目（总表「已完成课目」列）。

    这是 `training_progress.status = COMPLETED` 的**事实来源**：进度表由它
    加上课目周期长度物化出来，而不是反过来。
    """

    __tablename__ = "person_completed_missions"
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
    )

    person_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(String(16), primary_key=True)


# ─────────────────────────────────────────────────────────────────────
# 飞机
# ─────────────────────────────────────────────────────────────────────
class Aircraft(Base):
    """飞机（`aircraft.pdf` 一、飞机资源明细）。

    ⚠️ **AC73 是 JL-8 不是 JL-9**；JL-9 只有 AC84 / AC95 两架（v6 §1.3.2）。
    """

    __tablename__ = "aircraft"
    __table_args__ = (
        CheckConstraint("aircraft_type IN ('JL-8', 'JL-9')", name="aircraft_type_enum"),
        CheckConstraint("seats > 0", name="aircraft_seats_positive"),
        CheckConstraint("turnaround_minutes >= 0", name="aircraft_turnaround_nonneg"),
        CheckConstraint("daily_window_start < daily_window_end", name="aircraft_window_ordered"),
        CheckConstraint("aircraft_id ~ '^AC[0-9]{2}$'", name="aircraft_id_format"),
    )

    aircraft_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("data_snapshots.snapshot_id", ondelete="CASCADE"), primary_key=True
    )
    aircraft_type: Mapped[str] = mapped_column(String(8), nullable=False)
    seats: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_window_start: Mapped[time] = mapped_column(Time, nullable=False)
    daily_window_end: Mapped[time] = mapped_column(Time, nullable=False)
    turnaround_minutes: Mapped[int] = mapped_column(Integer, nullable=False)


class AircraftMissionCapability(Base):
    """飞机适配课目（`aircraft.pdf`「适配课目」列）。

    这一列正是 X2 的现场：PDF 文本流里 `missionC-` 与 `1` 被硬换行拆开，
    经修复层归一化后才能与 `missions.mission_id` 对上外键。
    """

    __tablename__ = "aircraft_mission_capability"
    __table_args__ = (
        ForeignKeyConstraint(
            ["aircraft_id", "snapshot_id"],
            ["aircraft.aircraft_id", "aircraft.snapshot_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["mission_id", "snapshot_id"],
            ["missions.mission_id", "missions.snapshot_id"],
            ondelete="CASCADE",
        ),
    )

    aircraft_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(String(16), primary_key=True)


class AircraftMaintenance(Base):
    """维护计划（`aircraft.pdf`「维护计划」列；基准周内只有 AC73 01-09 全天定检）。"""

    __tablename__ = "aircraft_maintenance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["aircraft_id", "snapshot_id"],
            ["aircraft.aircraft_id", "aircraft.snapshot_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("start_ts < end_ts", name="maintenance_interval_ordered"),
    )

    aircraft_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=False), primary_key=True)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="定检维护")
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


# ─────────────────────────────────────────────────────────────────────
# 空域 / 课目 / 跑道
# ─────────────────────────────────────────────────────────────────────
class Airspace(Base):
    """空域与航线（`aircraft.pdf` 二、空域/航线资源与容量）。

    `capacity` 即同时段容量，S-10 裁定为**硬约束**并入约束6（不新增第 15 条）。
    """

    __tablename__ = "airspaces"
    __table_args__ = (CheckConstraint("capacity >= 1", name="airspace_capacity_positive"),)

    airspace_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("data_snapshots.snapshot_id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)


class Mission(Base):
    """课目（`missions.pdf` 课目频率标准明细表）。

    `freq_days` 是「每 N 天 ≥1 次」的 N，A 类 3、B~F 类 7、G/H 类 14；
    `weekly_required` 对应 A-1/A-2 的「（每周必飞）」，即约束3 的适用标记；
    `dual_required` 是「带飞」列 —— **A-1/A-2 为否（D-1），学员 A 类单飞**。
    """

    __tablename__ = "missions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["airspace_id", "snapshot_id"],
            ["airspaces.airspace_id", "airspaces.snapshot_id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("duration_minutes > 0", name="mission_duration_positive"),
        CheckConstraint("freq_days > 0", name="mission_freq_days_positive"),
        CheckConstraint("cycle_weeks > 0", name="mission_cycle_weeks_positive"),
        CheckConstraint(
            "mission_class IN ('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H')",
            name="mission_class_enum",
        ),
        CheckConstraint("mission_id ~ '^mission[A-H]-[0-9]$'", name="mission_id_format"),
    )

    mission_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("data_snapshots.snapshot_id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    mission_class: Mapped[str] = mapped_column(String(2), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    cycle_weeks: Mapped[int] = mapped_column(Integer, nullable=False)
    freq_days: Mapped[int] = mapped_column(Integer, nullable=False)
    weekly_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dual_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    airspace_id: Mapped[str] = mapped_column(String(8), nullable=False)
    #: 频率要求原文，保留以便 Sheet 4 溯源
    frequency_text: Mapped[str] = mapped_column(Text, nullable=False, default="")


class MissionAircraftType(Base):
    """课目适配机型（`missions.pdf`「机型」列，形如 `JL-8/JL-9`）。"""

    __tablename__ = "mission_aircraft_types"
    __table_args__ = (
        ForeignKeyConstraint(
            ["mission_id", "snapshot_id"],
            ["missions.mission_id", "missions.snapshot_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("aircraft_type IN ('JL-8', 'JL-9')", name="mission_aircraft_type_enum"),
    )

    mission_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aircraft_type: Mapped[str] = mapped_column(String(8), primary_key=True)


class MissionPrereq(Base):
    """先修关系（`missions.pdf`「先修」列）。

    `prereq_ref` 既可以是课目编号（`missionC-1`），也可以是类别（`A类`）；
    `ref_kind` 区分两者。**类别引用按 S-01 展开为「该类全部课目」的动作放在
    `compile_spec_node`（M2/W7），不在 SQL 里做**（v6 §6.1）。
    """

    __tablename__ = "mission_prereq"
    __table_args__ = (
        ForeignKeyConstraint(
            ["mission_id", "snapshot_id"],
            ["missions.mission_id", "missions.snapshot_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("ref_kind IN ('mission', 'class')", name="prereq_ref_kind_enum"),
        CheckConstraint("mission_id <> prereq_ref", name="prereq_no_self_loop"),
    )

    mission_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    prereq_ref: Mapped[str] = mapped_column(String(16), primary_key=True)
    ref_kind: Mapped[str] = mapped_column(String(8), nullable=False)


class Runway(Base):
    """跑道（v6 §1.3.5 / S-05 双跑道模型）。

    ⚠️ **不是「RWY-1=JL-8、RWY-2=JL-9」**：
    `RWY-1` 服务 JL-8 与 JL-9（全 8 架），`RWY-2` 只服务 JL-8（六架）。
    """

    __tablename__ = "runways"
    __table_args__ = (CheckConstraint("runway_id ~ '^RWY-[0-9]$'", name="runway_id_format"),)

    runway_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("data_snapshots.snapshot_id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)


class RunwayAircraftType(Base):
    """跑道服务机型。`RWY-1 → {JL-8, JL-9}`，`RWY-2 → {JL-8}`。"""

    __tablename__ = "runway_aircraft_types"
    __table_args__ = (
        ForeignKeyConstraint(
            ["runway_id", "snapshot_id"],
            ["runways.runway_id", "runways.snapshot_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint("aircraft_type IN ('JL-8', 'JL-9')", name="runway_aircraft_type_enum"),
    )

    runway_id: Mapped[str] = mapped_column(String(8), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aircraft_type: Mapped[str] = mapped_column(String(8), primary_key=True)


__all__ = [
    "AIRCRAFT_TYPES",
    "IDENTITIES",
    "MISSION_CLASSES",
    "PREREQ_REF_KINDS",
    "QUAL_LEVELS",
    "Aircraft",
    "AircraftMaintenance",
    "AircraftMissionCapability",
    "Airspace",
    "Mission",
    "MissionAircraftType",
    "MissionPrereq",
    "Person",
    "PersonAircraftType",
    "PersonCompletedMission",
    "PersonQualification",
    "PersonUnavailability",
    "Runway",
    "RunwayAircraftType",
]
