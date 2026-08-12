r"""排班方案契约（v6 附录 B，逐字对齐）。

**编号与枚举的口径以 v6 §5.1.1 为准**（业务方 2026-08-11 裁定）：编号只固定前缀约定、
不限位数；空域编号由上传数据决定，**不是枚举**。附录 B 早先把 `person_id` 钉成
`^P\d{2}$`、`airspace_id`/`runway_id` 钉成基准取值的 `Literal`，与 §5.1.1 直接矛盾 ——
后果很具体：用户上传 9 个人（`P100`）、或空域叫 `LAC`，**摄取会通过、求解会通过，
组装 `SchedulePlan` 时才 ValidationError**。

仍然保持 `Literal` 的三处是**规格的一部分**，不是「把基准数据写成枚举」：
`CrewRole`（新增一个角色意味着新的编成规则，必须先有业务方裁决）、`Weekday`、
`RunwayModel`（S-05 的开关取值）。`sortie_id` 保持 `^S\d{6}$` —— 它不是上传数据，
是本系统自己发的号（§10.6）。

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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Weekday = Literal["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
CrewRole = Literal["教员", "学员", "单飞", "复训"]
RunwayModel = Literal["dual_runway", "single_runway"]
RelaxTier = Literal["TIER1", "TIER2", "TIER3"]

# ─────────────────────────────────────────────────────────────────────
# 编号格式（v6 §5.1.1：只固定前缀约定，不限位数）
#
# 这几个常量存在的意义是「只有一处定义」：M1 的 ORM CHECK、摄取校验与这里的契约
# 必须是同一口径，散在三处迟早漂。
# ─────────────────────────────────────────────────────────────────────
PERSON_ID_PATTERN = r"^P\d+$"
AIRCRAFT_ID_PATTERN = r"^AC\d+$"
MISSION_ID_PATTERN = r"^mission[A-Z]-\d+$"
RUNWAY_ID_PATTERN = r"^RWY-\d+$"
#: 系统自己发的架次号（§10.6 命名归档），不是上传数据，故仍钉死位数
SORTIE_ID_PATTERN = r"^S\d{6}$"

#: 训练窗（v6 §1.3.2 每日可用窗 06:00-18:00）
TRAINING_WINDOW_START: time = time(6, 0)
TRAINING_WINDOW_END: time = time(18, 0)


class CrewMember(BaseModel):
    """机组成员（v6 附录 B）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    person_id: str = Field(pattern=PERSON_ID_PATTERN)
    name: str = Field(min_length=1)
    role: CrewRole  # ★ v6 新增「复训」


class Sortie(BaseModel):
    """单个架次。**LLM 从头到尾不生成、不修改任何一条本记录**（v6 §0.1）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sortie_id: str = Field(pattern=SORTIE_ID_PATTERN)
    date: date_type
    weekday: Weekday
    takeoff: time
    landing: time
    mission_id: str = Field(pattern=MISSION_ID_PATTERN)
    mission_name: str = Field(min_length=1)
    #: 空域编号由上传数据决定，**不是枚举**（§5.1.1）。与 `airspaces` 表的一致性
    #: 由摄取期的引用完整性校验保证，不靠这里的类型
    airspace_id: str = Field(min_length=1)
    aircraft_id: str = Field(pattern=AIRCRAFT_ID_PATTERN)
    runway_id: str = Field(pattern=RUNWAY_ID_PATTERN)  # ★ v6 新增（S-05）
    is_recurrent: bool = False  # ★ v6 新增（S-11）
    crew: list[CrewMember] = Field(min_length=1, max_length=2)

    @field_validator("takeoff", "landing")
    @classmethod
    def _naive_time_only(cls, value: time) -> time:
        """时刻**不得带时区偏移**——全系统按本场当地时间记时。

        M3 的 schemathesis 契约测试抓到的：Pydantic 会把 `"06:00:00Z"` 解析成一个
        带 tzinfo 的 `time`，随后 `_time_consistency` 拿它与朴素的训练窗边界比较，
        直接 `TypeError: can't compare offset-naive and offset-aware times` ——
        本该是一条 422 契约错误，却成了 500。**在字段层挡下**，比在比较处补
        try/except 干净：带偏移的时刻在本领域里本就没有意义。
        """
        if value.tzinfo is not None:
            raise ValueError(f"时刻不得带时区偏移（本场当地时间），实际 {value!r}")
        return value

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

    person_id: str = Field(pattern=PERSON_ID_PATTERN)
    mission_id: str = Field(pattern=MISSION_ID_PATTERN)
    reason: str = Field(min_length=1, description="如「missionA-2 未完成」")
    missing_prereqs: list[str] = Field(default_factory=list)


class TrainingDebt(BaseModel):
    """松弛产生的训练欠账（v6 附录 B）。必须显式披露，不得静默吞掉。"""

    model_config = ConfigDict(extra="forbid")

    person_id: str = Field(pattern=PERSON_ID_PATTERN)
    mission_id: str = Field(pattern=MISSION_ID_PATTERN)
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
    "AIRCRAFT_ID_PATTERN",
    "MISSION_ID_PATTERN",
    "PERSON_ID_PATTERN",
    "RUNWAY_ID_PATTERN",
    "SORTIE_ID_PATTERN",
    "TRAINING_WINDOW_END",
    "TRAINING_WINDOW_START",
    "BlockedItem",
    "CrewMember",
    "CrewRole",
    "RelaxTier",
    "RunwayModel",
    "SchedulePlan",
    "Sortie",
    "TrainingDebt",
    "Weekday",
]
