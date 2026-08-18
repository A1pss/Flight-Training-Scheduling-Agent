"""落库：PG（事实）→ Chroma（向量化）→ 新 snapshot_id（v6 §5.1 最后一段）。

顺序不能反。PG 是事实唯一真源，Chroma 的 `field_map` 要回指 PG 的主键 ——
先写向量再写事实，中途失败就会留下指向不存在记录的向量。整个 PG 写入在一个
事务里，失败即回滚，**不做部分入库**（铁律 7）。

## `training_progress` 的物化

`cycle_start`（周期起点）**两级来源，没有静默默认值**：

1. **文件**：课目表的「课程开始日期」列 —— 上传的文件带了这一列就直接用，
   逐门课目可以不同，**不需要改任何代码**
2. **用户回答**：文件没给时，摄取会生成一条 `OpenQuestion`（`Q_cycle_start`）
   并**由人工确认门禁拒绝放行**，把问题抛给用户；用户答了才继续

两个都没有 → :func:`resolve_cycle_start` 抛 `FTS-1004`，绝不编日期。
理由：`cycle_start` 是 `training_progress` 主键的一部分（v6 §6.3），填错要迁移
全表；铁律 5「不假设」与铁律 10「有疑问就问」在这里是同一件事。

其余物化规则：

- `已完成课目` → `status=COMPLETED, completed_count=1, prereq_met=True`
- 学员在其**可及课目集**内未完成的课目 → `status=NOT_STARTED`，
  `prereq_met` / `blocked_reason` 按 S-01 求值
- `last_done_date` **一律 NULL** —— 原始 PDF 没有这个字段，由 S-12 在求解侧
  处理为「窗口从本周周一起算、不计欠账」。**写 `gap=999` 会让基准周假性不可行**
- `is_recurrent` / `recurrent_since` **M1 不写**（留 FALSE / NULL）：S-11 的落点
  在 `compile_spec_node`（v6 §6.3 明确写「排班当日由 compile_spec_node 写入」）

> **给 M2 的接口约定**：`training_progress` 是**物化视图**语义，不是独立真源。
> `compile_spec_node` 每次排班都会重算并覆盖 `prereq_met` / `blocked_reason` /
> `is_recurrent` / `recurrent_since`。先修判定两边都调
> :func:`backend.retrieval.prereq_cte.evaluate_prereq`，**不要各写一份**。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.core.errors import RequiredInputMissingError
from backend.core.logging import get_logger
from backend.ingestion.diff import content_sha256, normalize_facts
from backend.ingestion.schema import IngestedFacts, IngestedMission, SourceFile
from backend.models import (
    Aircraft,
    AircraftMaintenance,
    AircraftMissionCapability,
    Airspace,
    DataSnapshot,
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
    TrainingProgress,
)
from backend.retrieval.prereq_cte import evaluate_prereq

logger = get_logger(__name__)


def resolve_cycle_start(mission: IngestedMission, answered: date | None) -> date:
    """定出这门课目的周期起点。**两级来源，没有第三级兜底。**

    1. **文件**：课目表的「课程开始日期」列（`mission.cycle_start`）
    2. **用户回答**：`OpenQuestion` `Q_cycle_start` 的答案（对话/命令行/UI）

    两个都没有就是 bug —— 人工确认门禁本该在这之前就把问题抛给用户并拒绝放行，
    走到这里说明有人绕过了门禁。抛 `FTS-1004` 而不是编一个日期。
    """
    if mission.cycle_start is not None:
        return mission.cycle_start
    if answered is not None:
        return answered
    raise RequiredInputMissingError(
        f"{mission.mission_id} 的课程周期起点既不在文件里，也没有用户回答",
        details={"mission_id": mission.mission_id},
        suggestions=[
            "在课目文件里加一列「课程开始日期」，或在摄取时回答 Q_cycle_start",
            "这一步不设默认值：cycle_start 是 training_progress 主键的一部分",
        ],
    )


def make_snapshot_id(facts: IngestedFacts, *, prefix: str = "snap") -> str:
    """`snap_<内容哈希前 12 位>` —— **由内容决定，不含时间戳**（铁律 9）。

    同样的四份 PDF 重跑一次，snapshot_id 逐字节相同；内容变了才换 id。
    时间戳进 id 会让「重跑一次看看」变成「产生一个新快照」。
    """
    return f"{prefix}_{content_sha256(normalize_facts(facts))[:12]}"


def create_snapshot(
    session: Session,
    facts: IngestedFacts,
    *,
    snapshot_id: str | None = None,
    status: str = "PENDING",
    note: str = "",
) -> DataSnapshot:
    """建（或取回）快照头。"""
    sid = snapshot_id or make_snapshot_id(facts)
    existing = session.get(DataSnapshot, sid)
    if existing is not None:
        return existing

    normalized = normalize_facts(facts)
    snapshot = DataSnapshot(
        snapshot_id=sid,
        status=status,
        content_sha256=content_sha256(normalized),
        normalized_facts=normalized,
        note=note,
        source_manifest={
            "files": [
                {
                    "filename": s.filename,
                    "sha256": s.sha256,
                    "size_bytes": s.size_bytes,
                    "media_type": s.media_type,
                    "doc_class": s.doc_class,
                    "classifier": s.classifier,
                    "pages": s.pages,
                }
                for s in sorted(facts.sources, key=lambda s: s.filename)
            ]
        },
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def persist_facts(
    session: Session,
    snapshot_id: str,
    facts: IngestedFacts,
    *,
    answered_cycle_start: date | None = None,
) -> dict[str, int]:
    """把事实写进 PG。返回每张表的写入行数。

    先按 `snapshot_id` 清空再写，保证同一快照重跑是幂等的。
    """
    _purge_snapshot_facts(session, snapshot_id)

    counts: dict[str, int] = {}

    # 空域必须先落 —— missions 有外键指向它
    for airspace in facts.airspaces:
        session.add(
            Airspace(
                airspace_id=airspace.airspace_id,
                snapshot_id=snapshot_id,
                name=airspace.name,
                capacity=airspace.capacity,
            )
        )
    counts["airspaces"] = len(facts.airspaces)
    session.flush()

    airspace_by_name = {a.name: a.airspace_id for a in facts.airspaces}
    mission_type_rows = 0
    prereq_rows = 0
    for mission in facts.missions:
        session.add(
            Mission(
                mission_id=mission.mission_id,
                snapshot_id=snapshot_id,
                name=mission.name,
                mission_class=mission.mission_class,
                kind=mission.kind,
                duration_minutes=mission.duration_minutes,
                cycle_weeks=mission.cycle_weeks,
                freq_days=mission.freq_days,
                weekly_required=mission.weekly_required,
                dual_required=mission.dual_required,
                airspace_id=airspace_by_name[mission.airspace_name],
                frequency_text=mission.frequency_text,
            )
        )
        for aircraft_type in mission.aircraft_types:
            session.add(
                MissionAircraftType(
                    mission_id=mission.mission_id,
                    snapshot_id=snapshot_id,
                    aircraft_type=aircraft_type,
                )
            )
            mission_type_rows += 1
        for prereq in mission.prereqs:
            session.add(
                MissionPrereq(
                    mission_id=mission.mission_id,
                    snapshot_id=snapshot_id,
                    prereq_ref=prereq.prereq_ref,
                    ref_kind=prereq.ref_kind,
                )
            )
            prereq_rows += 1
    counts["missions"] = len(facts.missions)
    counts["mission_aircraft_types"] = mission_type_rows
    counts["mission_prereq"] = prereq_rows
    session.flush()

    capability_rows = 0
    maintenance_rows = 0
    for aircraft in facts.aircraft:
        session.add(
            Aircraft(
                aircraft_id=aircraft.aircraft_id,
                snapshot_id=snapshot_id,
                aircraft_type=aircraft.aircraft_type,
                seats=aircraft.seats,
                daily_window_start=aircraft.daily_window_start,
                daily_window_end=aircraft.daily_window_end,
                turnaround_minutes=aircraft.turnaround_minutes,
            )
        )
        session.flush()
        for mission_id in aircraft.capable_missions:
            session.add(
                AircraftMissionCapability(
                    aircraft_id=aircraft.aircraft_id,
                    snapshot_id=snapshot_id,
                    mission_id=mission_id,
                )
            )
            capability_rows += 1
        for entry in aircraft.maintenance:
            session.add(
                AircraftMaintenance(
                    aircraft_id=aircraft.aircraft_id,
                    snapshot_id=snapshot_id,
                    start_ts=entry.start_ts,
                    end_ts=entry.end_ts,
                    kind=entry.kind,
                    all_day=entry.all_day,
                )
            )
            maintenance_rows += 1
    counts["aircraft"] = len(facts.aircraft)
    counts["aircraft_mission_capability"] = capability_rows
    counts["aircraft_maintenance"] = maintenance_rows
    session.flush()

    qual_rows = 0
    person_type_rows = 0
    completed_rows = 0
    unavailable_rows = 0
    for person in facts.persons:
        session.add(
            Person(
                person_id=person.person_id,
                snapshot_id=snapshot_id,
                name=person.name,
                identity=person.identity,
            )
        )
        session.flush()
        for aircraft_type in person.aircraft_types:
            session.add(
                PersonAircraftType(
                    person_id=person.person_id,
                    snapshot_id=snapshot_id,
                    aircraft_type=aircraft_type,
                )
            )
            person_type_rows += 1
        for qual in person.qualifications:
            session.add(
                PersonQualification(
                    person_id=person.person_id,
                    snapshot_id=snapshot_id,
                    mission_class=qual.mission_class,
                    level=qual.level,
                    expiry_date=qual.expiry_date,
                )
            )
            qual_rows += 1
        for mission_id in person.completed_missions:
            session.add(
                PersonCompletedMission(
                    person_id=person.person_id,
                    snapshot_id=snapshot_id,
                    mission_id=mission_id,
                )
            )
            completed_rows += 1
        for day in person.unavailable_dates:
            session.add(
                PersonUnavailability(
                    person_id=person.person_id,
                    snapshot_id=snapshot_id,
                    unavailable_date=day,
                    reason="personnel.pdf「不可用日期」",
                )
            )
            unavailable_rows += 1
    counts["persons"] = len(facts.persons)
    counts["person_aircraft_types"] = person_type_rows
    counts["person_qualifications"] = qual_rows
    counts["person_completed_missions"] = completed_rows
    counts["person_unavailability"] = unavailable_rows
    session.flush()

    runway_type_rows = 0
    for runway in facts.runways:
        session.add(Runway(runway_id=runway.runway_id, snapshot_id=snapshot_id, name=runway.name))
        session.flush()
        for aircraft_type in runway.aircraft_types:
            session.add(
                RunwayAircraftType(
                    runway_id=runway.runway_id,
                    snapshot_id=snapshot_id,
                    aircraft_type=aircraft_type,
                )
            )
            runway_type_rows += 1
    counts["runways"] = len(facts.runways)
    counts["runway_aircraft_types"] = runway_type_rows
    session.flush()

    counts["training_progress"] = materialize_training_progress(
        session, snapshot_id, facts, answered_cycle_start=answered_cycle_start
    )
    return counts


def _purge_snapshot_facts(session: Session, snapshot_id: str) -> None:
    """按外键反序清空该快照下的事实，使重跑幂等。"""
    for model in (
        TrainingProgress,
        RunwayAircraftType,
        Runway,
        PersonUnavailability,
        PersonCompletedMission,
        PersonQualification,
        PersonAircraftType,
        Person,
        AircraftMaintenance,
        AircraftMissionCapability,
        Aircraft,
        MissionPrereq,
        MissionAircraftType,
        Mission,
        Airspace,
    ):
        session.execute(delete(model).where(model.snapshot_id == snapshot_id))
    session.flush()


def reachable_missions(
    aircraft_types: Sequence[str], qual_classes: Sequence[str], facts: IngestedFacts
) -> list[str]:
    """某人**可及**的课目集：持有类别资质 ∧ 持有机型资质。

    学员只持 JL-8 与 A/B/C/F 四类，而 D-1/E-1/E-2/G-1/H-1 机型均为 JL-9 且分属
    D/E/G/H 类 —— **双重排除**，所以学员可及集恒为
    `{A-1, A-2, B-1, B-2, C-1, C-2, F-1}`（v6 §1.4.1）。
    """
    owned_types = set(aircraft_types)
    owned_classes = set(qual_classes)
    return [
        m.mission_id
        for m in facts.missions
        if m.mission_class in owned_classes and owned_types & set(m.aircraft_types)
    ]


def materialize_training_progress(
    session: Session,
    snapshot_id: str,
    facts: IngestedFacts,
    *,
    answered_cycle_start: date | None = None,
) -> int:
    """物化 `training_progress`。返回写入行数。

    `answered_cycle_start` 是用户对 `Q_cycle_start` 的回答，只在**课目文件没给
    「课程开始日期」列**时才会用到；文件给了就逐门课目用文件里的值。

    ⚠️ **本表主键 `(person_id, mission_id, cycle_start)` 不含 `snapshot_id`**
    （v6 §6.3 原文如此），所以同一 (人, 课目, 周期起点) 在全库唯一 —— 两个快照
    没法各存一份。这正好对应它「**物化视图**」的定位：只保留最新一次物化的结果。
    因此写入前要按**主键**清掉旧行，而不是只按 `snapshot_id` 清 —— 后者在
    snapshot_id 变了（内容变更）而主键没变时会撞唯一约束。
    """
    mission_by_id = {m.mission_id: m for m in facts.missions}
    mission_ids = list(mission_by_id)
    prereq_map = {
        m.mission_id: [(p.prereq_ref, p.ref_kind) for p in m.prereqs] for m in facts.missions
    }
    now = datetime.now()
    rows = 0

    # 先按主键清掉任何快照下的同键旧行（见上方说明）
    keys = {
        (
            person.person_id,
            mission_id,
            resolve_cycle_start(mission_by_id[mission_id], answered_cycle_start),
        )
        for person in facts.persons
        for mission_id in set(person.completed_missions)
        | (
            set(
                reachable_missions(
                    person.aircraft_types, [q.mission_class for q in person.qualifications], facts
                )
            )
            if person.identity == "学员"
            else set()
        )
        if mission_id in mission_by_id
    }
    for person_id, mission_id, cycle in keys:
        session.execute(
            delete(TrainingProgress).where(
                TrainingProgress.person_id == person_id,
                TrainingProgress.mission_id == mission_id,
                TrainingProgress.cycle_start == cycle,
            )
        )
    session.flush()

    for person in facts.persons:
        completed = set(person.completed_missions)
        qual_classes = [q.mission_class for q in person.qualifications]

        # ① 已完成课目 —— 事实，直接落
        for mission_id in sorted(completed):
            mission = mission_by_id[mission_id]
            session.add(
                TrainingProgress(
                    person_id=person.person_id,
                    mission_id=mission_id,
                    cycle_start=resolve_cycle_start(mission, answered_cycle_start),
                    status="COMPLETED",
                    completed_count=1,
                    last_done_date=None,  # ★ PDF 无此字段，S-12 在求解侧处理
                    cycle_weeks=mission.cycle_weeks,
                    debt_count=0,
                    prereq_met=True,
                    blocked_reason=None,
                    is_recurrent=False,
                    recurrent_since=None,
                    updated_at=now,
                    snapshot_id=snapshot_id,
                )
            )
            rows += 1

        # ② 学员可及但未完成的课目 —— 先修按 S-01 求值
        if person.identity != "学员":
            continue
        for mission_id in sorted(reachable_missions(person.aircraft_types, qual_classes, facts)):
            if mission_id in completed:
                continue
            mission = mission_by_id[mission_id]
            met, missing = evaluate_prereq(prereq_map[mission_id], completed, mission_ids)
            session.add(
                TrainingProgress(
                    person_id=person.person_id,
                    mission_id=mission_id,
                    cycle_start=resolve_cycle_start(mission, answered_cycle_start),
                    status="NOT_STARTED",
                    completed_count=0,
                    last_done_date=None,
                    cycle_weeks=mission.cycle_weeks,
                    debt_count=0,
                    prereq_met=met,
                    blocked_reason=None if met else f"缺少先修：{'、'.join(missing)}",
                    is_recurrent=False,
                    recurrent_since=None,
                    updated_at=now,
                    snapshot_id=snapshot_id,
                )
            )
            rows += 1

    session.flush()
    return rows


def activate_snapshot(
    session: Session, snapshot_id: str, *, confirmed_by: str, note: str = ""
) -> None:
    """把快照置为 ACTIVE，并把此前的 ACTIVE 快照置为 SUPERSEDED。"""
    for old in session.scalars(
        select(DataSnapshot).where(
            DataSnapshot.status == "ACTIVE", DataSnapshot.snapshot_id != snapshot_id
        )
    ):
        old.status = "SUPERSEDED"

    snapshot = session.get(DataSnapshot, snapshot_id)
    if snapshot is None:
        raise LookupError(f"快照不存在：{snapshot_id}")
    snapshot.status = "ACTIVE"
    snapshot.confirmed_by = confirmed_by
    snapshot.confirmed_at = datetime.now()
    if note:
        snapshot.note = note
    session.flush()


def active_snapshot_id(session: Session) -> str | None:
    """当前 ACTIVE 快照的 id（Diff 的基线）。"""
    return session.scalars(
        select(DataSnapshot.snapshot_id).where(DataSnapshot.status == "ACTIVE")
    ).first()


def load_snapshot_normalized(
    session: Session, snapshot_id: str
) -> dict[str, dict[str, dict[str, Any]]]:
    """取某快照存档的规范化事实 —— **Diff 的基线**。

    为什么不用 :func:`load_normalized_from_db`：`rules.pdf` 的条文原文按
    v6 §6.1 只进 Chroma、不进 PG，从事实表回读永远拼不出 `rule` 那一类，
    Diff 每次都会凭空多报 14 条「新增规则」。
    """
    snapshot = session.get(DataSnapshot, snapshot_id)
    if snapshot is None:
        raise LookupError(f"快照不存在：{snapshot_id}")
    stored = snapshot.normalized_facts or {}
    if stored:
        return {k: dict(v) for k, v in stored.items()}
    # 早期快照没存这一列时退回事实表回读（rule 类会缺，但好过完全没有基线）
    return load_normalized_from_db(session, snapshot_id)


def load_normalized_from_db(
    session: Session, snapshot_id: str
) -> dict[str, dict[str, dict[str, Any]]]:
    """从**事实表**读回某快照并规范化。

    刻意**不复用**写入路径的对象，而是重新查库 —— 「写进去的」和「读出来的」
    必须能对上，这一步顺带就是一次往返自证（集成测试用它做回读校验）。

    ⚠️ 不含 `rule` 类（条文原文不落 PG），所以**不要拿它当 Diff 基线**，
    那是 :func:`load_snapshot_normalized` 的活。
    """
    out: dict[str, dict[str, dict[str, Any]]] = {
        "person": {},
        "aircraft": {},
        "mission": {},
        "airspace": {},
        "runway": {},
        "rule": {},
    }

    for person in session.scalars(select(Person).where(Person.snapshot_id == snapshot_id)):
        types = sorted(
            session.scalars(
                select(PersonAircraftType.aircraft_type).where(
                    PersonAircraftType.person_id == person.person_id,
                    PersonAircraftType.snapshot_id == snapshot_id,
                )
            )
        )
        completed = sorted(
            session.scalars(
                select(PersonCompletedMission.mission_id).where(
                    PersonCompletedMission.person_id == person.person_id,
                    PersonCompletedMission.snapshot_id == snapshot_id,
                )
            )
        )
        unavailable = sorted(
            d.isoformat()
            for d in session.scalars(
                select(PersonUnavailability.unavailable_date).where(
                    PersonUnavailability.person_id == person.person_id,
                    PersonUnavailability.snapshot_id == snapshot_id,
                )
            )
        )
        quals = sorted(
            (
                {
                    "mission_class": q.mission_class,
                    "level": q.level,
                    "expiry_date": q.expiry_date.isoformat() if q.expiry_date else None,
                }
                for q in session.scalars(
                    select(PersonQualification).where(
                        PersonQualification.person_id == person.person_id,
                        PersonQualification.snapshot_id == snapshot_id,
                    )
                )
            ),
            key=lambda q: str(q["mission_class"]),
        )
        out["person"][person.person_id] = {
            "name": person.name,
            "identity": person.identity,
            "aircraft_types": types,
            "completed_missions": completed,
            "unavailable_dates": unavailable,
            "qualifications": quals,
        }

    for aircraft in session.scalars(select(Aircraft).where(Aircraft.snapshot_id == snapshot_id)):
        out["aircraft"][aircraft.aircraft_id] = {
            "aircraft_type": aircraft.aircraft_type,
            "seats": aircraft.seats,
            "daily_window_start": aircraft.daily_window_start.strftime("%H:%M"),
            "daily_window_end": aircraft.daily_window_end.strftime("%H:%M"),
            "turnaround_minutes": aircraft.turnaround_minutes,
            "capable_missions": sorted(
                session.scalars(
                    select(AircraftMissionCapability.mission_id).where(
                        AircraftMissionCapability.aircraft_id == aircraft.aircraft_id,
                        AircraftMissionCapability.snapshot_id == snapshot_id,
                    )
                )
            ),
            "maintenance": sorted(
                (
                    {
                        "start_ts": m.start_ts.isoformat(),
                        "end_ts": m.end_ts.isoformat(),
                        "kind": m.kind,
                        "all_day": m.all_day,
                    }
                    for m in session.scalars(
                        select(AircraftMaintenance).where(
                            AircraftMaintenance.aircraft_id == aircraft.aircraft_id,
                            AircraftMaintenance.snapshot_id == snapshot_id,
                        )
                    )
                ),
                key=lambda m: str(m["start_ts"]),
            ),
        }

    airspace_name_by_id: dict[str, str] = {}
    for airspace in session.scalars(select(Airspace).where(Airspace.snapshot_id == snapshot_id)):
        airspace_name_by_id[airspace.airspace_id] = airspace.name
        out["airspace"][airspace.airspace_id] = {
            "name": airspace.name,
            "capacity": airspace.capacity,
            "bound_missions": [],
        }

    for mission in session.scalars(select(Mission).where(Mission.snapshot_id == snapshot_id)):
        out["mission"][mission.mission_id] = {
            "name": mission.name,
            "mission_class": mission.mission_class,
            "kind": mission.kind,
            "duration_minutes": mission.duration_minutes,
            "cycle_weeks": mission.cycle_weeks,
            "freq_days": mission.freq_days,
            "weekly_required": mission.weekly_required,
            "dual_required": mission.dual_required,
            "prereqs": sorted(
                (
                    {"prereq_ref": p.prereq_ref, "ref_kind": p.ref_kind}
                    for p in session.scalars(
                        select(MissionPrereq).where(
                            MissionPrereq.mission_id == mission.mission_id,
                            MissionPrereq.snapshot_id == snapshot_id,
                        )
                    )
                ),
                key=lambda p: str(p["prereq_ref"]),
            ),
            "aircraft_types": sorted(
                session.scalars(
                    select(MissionAircraftType.aircraft_type).where(
                        MissionAircraftType.mission_id == mission.mission_id,
                        MissionAircraftType.snapshot_id == snapshot_id,
                    )
                )
            ),
            "airspace_name": airspace_name_by_id[mission.airspace_id],
        }
        out["airspace"][mission.airspace_id]["bound_missions"].append(mission.mission_id)

    for entry in out["airspace"].values():
        entry["bound_missions"] = sorted(entry["bound_missions"])

    for runway in session.scalars(select(Runway).where(Runway.snapshot_id == snapshot_id)):
        out["runway"][runway.runway_id] = {
            "name": runway.name,
            "aircraft_types": sorted(
                session.scalars(
                    select(RunwayAircraftType.aircraft_type).where(
                        RunwayAircraftType.runway_id == runway.runway_id,
                        RunwayAircraftType.snapshot_id == snapshot_id,
                    )
                )
            ),
        }

    return out


def source_files_digest(sources: Sequence[SourceFile]) -> str:
    """全部源文件 sha256 的聚合指纹，进审计日志便于溯源。"""
    joined = "|".join(f"{s.filename}:{s.sha256}" for s in sorted(sources, key=lambda s: s.filename))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


__all__ = [
    "activate_snapshot",
    "active_snapshot_id",
    "create_snapshot",
    "load_normalized_from_db",
    "load_snapshot_normalized",
    "make_snapshot_id",
    "materialize_training_progress",
    "persist_facts",
    "reachable_missions",
    "resolve_cycle_start",
    "source_files_digest",
]
