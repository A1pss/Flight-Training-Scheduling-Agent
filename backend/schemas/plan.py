"""排班方案契约（v6 附录 B，逐字对齐）。

v6 相对 v5.2 的四处新增，一个都不能少：

- ``CrewMember.role`` 枚举新增 **「复训」**（S-11）
- ``Sortie.runway_id``（S-05 双跑道，跑道是求解决策变量）
- ``Sortie.is_recurrent``（S-11 复训标记）
- ``SchedulePlan.semantics_switches`` 与 ``SchedulePlan.runway_model``

后两者进 `SchedulePlan` 而非只进 manifest，是因为它们**参与
`content_sha256` 的计算**：同一份数据在不同语义解读下排出的两个方案，
即使架次完全相同，也是两个不同的计划版本（v6 附录 B 脚注）。
"""

from __future__ import annotations

from datetime import date as date_type
from datetime import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Weekday = Literal["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
CrewRole = Literal["教员", "学员", "单飞", "复训"]
AirspaceId = Literal["SAA", "SAB", "IFR", "RT1", "RT2", "RNG"]
RunwayId = Literal["RWY-1", "RWY-2"]
RunwayModel = Literal["dual_runway", "single_runway"]
RelaxTier = Literal["TIER1", "TIER2", "TIER3"]

#: 训练窗（v6 §1.3.2 每日可用窗 06:00-18:00）
TRAINING_WINDOW_START: time = time(6, 0)
TRAINING_WINDOW_END: time = time(18, 0)


class CrewMember(BaseModel):
    """机组成员（v6 附录 B）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    person_id: str = Field(pattern=r"^P\d{2}$")
    name: str = Field(min_length=1)
    role: CrewRole  # ★ v6 新增「复训」


class Sortie(BaseModel):
    """单个架次。**LLM 从头到尾不生成、不修改任何一条本记录**（v6 §0.1）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sortie_id: str = Field(pattern=r"^S\d{6}$")
    date: date_type
    weekday: Weekday
    takeoff: time
    landing: time
    mission_id: str = Field(pattern=r"^mission[A-H]-\d$")
    mission_name: str = Field(min_length=1)
    airspace_id: AirspaceId
    aircraft_id: str = Field(pattern=r"^AC\d{2}$")
    runway_id: RunwayId  # ★ v6 新增（S-05）
    is_recurrent: bool = False  # ★ v6 新增（S-11）
    crew: list[CrewMember] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def _time_consistency(self) -> Sortie:
        if self.landing <= self.takeoff:
            raise ValueError("着陆时刻必须晚于起飞时刻")
        if not (self.takeoff >= TRAINING_WINDOW_START and self.landing <= TRAINING_WINDOW_END):
            raise ValueError("架次必须落在训练窗 06:00-18:00 内")
        return self

    @model_validator(mode="after")
    def _crew_composition(self) -> Sortie:
        """§3.1.1 机组编成：带飞 2 人（1 教员 1 学员），单飞/复训 1 人。"""
        roles = [c.role for c in self.crew]
        if len(self.crew) == 2:
            if sorted(roles) != sorted(["教员", "学员"]):
                raise ValueError(f"带飞架次机组必须为 1 教员 + 1 学员，实际 {roles}")
        elif roles[0] not in ("单飞", "复训"):
            raise ValueError(f"单人架次角色必须为 单飞 或 复训，实际 {roles[0]}")
        if self.is_recurrent and roles != ["复训"]:
            raise ValueError("is_recurrent 架次的角色必须为 复训")
        return self


class BlockedItem(BaseModel):
    """先修未满足而被排除的 (学员, 课目) 组合。

    **披露率 100% 是 v6 §0.3 的四条可测断言之一**——被排除的组合必须
    出现在 Sheet 4 区块 4，不能悄悄消失。
    """

    model_config = ConfigDict(extra="forbid")

    person_id: str = Field(pattern=r"^P\d{2}$")
    mission_id: str = Field(pattern=r"^mission[A-H]-\d$")
    reason: str = Field(min_length=1, description="如「先修 A 类未达标」")
    missing_prereqs: list[str] = Field(default_factory=list)


class TrainingDebt(BaseModel):
    """松弛产生的训练欠账（v6 附录 B）。必须显式披露，不得静默吞掉。"""

    model_config = ConfigDict(extra="forbid")

    person_id: str = Field(pattern=r"^P\d{2}$")
    mission_id: str = Field(pattern=r"^mission[A-H]-\d$")
    required: int = Field(ge=0, description="按 freq_days 滑窗推出的本周应排次数")
    scheduled: int = Field(ge=0)
    debt: int = Field(ge=0)
    relaxed_by: RelaxTier

    @model_validator(mode="after")
    def _debt_arithmetic(self) -> TrainingDebt:
        expected = max(0, self.required - self.scheduled)
        if self.debt != expected:
            raise ValueError(
                f"debt 应为 max(0, required - scheduled) = {expected}，实际 {self.debt}"
            )
        return self


class SchedulePlan(BaseModel):
    """一周排班方案（v6 附录 B）。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1)
    iso_week: str = Field(pattern=r"^\d{4}W\d{2}$")
    week_start: date_type
    week_end: date_type
    snapshot_id: str = Field(min_length=1)
    ruleset_version: str = Field(min_length=1)
    semantics_version: str = Field(min_length=1)
    #: ★ v6 新增：S-01~S-13 的取值快照，参与 content_sha256
    semantics_switches: dict[str, str] = Field(default_factory=dict)
    runway_model: RunwayModel  # ★ v6 新增
    relaxation_tier: int = Field(ge=0, le=3)
    sorties: list[Sortie] = Field(default_factory=list)
    debts: list[TrainingDebt] = Field(default_factory=list)
    blocked_items: list[BlockedItem] = Field(default_factory=list)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _week_span(self) -> SchedulePlan:
        if (self.week_end - self.week_start).days != 6:
            raise ValueError(
                f"排班跨度必须为 7 天（周一~周日），实际 {self.week_start} ~ {self.week_end}"
            )
        return self

    @model_validator(mode="after")
    def _sorties_within_week(self) -> SchedulePlan:
        for s in self.sorties:
            if not (self.week_start <= s.date <= self.week_end):
                raise ValueError(
                    f"架次 {s.sortie_id} 日期 {s.date} 落在排班周 "
                    f"{self.week_start}~{self.week_end} 之外"
                )
        return self

    @model_validator(mode="after")
    def _unique_sortie_ids(self) -> SchedulePlan:
        ids = [s.sortie_id for s in self.sorties]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"sortie_id 必须唯一，重复项：{dupes}")
        return self


__all__ = [
    "TRAINING_WINDOW_END",
    "TRAINING_WINDOW_START",
    "AirspaceId",
    "BlockedItem",
    "CrewMember",
    "CrewRole",
    "RelaxTier",
    "RunwayId",
    "RunwayModel",
    "SchedulePlan",
    "Sortie",
    "TrainingDebt",
    "Weekday",
]
