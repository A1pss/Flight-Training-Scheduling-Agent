"""对外冻结的 Pydantic 契约（v6 附录 B）。

全部模型 ``extra="forbid"``：多写一个字段就报错。这不是洁癖——摄取与 LLM
两条链路都会往这些结构里塞东西，宽松的 schema 会让「多了一个没人认识的
字段」这类错误一路漏到 Excel 才暴露。
"""

from backend.schemas.common import (
    DateRange,
    EntityKind,
    EntityRef,
    ErrorItem,
    HumanDecision,
    TraceEvent,
    TraceKind,
)
from backend.schemas.intent import (
    ConstraintSpec,
    FreezePolicy,
    IncrementalConstraint,
    ObjectiveWeights,
    RevisionKind,
    SolveIntent,
)
from backend.schemas.plan import (
    TRAINING_WINDOW_END,
    TRAINING_WINDOW_START,
    AirspaceId,
    BlockedItem,
    CrewMember,
    CrewRole,
    RelaxTier,
    RunwayId,
    RunwayModel,
    SchedulePlan,
    Sortie,
    TrainingDebt,
    Weekday,
)
from backend.schemas.retrieval import (
    Citation,
    GroundedClaim,
    GroundingReport,
    RewrittenQuery,
)
from backend.schemas.solver import (
    ConflictItem,
    ProbeResult,
    RelaxationProposal,
    RelaxAuthority,
    RuleTier,
    SolverStats,
    SolveStatus,
)
from backend.schemas.validation import (
    RULE_IDS,
    CheckResult,
    SchemaCheckReport,
    ValidationReport,
    Violation,
    ViolationSeverity,
)

__all__ = [
    "RULE_IDS",
    "TRAINING_WINDOW_END",
    "TRAINING_WINDOW_START",
    "AirspaceId",
    "BlockedItem",
    "CheckResult",
    "Citation",
    "ConflictItem",
    "ConstraintSpec",
    "CrewMember",
    "CrewRole",
    "DateRange",
    "EntityKind",
    "EntityRef",
    "ErrorItem",
    "FreezePolicy",
    "GroundedClaim",
    "GroundingReport",
    "HumanDecision",
    "IncrementalConstraint",
    "ObjectiveWeights",
    "ProbeResult",
    "RelaxAuthority",
    "RelaxTier",
    "RelaxationProposal",
    "RevisionKind",
    "RewrittenQuery",
    "RuleTier",
    "RunwayId",
    "RunwayModel",
    "SchedulePlan",
    "SchemaCheckReport",
    "SolveIntent",
    "SolveStatus",
    "SolverStats",
    "Sortie",
    "TraceEvent",
    "TraceKind",
    "TrainingDebt",
    "ValidationReport",
    "Violation",
    "ViolationSeverity",
    "Weekday",
]
