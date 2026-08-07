"""通用契约：实体引用、日期区间、错误项、轨迹事件、人工决策。

`EntityRef` / `DateRange` 见 v6 §6.5.3；`TraceEvent` 见 §8.2；
`ErrorItem` 见 §9.2 `resume_guard` 的用法；`HumanDecision` 见 §7.4 黑板状态。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.core.errors import ErrorCode, Severity, Stage

EntityKind = Literal["person", "aircraft", "mission", "airspace", "runway", "week"]


class EntityRef(BaseModel):
    """查询改写中消解出的实体引用（v6 §6.5.3）。

    `surface` 保留原文里的表述，`entity_id` 是消解后的主键——两者都留着，
    是因为「何超 / 高超」这类近音名一旦消解错，靠 `surface` 才能回溯。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EntityKind
    entity_id: str = Field(min_length=1)
    surface: str = Field(min_length=1, description="原文中的表述")
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)


class DateRange(BaseModel):
    """闭区间日期范围。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> DateRange:
        if self.end < self.start:
            raise ValueError(f"end({self.end}) 不得早于 start({self.start})")
        return self


class ErrorItem(BaseModel):
    """写入黑板 `state.errors` 的错误项（v6 §7.4 / §9.2）。

    与 `ErrorResponse` 的区别：`ErrorItem` 是**流程内**的累积记录（`add`
    reducer 只增不改），不带 `trace_id`——trace_id 属于整次运行，不属于单条。
    """

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str = Field(min_length=1)
    severity: Severity
    stage: Stage
    details: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)
    retryable: bool = False
    occurred_at: datetime = Field(default_factory=datetime.now)


TraceKind = Literal[
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
]


class TraceEvent(BaseModel):
    """过程回放事件（v6 §8.2）。`seq` 保证回放顺序。"""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0, description="全局序号，保证回放顺序")
    ts: datetime
    agent: str = Field(min_length=1)
    kind: TraceKind
    payload: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = Field(default=None, ge=0.0)
    token_usage: dict[str, int] | None = None


class HumanDecision(BaseModel):
    """HITL 人工门禁的决策记录（v6 §7.4 / §9.1 approve|reject）。"""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["APPROVE", "REJECT", "REVISE"]
    user_id: str = Field(min_length=1)
    role: Literal["viewer", "scheduler", "director", "admin"]
    comment: str = ""
    decided_at: datetime = Field(default_factory=datetime.now)
    authorized_tiers: list[int] = Field(
        default_factory=list, description="本次决策授权的松弛档位（R1 需训练主任）"
    )

    @model_validator(mode="after")
    def _tiers_in_range(self) -> HumanDecision:
        for tier in self.authorized_tiers:
            if not 0 <= tier <= 3:
                raise ValueError(f"松弛档位必须在 0~3，实际 {tier}")
        return self


__all__ = [
    "DateRange",
    "EntityKind",
    "EntityRef",
    "ErrorItem",
    "HumanDecision",
    "TraceEvent",
    "TraceKind",
]
