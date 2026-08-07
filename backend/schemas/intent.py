"""求解意图与约束规格（v6 §7.3 / §7.2 `compile_spec_node`）。

`SolveIntent` 是 Planner 的**唯一**产物，也是 LLM 能触及排班的**唯一**接口。
它只能调四类旋钮——范围 / 冻结策略 / 目标权重 / 松弛档位——**不能增删硬约束、
不能指定具体架次、不能绕过任何 R0 规则**（v6 §7.3.2）。

`ConstraintSpec` 是 `compile_spec_node` 的确定性编译产物：
``ruleset.yaml + semantics.yaml + SolveIntent → ConstraintSpec``。
冲突时以 ruleset 为准，所以 `ConstraintSpec` 里的规则集部分不接受 intent 覆写。
"""

from __future__ import annotations

from datetime import date as date_type
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.plan import RunwayModel

FreezePolicy = Literal["CONSERVATIVE", "BALANCED", "AGGRESSIVE"]
RevisionKind = Literal[
    "FORBID",
    "PIN_TIME",
    "PIN_RESOURCE",
    "SHIFT_WINDOW",
    "REDUCE_DENSITY",
    "PIN_RUNWAY",
]


class ObjectiveWeights(BaseModel):
    """目标函数三项权重（v6 §7.3.2「进度/扰动/均衡 三项权重」，属 R3 偏好）。

    R3 是自由调整档，**不影响可行性**——权重怎么调都不会让一个不可行问题变
    可行，也不会让合规的解变得不合规。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    progress: float = Field(ge=0.0, description="训练进度推进权重")
    disruption: float = Field(ge=0.0, description="相对上一版方案的扰动惩罚权重")
    balance: float = Field(ge=0.0, description="负载均衡（含跑道均衡）权重")

    @model_validator(mode="after")
    def _not_all_zero(self) -> ObjectiveWeights:
        if self.progress == self.disruption == self.balance == 0.0:
            raise ValueError("三项权重不得同时为 0，否则目标函数退化为常量")
        return self


class IncrementalConstraint(BaseModel):
    """多轮修订注入的增量约束（v6 §7.3.4）。

    **它是求解器输入，不是对结果的修改**——翻译完仍走完整 ``solve → validate``。
    `origin_utterance` 保留用户原话，既供撤销与审计，也供 UI 回显确认
    （v6 §7.3.4 第 4 条：翻译结果必须回显，这一步不能省）。
    """

    model_config = ConfigDict(extra="forbid")

    kind: RevisionKind
    targets: list[str] = Field(min_length=1, description="sortie_id / person_id / aircraft_id")
    params: dict[str, Any] = Field(default_factory=dict)
    origin_utterance: str = Field(min_length=1, description="★ 原始语句，供撤销与审计")
    round_no: int = Field(ge=1, description="★ 第几轮修订")


class SolveIntent(BaseModel):
    """Planner 的唯一产物。它是求解的输入，不是求解的结果。"""

    model_config = ConfigDict(extra="forbid")

    scope_persons: list[str] | Literal["ALL"]
    scope_missions: list[str] | Literal["ALL"]
    freeze_policy: FreezePolicy
    freeze_reason: str = Field(min_length=1, description="为何选这一档，写进 Sheet 4")
    objective_weights: ObjectiveWeights
    pre_authorized_tiers: list[int] = Field(default_factory=list)
    incremental_constraints: list[IncrementalConstraint] = Field(default_factory=list)
    estimated_blast_radius: int = Field(ge=0, description="预计受影响架次数")
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _tiers_valid(self) -> SolveIntent:
        for tier in self.pre_authorized_tiers:
            if not 0 <= tier <= 3:
                raise ValueError(f"松弛档位必须在 0~3，实际 {tier}")
        return self


class ConstraintSpec(BaseModel):
    """`compile_spec_node` 的确定性编译产物（v6 §7.2）。

    含 S-01 类别先修展开与 S-11 复训标记写入。**这个节点不经 Harness、
    不读 Skill、不注册为任何 LLM 组件的工具**（CLAUDE.md 铁律 4）。
    """

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(min_length=1)
    ruleset_version: str = Field(min_length=1)
    semantics_version: str = Field(min_length=1)
    semantics_switches: dict[str, str] = Field(default_factory=dict)

    iso_week: str = Field(pattern=r"^\d{4}W\d{2}$")
    week_start: date_type
    week_end: date_type

    scope_persons: list[str] | Literal["ALL"]
    scope_missions: list[str] | Literal["ALL"]
    relaxation_tier: int = Field(ge=0, le=3)
    objective_weights: ObjectiveWeights
    incremental_constraints: list[IncrementalConstraint] = Field(default_factory=list)

    #: S-05 跑道模型与映射（跑道 → 可用机型）
    runway_model: RunwayModel
    runways: dict[str, list[str]] = Field(default_factory=dict)
    #: 20 分钟窗口 / 7 分钟间隔的分组口径（D-2）
    density_scope: dict[str, str] = Field(default_factory=dict)
    #: S-10 空域同时段容量
    airspace_capacity: dict[str, int] = Field(default_factory=dict)
    #: 各课目 freq_days（约束13 滑窗长度）
    freq_days: dict[str, int] = Field(default_factory=dict)
    #: 约束14 的 req_max = ceil(7 / freq_days)
    req_max: dict[str, int] = Field(default_factory=dict)
    #: 求解预算（§3.11），进 manifest 参与可复现性
    solver_seed: int = 42
    solver_workers: int = Field(default=4, ge=1)
    solver_time_limit_s: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _week_span(self) -> ConstraintSpec:
        if (self.week_end - self.week_start).days != 6:
            raise ValueError("排班跨度必须为 7 天（周一~周日）")
        return self


__all__ = [
    "ConstraintSpec",
    "FreezePolicy",
    "IncrementalConstraint",
    "ObjectiveWeights",
    "RevisionKind",
    "SolveIntent",
]
