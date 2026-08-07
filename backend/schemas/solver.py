"""求解统计、冲突集、松弛提案、探针结果（v6 §3.6 / §3.9 / §3.10）。

**铁律 8 在类型层的落点**：`SolveStatus` 把 ``UNKNOWN`` 与 ``INFEASIBLE`` 做成
两个不同的枚举值，`ProbeResult` 与 `SolverStats` 都必须携带它。把二者混为一谈
是本类系统最伤信任的 bug——「排不出来」和「排不出来但我不确定」对训练主任
是完全不同的两件事。

**松弛提案必须经实证验证**（v6 §3.9.1）：未经 `probe_solve` 实际验证过的提案
不得呈现给用户。`verified` 与 `note` 两个字段就是这条规矩的载体：验证过的标
「✔ 已验证可行」，探针超时的标「⚠ 未能确认」，INFEASIBLE 的直接丢弃不进列表。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.schemas.plan import TrainingDebt

SolveStatus = Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN", "MODEL_INVALID"]
RelaxAuthority = Literal["排班员", "训练主任", "不可自动执行"]
RuleTier = Literal["R0", "R1", "R2", "R3"]


class SolverStats(BaseModel):
    """CP-SAT 求解统计（v6 §8.2 求解面板）。

    `random_seed` 与 `num_workers` 都是**可复现性的组成部分**（v6 §3.11）：
    CP-SAT 的多线程搜索在不同 worker 数下可能返回不同的等价最优解，所以
    两者必须随统计一起落库并进 manifest。
    """

    model_config = ConfigDict(extra="forbid")

    status: SolveStatus
    num_candidates: int = Field(ge=0)
    num_variables: int = Field(ge=0)
    num_constraints: int = Field(ge=0)
    objective_value: float | None = None
    best_bound: float | None = None
    gap: float | None = Field(default=None, ge=0.0)
    wall_time_ms: float = Field(ge=0.0)
    num_branches: int = Field(default=0, ge=0)
    num_conflicts: int = Field(default=0, ge=0)
    random_seed: int = 42
    num_workers: int = Field(default=4, ge=1)
    relaxation_tier: int = Field(default=0, ge=0, le=3)

    @model_validator(mode="after")
    def _objective_present_when_solved(self) -> SolverStats:
        if self.status in ("OPTIMAL", "FEASIBLE") and self.objective_value is None:
            raise ValueError(f"status={self.status} 时 objective_value 不得为空")
        return self


class ConflictItem(BaseModel):
    """最小冲突集中的一项（v6 §3.9，来自 assumption literals）。"""

    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(min_length=1, description="可松弛约束组的 id")
    rule_ids: list[str] = Field(min_length=1, description="涉及的规则编号，如 ['C11','C13']")
    tier: RuleTier
    description: str = Field(min_length=1, description="人类可读的冲突陈述")
    subjects: list[str] = Field(default_factory=list, description="归因到的具体实体")


class ProbeResult(BaseModel):
    """`probe_solve` 只读探针的结果（v6 §3.9.1）。

    探针是 CLAUDE.md 铁律 4 确定性边界的**唯一例外**——它不产出交付方案，
    结果必须经 `validate_node` 才能进入输出。
    """

    model_config = ConfigDict(extra="forbid")

    status: SolveStatus
    sorties: int = Field(ge=0)
    debts: list[TrainingDebt] = Field(default_factory=list)
    wall_time_ms: float = Field(ge=0.0, default=0.0)


class RelaxationProposal(BaseModel):
    """分级松弛提案（v6 §3.9.3 表格 + §3.10 松弛阶梯）。"""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1)
    tier: int = Field(ge=0, le=3, description="松弛阶梯档位")
    action: str = Field(min_length=1, description="如「missionF-1 本周顺延，欠账记入下周」")
    cost: str = Field(min_length=1, description="如「进度延迟 1 周」")
    affected_rules: list[str] = Field(min_length=1, description="如 ['C13']")
    rule_tier: RuleTier
    authority: RelaxAuthority
    recommended: bool = False
    verified: bool = False
    verified_result: ProbeResult | None = None
    note: str | None = Field(
        default=None, description="未验证时的说明，如「探针超时，未能确认此方案可行」"
    )

    @model_validator(mode="after")
    def _r0_never_relaxable(self) -> RelaxationProposal:
        """R0 安全刚性绝不可松弛（v6 §3.10）——在契约层就堵死。"""
        if self.rule_tier == "R0":
            raise ValueError("R0 安全刚性规则绝不可松弛，不得构造针对它的松弛提案（v6 §3.10）")
        return self

    @model_validator(mode="after")
    def _verified_needs_result(self) -> RelaxationProposal:
        """已验证的提案必须带探针结果；未验证的必须给出说明（v6 §3.9.1）。"""
        if self.verified and self.verified_result is None:
            raise ValueError("verified=True 的提案必须携带 verified_result")
        if not self.verified and not self.note:
            raise ValueError("未经验证的提案必须在 note 中明确标注原因，不得隐瞒")
        return self


__all__ = [
    "ConflictItem",
    "ProbeResult",
    "RelaxAuthority",
    "RelaxationProposal",
    "RuleTier",
    "SolveStatus",
    "SolverStats",
]
