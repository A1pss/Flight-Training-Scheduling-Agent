"""语义记忆：**精确查询，不走向量**（v6 §6.2 第一行）。

| 承载 | 存储 | 检索方式 |
|---|---|---|
| 人员资质、机型能力、课目定义、空域容量、已完成课目、`last_done_date` | PG | **精确查询，不走向量** |

「不走向量」这五个字是本模块存在的全部理由，也是 v6 §12.4 那张表里语义类
120 条探针能报 ≥98% 的结构性依靠：

> 撑住总体数的是**语义类 120 条走 SQL 精确通道**：这一路不依赖模型，
> 是把加权值拉过交付线的结构性依靠。

「何超」与「高超」在 embedding 空间里极近（§6.5.1），靠调嵌入模型解决不了；
而在 `persons` 表里它们是 `P08` 与 `P02` 两行，`WHERE person_id = 'P08'`
不存在混淆的可能。

## 每个返回值都带 `table` + `pk`

生成层要能回答「这个数是从哪张表哪一行读出来的」（§6.5.2 第 ④ 步的事实核验）。
所以本模块的每个 dataclass 都有 `cite()`，把自己变成一条可核对的引用。

## 本模块不判定「能不能排」

它只回答事实：这个人有什么资质、这门课要不要带飞、这架飞机什么机型。
**先修达标与否、能不能排，是 `retrieval/structured.py` 组合这些事实之后的结论**，
且先修判定只有一份实现（`retrieval/prereq_cte.py::evaluate_prereq`，v6 §6.1）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.entities import (
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
from backend.models.progress import TrainingProgress
from backend.retrieval.documents import RetrievedDoc, structured_doc

# ─────────────────────────────────────────────────────────────────────
# 事实类型
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PersonFact:
    """一个人的身份、机型资质与类别资质。"""

    person_id: str
    name: str
    identity: str
    aircraft_types: tuple[str, ...]
    #: 类别 → (等级, 到期日)。到期日为 None 表示不设到期（教员/学员均如此）
    qualifications: Mapping[str, tuple[str, date | None]]

    def sentence(self) -> str:
        quals = "、".join(
            f"{cls}类/{level}" + (f"/到期 {expiry.isoformat()}" if expiry else "")
            for cls, (level, expiry) in sorted(self.qualifications.items())
        )
        return (
            f"{self.name}（{self.person_id}）身份为{self.identity}，"
            f"可飞机型 {'、'.join(self.aircraft_types) or '无'}；资质：{quals or '无'}"
        )

    def doc(self) -> RetrievedDoc:
        return structured_doc(
            f"pg:persons:{self.person_id}",
            self.sentence(),
            table="persons",
            pk={"person_id": self.person_id},
        )


@dataclass(frozen=True)
class QualificationFact:
    """一条类别资质。**到期日是 M1 探针的那个数**（刘斌 C 类 2026-01-07）。"""

    person_id: str
    person_name: str
    identity: str
    mission_class: str
    level: str
    expiry_date: date | None

    def expired_at(self, at: date) -> bool:
        """到期日**当日仍可执行**（`rules.pdf` 约束2 原文），次日起算过期。"""
        return self.expiry_date is not None and at > self.expiry_date

    def sentence(self) -> str:
        expiry = self.expiry_date.isoformat() if self.expiry_date else "不设到期"
        return (
            f"{self.person_name}（{self.person_id}）的 {self.mission_class} 类资质"
            f"等级为{self.level}，复训到期日 {expiry}"
        )

    def doc(self) -> RetrievedDoc:
        return structured_doc(
            f"pg:person_qualifications:{self.person_id}:{self.mission_class}",
            self.sentence(),
            table="person_qualifications",
            pk={"person_id": self.person_id, "mission_class": self.mission_class},
            valid_to=self.expiry_date.isoformat() if self.expiry_date else None,
        )


@dataclass(frozen=True)
class AircraftFact:
    """一架飞机。**`aircraft_type` 是 M2 探针的那个值**（AC73 = JL-8）。"""

    aircraft_id: str
    aircraft_type: str
    seats: int
    turnaround_minutes: int
    missions: tuple[str, ...]
    runways: tuple[str, ...]

    def sentence(self) -> str:
        return (
            f"{self.aircraft_id} 的机型是 {self.aircraft_type}，{self.seats} 座，"
            f"周转时间 {self.turnaround_minutes} 分钟；"
            f"可用跑道 {'、'.join(self.runways) or '无'}；"
            f"可执行课目 {'、'.join(self.missions) or '无'}"
        )

    def doc(self) -> RetrievedDoc:
        return structured_doc(
            f"pg:aircraft:{self.aircraft_id}",
            self.sentence(),
            table="aircraft",
            pk={"aircraft_id": self.aircraft_id},
        )


@dataclass(frozen=True)
class MissionFact:
    """一门课目。**`dual_required` 是 M4 探针的那个值**（A-1 带飞=否）。"""

    mission_id: str
    name: str
    mission_class: str
    duration_minutes: int
    cycle_weeks: int
    freq_days: int
    dual_required: bool
    airspace_id: str
    aircraft_types: tuple[str, ...]
    prereqs: tuple[tuple[str, str], ...]

    def sentence(self) -> str:
        dual = "是" if self.dual_required else "否"
        prereq = "、".join(ref for ref, _ in self.prereqs) or "无"
        return (
            f"{self.mission_id}（{self.name}）属 {self.mission_class} 类，"
            f"时长 {self.duration_minutes} 分钟，周期 {self.cycle_weeks} 周，"
            f"频率每 {self.freq_days} 天 1 次，带飞 {dual}，"
            f"空域 {self.airspace_id}，机型 {'、'.join(self.aircraft_types) or '无'}，"
            f"先修 {prereq}"
        )

    def doc(self) -> RetrievedDoc:
        return structured_doc(
            f"pg:missions:{self.mission_id}",
            self.sentence(),
            table="missions",
            pk={"mission_id": self.mission_id},
        )


@dataclass(frozen=True)
class AirspaceFact:
    """一个空域及其**同时段容量**（S-10，硬约束）。"""

    airspace_id: str
    name: str
    capacity: int

    def sentence(self) -> str:
        return f"空域 {self.airspace_id}（{self.name}）的同时段容量为 {self.capacity} 架次"

    def doc(self) -> RetrievedDoc:
        return structured_doc(
            f"pg:airspaces:{self.airspace_id}",
            self.sentence(),
            table="airspaces",
            pk={"airspace_id": self.airspace_id},
        )


@dataclass(frozen=True)
class ProgressFact:
    """一条训练进度。`last_done_date` 是跨周频率约束的锚点（§6.3）。"""

    person_id: str
    mission_id: str
    status: str
    completed_count: int
    last_done_date: date | None
    prereq_met: bool
    blocked_reason: str
    is_recurrent: bool

    def sentence(self) -> str:
        anchor = self.last_done_date.isoformat() if self.last_done_date else "无记录（走 S-12）"
        recurrent = "，处于 S-11 强制复训周期" if self.is_recurrent else ""
        return (
            f"{self.person_id} 的 {self.mission_id} 进度为 {self.status}，"
            f"已完成 {self.completed_count} 次，上次执行日 {anchor}，"
            f"先修{'达标' if self.prereq_met else '未达标'}{recurrent}"
        )

    def doc(self) -> RetrievedDoc:
        return structured_doc(
            f"pg:training_progress:{self.person_id}:{self.mission_id}",
            self.sentence(),
            table="training_progress",
            pk={"person_id": self.person_id, "mission_id": self.mission_id},
        )


@dataclass(frozen=True)
class MaintenanceFact:
    """一次维修窗口。"""

    aircraft_id: str
    start_ts: datetime
    end_ts: datetime
    kind: str
    all_day: bool

    def sentence(self) -> str:
        scope = "全天" if self.all_day else f"{self.start_ts:%H:%M}-{self.end_ts:%H:%M}"
        return f"{self.aircraft_id} 在 {self.start_ts:%Y-%m-%d} {scope} 有{self.kind}"

    def doc(self) -> RetrievedDoc:
        return structured_doc(
            f"pg:aircraft_maintenance:{self.aircraft_id}:{self.start_ts:%Y%m%dT%H%M}",
            self.sentence(),
            table="aircraft_maintenance",
            pk={"aircraft_id": self.aircraft_id, "start_ts": self.start_ts.isoformat()},
            valid_from=self.start_ts.isoformat(),
            valid_to=self.end_ts.isoformat(),
        )


@dataclass(frozen=True)
class UnavailabilityFact:
    """某人某天不可用。"""

    person_id: str
    person_name: str
    unavailable_date: date
    reason: str

    def sentence(self) -> str:
        why = f"（{self.reason}）" if self.reason else ""
        return f"{self.person_name}（{self.person_id}）在 {self.unavailable_date} 不可用{why}"

    def doc(self) -> RetrievedDoc:
        return structured_doc(
            f"pg:person_unavailability:{self.person_id}:{self.unavailable_date}",
            self.sentence(),
            table="person_unavailability",
            pk={"person_id": self.person_id, "unavailable_date": self.unavailable_date.isoformat()},
            valid_from=self.unavailable_date.isoformat(),
        )


# ─────────────────────────────────────────────────────────────────────
# 查询
# ─────────────────────────────────────────────────────────────────────
def person_fact(session: Session, snapshot_id: str, person_id: str) -> PersonFact | None:
    """一个人的完整画像。查不到返回 `None` —— **不返回一个空壳**。"""
    row = session.get(Person, {"person_id": person_id, "snapshot_id": snapshot_id})
    if row is None:
        return None
    types = tuple(
        sorted(
            session.scalars(
                select(PersonAircraftType.aircraft_type).where(
                    PersonAircraftType.person_id == person_id,
                    PersonAircraftType.snapshot_id == snapshot_id,
                )
            )
        )
    )
    quals = {
        q.mission_class: (q.level, q.expiry_date)
        for q in session.scalars(
            select(PersonQualification).where(
                PersonQualification.person_id == person_id,
                PersonQualification.snapshot_id == snapshot_id,
            )
        )
    }
    return PersonFact(
        person_id=row.person_id,
        name=row.name,
        identity=row.identity,
        aircraft_types=types,
        qualifications=quals,
    )


def all_persons(session: Session, snapshot_id: str) -> list[PersonFact]:
    """当前快照的全部人员。名录与消解都从这里取 —— **不写死人数**。"""
    ids = sorted(session.scalars(select(Person.person_id).where(Person.snapshot_id == snapshot_id)))
    return [f for pid in ids if (f := person_fact(session, snapshot_id, pid)) is not None]


def qualification_facts(
    session: Session, snapshot_id: str, person_id: str, *, mission_class: str | None = None
) -> list[QualificationFact]:
    """某人的类别资质。给了 `mission_class` 就只取那一类。"""
    person = session.get(Person, {"person_id": person_id, "snapshot_id": snapshot_id})
    if person is None:
        return []
    stmt = select(PersonQualification).where(
        PersonQualification.person_id == person_id,
        PersonQualification.snapshot_id == snapshot_id,
    )
    if mission_class:
        stmt = stmt.where(PersonQualification.mission_class == mission_class)
    return [
        QualificationFact(
            person_id=person_id,
            person_name=person.name,
            identity=person.identity,
            mission_class=q.mission_class,
            level=q.level,
            expiry_date=q.expiry_date,
        )
        for q in sorted(session.scalars(stmt), key=lambda q: q.mission_class)
    ]


def aircraft_fact(session: Session, snapshot_id: str, aircraft_id: str) -> AircraftFact | None:
    """一架飞机的完整画像（含机型 —— M2 探针要的就是这个字段）。"""
    row = session.get(Aircraft, {"aircraft_id": aircraft_id, "snapshot_id": snapshot_id})
    if row is None:
        return None
    missions = tuple(
        sorted(
            session.scalars(
                select(AircraftMissionCapability.mission_id).where(
                    AircraftMissionCapability.aircraft_id == aircraft_id,
                    AircraftMissionCapability.snapshot_id == snapshot_id,
                )
            )
        )
    )
    runways = tuple(
        sorted(
            session.scalars(
                select(RunwayAircraftType.runway_id).where(
                    RunwayAircraftType.aircraft_type == row.aircraft_type,
                    RunwayAircraftType.snapshot_id == snapshot_id,
                )
            )
        )
    )
    return AircraftFact(
        aircraft_id=row.aircraft_id,
        aircraft_type=row.aircraft_type,
        seats=row.seats,
        turnaround_minutes=row.turnaround_minutes,
        missions=missions,
        runways=runways,
    )


def all_aircraft(session: Session, snapshot_id: str) -> list[AircraftFact]:
    ids = sorted(
        session.scalars(select(Aircraft.aircraft_id).where(Aircraft.snapshot_id == snapshot_id))
    )
    return [f for aid in ids if (f := aircraft_fact(session, snapshot_id, aid)) is not None]


def mission_fact(session: Session, snapshot_id: str, mission_id: str) -> MissionFact | None:
    row = session.get(Mission, {"mission_id": mission_id, "snapshot_id": snapshot_id})
    if row is None:
        return None
    types = tuple(
        sorted(
            session.scalars(
                select(MissionAircraftType.aircraft_type).where(
                    MissionAircraftType.mission_id == mission_id,
                    MissionAircraftType.snapshot_id == snapshot_id,
                )
            )
        )
    )
    prereqs = tuple(
        sorted(
            (p.prereq_ref, p.ref_kind)
            for p in session.scalars(
                select(MissionPrereq).where(
                    MissionPrereq.mission_id == mission_id,
                    MissionPrereq.snapshot_id == snapshot_id,
                )
            )
        )
    )
    return MissionFact(
        mission_id=row.mission_id,
        name=row.name,
        mission_class=row.mission_class,
        duration_minutes=row.duration_minutes,
        cycle_weeks=row.cycle_weeks,
        freq_days=row.freq_days,
        dual_required=row.dual_required,
        airspace_id=row.airspace_id,
        aircraft_types=types,
        prereqs=prereqs,
    )


def all_missions(session: Session, snapshot_id: str) -> list[MissionFact]:
    ids = sorted(
        session.scalars(select(Mission.mission_id).where(Mission.snapshot_id == snapshot_id))
    )
    return [f for mid in ids if (f := mission_fact(session, snapshot_id, mid)) is not None]


def missions_of_class(session: Session, snapshot_id: str, mission_class: str) -> list[MissionFact]:
    """某一类的全部课目（S-01 的类展开在 `prereq_cte` 里做，这里只是查询）。"""
    return [m for m in all_missions(session, snapshot_id) if m.mission_class == mission_class]


def airspace_facts(session: Session, snapshot_id: str) -> list[AirspaceFact]:
    return [
        AirspaceFact(airspace_id=a.airspace_id, name=a.name, capacity=a.capacity)
        for a in sorted(
            session.scalars(select(Airspace).where(Airspace.snapshot_id == snapshot_id)),
            key=lambda a: a.airspace_id,
        )
    ]


def runway_ids(session: Session, snapshot_id: str) -> tuple[str, ...]:
    return tuple(
        sorted(session.scalars(select(Runway.runway_id).where(Runway.snapshot_id == snapshot_id)))
    )


def completed_missions(session: Session, snapshot_id: str, person_id: str) -> tuple[str, ...]:
    """已完成课目 —— **先修判定的事实来源**（v6 §6.1 / `Z-16`）。"""
    return tuple(
        sorted(
            session.scalars(
                select(PersonCompletedMission.mission_id).where(
                    PersonCompletedMission.person_id == person_id,
                    PersonCompletedMission.snapshot_id == snapshot_id,
                )
            )
        )
    )


def progress_facts(
    session: Session, person_id: str, *, mission_id: str | None = None
) -> list[ProgressFact]:
    """训练进度（含 `last_done_date` 锚点）。

    ⚠️ **本表不带 `snapshot_id` 过滤**，因为它是物化视图、主键里没有
    `snapshot_id`（v6 §6.3.2）。按快照过滤会查不到任何东西。
    """
    stmt = select(TrainingProgress).where(TrainingProgress.person_id == person_id)
    if mission_id:
        stmt = stmt.where(TrainingProgress.mission_id == mission_id)
    return [
        ProgressFact(
            person_id=p.person_id,
            mission_id=p.mission_id,
            status=p.status,
            completed_count=p.completed_count,
            last_done_date=p.last_done_date,
            prereq_met=p.prereq_met,
            blocked_reason=p.blocked_reason or "",
            is_recurrent=p.is_recurrent,
        )
        for p in sorted(session.scalars(stmt), key=lambda p: p.mission_id)
    ]


def maintenance_facts(
    session: Session, snapshot_id: str, *, aircraft_id: str | None = None
) -> list[MaintenanceFact]:
    stmt = select(AircraftMaintenance).where(AircraftMaintenance.snapshot_id == snapshot_id)
    if aircraft_id:
        stmt = stmt.where(AircraftMaintenance.aircraft_id == aircraft_id)
    return [
        MaintenanceFact(
            aircraft_id=m.aircraft_id,
            start_ts=m.start_ts,
            end_ts=m.end_ts,
            kind=m.kind,
            all_day=m.all_day,
        )
        for m in sorted(session.scalars(stmt), key=lambda m: (m.aircraft_id, m.start_ts))
    ]


def unavailability_facts(
    session: Session, snapshot_id: str, *, person_id: str | None = None
) -> list[UnavailabilityFact]:
    stmt = select(PersonUnavailability, Person.name).join(
        Person,
        (Person.person_id == PersonUnavailability.person_id)
        & (Person.snapshot_id == PersonUnavailability.snapshot_id),
    )
    stmt = stmt.where(PersonUnavailability.snapshot_id == snapshot_id)
    if person_id:
        stmt = stmt.where(PersonUnavailability.person_id == person_id)
    rows = session.execute(stmt).all()
    facts = [
        UnavailabilityFact(
            person_id=row[0].person_id,
            person_name=row[1],
            unavailable_date=row[0].unavailable_date,
            reason=row[0].reason or "",
        )
        for row in rows
    ]
    return sorted(facts, key=lambda f: (f.person_id, f.unavailable_date))


def prereq_map(session: Session, snapshot_id: str) -> dict[str, list[tuple[str, str]]]:
    """课目 → 先修引用列表，喂给 `retrieval.prereq_cte` 的类展开。"""
    out: dict[str, list[tuple[str, str]]] = {}
    for row in session.scalars(
        select(MissionPrereq).where(MissionPrereq.snapshot_id == snapshot_id)
    ):
        out.setdefault(row.mission_id, []).append((row.prereq_ref, row.ref_kind))
    for refs in out.values():
        refs.sort()
    return out


def facts_to_docs(facts: Sequence[Any]) -> list[RetrievedDoc]:
    """把一批事实转成路 A 文档（保持传入顺序 —— 顺序即优先级）。"""
    return [fact.doc() for fact in facts]


__all__ = [
    "AircraftFact",
    "AirspaceFact",
    "MaintenanceFact",
    "MissionFact",
    "PersonFact",
    "ProgressFact",
    "QualificationFact",
    "UnavailabilityFact",
    "aircraft_fact",
    "airspace_facts",
    "all_aircraft",
    "all_missions",
    "all_persons",
    "completed_missions",
    "facts_to_docs",
    "maintenance_facts",
    "mission_fact",
    "missions_of_class",
    "person_fact",
    "prereq_map",
    "progress_facts",
    "qualification_facts",
    "runway_ids",
    "unavailability_facts",
]
