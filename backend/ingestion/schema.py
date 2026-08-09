"""摄取层内部契约。

与 `backend/schemas/`（v6 附录 B 的**对外冻结**契约）分开：这里的模型描述的是
「从文档里抽出来、还没落库」的中间形态，字段贴着 PDF 的列走。全部
``extra="forbid"`` —— 多抽出一个没人认识的字段就报错，而不是一路漏到 Excel。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.models.entities import AIRCRAFT_TYPES, IDENTITIES, QUAL_LEVELS

#: v6 §5.1 文档分类器的六类
DocumentClass = Literal[
    "人员档案",
    "飞机资源",
    "课目标准",
    "规则条文",
    "情况文件",
    "未知",
]

Identity = Literal["教员", "成熟飞行员", "学员"]
QualLevel = Literal["教员", "单飞", "带飞"]
AircraftType = Literal["JL-8", "JL-9"]
MissionClass = Literal["A", "B", "C", "D", "E", "F", "G", "H"]
PrereqRefKind = Literal["mission", "class"]


class SourceFile(BaseModel):
    """一份参与摄取的源文件。`sha256` 进 snapshot manifest，保证可追溯。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1)
    doc_class: DocumentClass
    classifier: Literal["rule", "llm"] = "rule"
    pages: int = Field(ge=0, default=0)


class IngestedQualification(BaseModel):
    """课目类别资质。`expiry_date` 只有刘斌 C 类非空。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    person_id: str = Field(pattern=r"^P\d{2}$")
    mission_class: MissionClass
    level: QualLevel
    expiry_date: date | None = None


class IngestedPerson(BaseModel):
    """`personnel.pdf` 的一名飞行人员（总表 + 明细表合并后的形态）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    person_id: str = Field(pattern=r"^P\d{2}$")
    name: str = Field(min_length=1)
    identity: Identity
    aircraft_types: tuple[AircraftType, ...]
    completed_missions: tuple[str, ...] = ()
    unavailable_dates: tuple[date, ...] = ()
    qualifications: tuple[IngestedQualification, ...] = ()
    #: 总表「复训到期」列的原文，形如 `仪表等级(C类):2026-01-07`。X1 冲突的来源 A。
    recurrent_due_raw: str = ""

    @field_validator("aircraft_types")
    @classmethod
    def _nonempty_types(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("机型资质不得为空")
        return v


class IngestedMaintenance(BaseModel):
    """维护时段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    aircraft_id: str = Field(pattern=r"^AC\d{2}$")
    start_ts: datetime
    end_ts: datetime
    kind: str = "定检维护"
    all_day: bool = False

    @model_validator(mode="after")
    def _ordered(self) -> IngestedMaintenance:
        if self.end_ts <= self.start_ts:
            raise ValueError(f"维护结束 {self.end_ts} 不得早于或等于开始 {self.start_ts}")
        return self


class IngestedAircraft(BaseModel):
    """`aircraft.pdf` 的一架飞机。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    aircraft_id: str = Field(pattern=r"^AC\d{2}$")
    aircraft_type: AircraftType
    seats: int = Field(gt=0)
    daily_window_start: time
    daily_window_end: time
    turnaround_minutes: int = Field(ge=0)
    capable_missions: tuple[str, ...]
    maintenance: tuple[IngestedMaintenance, ...] = ()

    @model_validator(mode="after")
    def _window_ordered(self) -> IngestedAircraft:
        if self.daily_window_start >= self.daily_window_end:
            raise ValueError("每日可用窗起点须早于终点")
        return self


class IngestedAirspace(BaseModel):
    """`aircraft.pdf` 二、空域/航线资源与容量。`capacity` 是硬约束（S-10）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    airspace_id: str = Field(min_length=1, max_length=8)
    name: str = Field(min_length=1)
    capacity: int = Field(ge=1)
    #: 「绑定课目」列 —— 与 missions.pdf 的「空域/航线」列必须互相印证
    bound_missions: tuple[str, ...] = ()


class IngestedPrereq(BaseModel):
    """先修引用。`class` 类引用按 S-01 展开的动作在 compile_spec，不在这里。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prereq_ref: str = Field(min_length=1)
    ref_kind: PrereqRefKind


class IngestedMission(BaseModel):
    """`missions.pdf` 的一门课目。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mission_id: str = Field(pattern=r"^mission[A-H]-\d$")
    name: str = Field(min_length=1)
    mission_class: MissionClass
    kind: str = Field(min_length=1)
    duration_minutes: int = Field(gt=0)
    cycle_weeks: int = Field(gt=0)
    freq_days: int = Field(gt=0)
    #: A-1/A-2 的「（每周必飞）」标记 —— 约束3 的适用面
    weekly_required: bool = False
    #: 「带飞」列。**A-1/A-2 为否（D-1），学员 A 类单飞**
    dual_required: bool
    prereqs: tuple[IngestedPrereq, ...] = ()
    aircraft_types: tuple[AircraftType, ...]
    airspace_name: str = Field(min_length=1)
    frequency_text: str = ""

    @model_validator(mode="after")
    def _class_matches_id(self) -> IngestedMission:
        if self.mission_id[len("mission")] != self.mission_class:
            raise ValueError(f"{self.mission_id} 的类别应为 {self.mission_id[len('mission')]}")
        return self


class IngestedRunway(BaseModel):
    """跑道（v6 §1.3.5 / S-05）。

    **来源不是 PDF** —— 四份 PDF 都没有跑道表。权威来源是
    `rules/semantics.yaml` 的 S-05 开关，由业务方裁定。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runway_id: str = Field(pattern=r"^RWY-\d$")
    name: str = Field(min_length=1)
    aircraft_types: tuple[AircraftType, ...]

    @field_validator("aircraft_types")
    @classmethod
    def _nonempty(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("跑道服务机型不得为空")
        return v


class IngestedRule(BaseModel):
    """`rules.pdf` 的一条约束条文（切分单元，**禁止拆分**，v6 §5.3）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: int = Field(ge=1)
    title: str = Field(min_length=1)
    hard_soft: str = Field(min_length=1)
    text: str = Field(min_length=1)


class IngestedFacts(BaseModel):
    """一次摄取抽出的全部事实。落库前的最终形态。"""

    model_config = ConfigDict(extra="forbid")

    persons: tuple[IngestedPerson, ...] = ()
    aircraft: tuple[IngestedAircraft, ...] = ()
    airspaces: tuple[IngestedAirspace, ...] = ()
    missions: tuple[IngestedMission, ...] = ()
    runways: tuple[IngestedRunway, ...] = ()
    rules: tuple[IngestedRule, ...] = ()
    sources: tuple[SourceFile, ...] = ()

    def merged_with(self, other: IngestedFacts) -> IngestedFacts:
        """按实体类别合并两次抽取的结果（每份 PDF 只贡献自己那几类）。"""
        return IngestedFacts(
            persons=self.persons + other.persons,
            aircraft=self.aircraft + other.aircraft,
            airspaces=self.airspaces + other.airspaces,
            missions=self.missions + other.missions,
            runways=self.runways + other.runways,
            rules=self.rules + other.rules,
            sources=self.sources + other.sources,
        )


__all__ = [
    "AIRCRAFT_TYPES",
    "IDENTITIES",
    "QUAL_LEVELS",
    "AircraftType",
    "DocumentClass",
    "Identity",
    "IngestedAircraft",
    "IngestedAirspace",
    "IngestedFacts",
    "IngestedMaintenance",
    "IngestedMission",
    "IngestedPerson",
    "IngestedPrereq",
    "IngestedQualification",
    "IngestedRule",
    "IngestedRunway",
    "MissionClass",
    "PrereqRefKind",
    "QualLevel",
    "SourceFile",
]
