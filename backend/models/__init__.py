"""SQLAlchemy ORM 模型。

分组：
- :mod:`backend.models.versioning` —— 快照 / 规则集 / 语义版本（铁律 9 的三把钥匙）
- :mod:`backend.models.entities`   —— 人员 / 飞机 / 空域 / 课目 / 跑道等事实表
- :mod:`backend.models.progress`   —— 跨周进度锚点（v6 §6.3）
- :mod:`backend.models.planning`   —— 排班产物（计划 / 架次 / 机组 / 欠账 / 阻塞项）
- :mod:`backend.models.memory`     —— 情景记忆 / 程序记忆（v6 §6.2、§6.4）
- :mod:`backend.models.audit`      —— 审计流水 / 过程回放事件

LangGraph 的 checkpoint 表不在这里定义 —— 它们由 `PostgresSaver.setup()`
自行建表，落点见 :mod:`backend.graph.checkpointer`，由独立的 Alembic 迁移调用。
"""

from backend.models.audit import TRACE_KINDS, AuditLog, TraceEventRow
from backend.models.base import NAMING_CONVENTION, Base, TimestampMixin
from backend.models.entities import (
    AIRCRAFT_TYPES,
    IDENTITIES,
    MISSION_CLASSES,
    PREREQ_REF_KINDS,
    QUAL_LEVELS,
    Aircraft,
    AircraftMaintenance,
    AircraftMissionCapability,
    Airspace,
    Mission,
    MissionAircraftType,
    MissionPrereq,
    Person,
    PersonAircraftType,
    PersonCompletedMission,
    PersonQualification,
    PersonUnavailability,
    Runway,
    RunwayAircraftType,
)
from backend.models.memory import EPISODIC_KINDS, EpisodicMemory, ProceduralMemory
from backend.models.planning import (
    CREW_ROLES,
    SOLVE_STATUSES,
    WEEKDAYS,
    BlockedItem,
    Plan,
    Sortie,
    SortieCrew,
    TrainingDebt,
)
from backend.models.progress import PROGRESS_STATUSES, TrainingProgress
from backend.models.versioning import (
    SNAPSHOT_STATUSES,
    DataSnapshot,
    Ruleset,
    SemanticsVersion,
)

__all__ = [
    "AIRCRAFT_TYPES",
    "CREW_ROLES",
    "EPISODIC_KINDS",
    "IDENTITIES",
    "MISSION_CLASSES",
    "NAMING_CONVENTION",
    "PREREQ_REF_KINDS",
    "PROGRESS_STATUSES",
    "QUAL_LEVELS",
    "SNAPSHOT_STATUSES",
    "SOLVE_STATUSES",
    "TRACE_KINDS",
    "WEEKDAYS",
    "Aircraft",
    "AircraftMaintenance",
    "AircraftMissionCapability",
    "Airspace",
    "AuditLog",
    "Base",
    "BlockedItem",
    "DataSnapshot",
    "EpisodicMemory",
    "Mission",
    "MissionAircraftType",
    "MissionPrereq",
    "Person",
    "PersonAircraftType",
    "PersonCompletedMission",
    "PersonQualification",
    "PersonUnavailability",
    "Plan",
    "ProceduralMemory",
    "Ruleset",
    "Runway",
    "RunwayAircraftType",
    "SemanticsVersion",
    "Sortie",
    "SortieCrew",
    "TimestampMixin",
    "TraceEventRow",
    "TrainingDebt",
    "TrainingProgress",
]
