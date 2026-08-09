"""手工构造的最小事实集，供摄取层单测使用。

**刻意手写而不是从 PDF 抽**：负例（机组编成违例、孤立外键、容量越界）在真实
PDF 里不存在，必须造出来才能验证拦截逻辑真的会拦。正例走真实 PDF，见
`tests/unit/test_ingestion_parsers.py`。
"""

from __future__ import annotations

from datetime import date, datetime, time

from backend.ingestion.schema import (
    IngestedAircraft,
    IngestedAirspace,
    IngestedFacts,
    IngestedMaintenance,
    IngestedMission,
    IngestedPerson,
    IngestedPrereq,
    IngestedQualification,
    IngestedRule,
    IngestedRunway,
)


def make_mission(
    mission_id: str,
    *,
    airspace_name: str,
    dual_required: bool,
    freq_days: int = 7,
    cycle_weeks: int = 16,
    weekly_required: bool = False,
    prereqs: tuple[IngestedPrereq, ...] = (),
    aircraft_types: tuple[str, ...] = ("JL-8",),
    duration_minutes: int = 30,
) -> IngestedMission:
    return IngestedMission(
        mission_id=mission_id,
        name="测试课目",
        mission_class=mission_id[len("mission")],  # type: ignore[arg-type]
        kind="实装飞行课",
        duration_minutes=duration_minutes,
        cycle_weeks=cycle_weeks,
        freq_days=freq_days,
        weekly_required=weekly_required,
        dual_required=dual_required,
        prereqs=prereqs,
        aircraft_types=aircraft_types,  # type: ignore[arg-type]
        airspace_name=airspace_name,
        frequency_text=f"{cycle_weeks}周,每{freq_days}天≥1次",
    )


def make_person(
    person_id: str,
    *,
    identity: str,
    name: str | None = None,
    quals: tuple[tuple[str, str], ...],
    completed: tuple[str, ...] = (),
    aircraft_types: tuple[str, ...] = ("JL-8",),
    unavailable: tuple[date, ...] = (),
    recurrent_due_raw: str = "",
    expiries: dict[str, date] | None = None,
) -> IngestedPerson:
    expiry_map = expiries or {}
    return IngestedPerson(
        person_id=person_id,
        name=name or f"测试{person_id}",
        identity=identity,  # type: ignore[arg-type]
        aircraft_types=aircraft_types,  # type: ignore[arg-type]
        completed_missions=completed,
        unavailable_dates=unavailable,
        qualifications=tuple(
            IngestedQualification(
                person_id=person_id,
                mission_class=cls,  # type: ignore[arg-type]
                level=level,  # type: ignore[arg-type]
                expiry_date=expiry_map.get(cls),
            )
            for cls, level in quals
        ),
        recurrent_due_raw=recurrent_due_raw,
    )


def minimal_facts() -> IngestedFacts:
    """一份自洽的最小事实集：2 人 / 1 机 / 2 课目 / 2 空域 / 1 跑道 / 1 条规则。

    机组编成完全符合 §3.1.1：A 类带飞=否 → 学员「单飞」；B 类带飞=是 → 学员
    「带飞」；教员一律「教员」。
    """
    missions = (
        make_mission(
            "missionA-1",
            airspace_name="Small Area A",
            dual_required=False,
            freq_days=3,
            cycle_weeks=12,
            weekly_required=True,
        ),
        make_mission(
            "missionB-1",
            airspace_name="Route 2",
            dual_required=True,
            prereqs=(IngestedPrereq(prereq_ref="A类", ref_kind="class"),),
        ),
    )
    return IngestedFacts(
        persons=(
            make_person(
                "P01",
                identity="教员",
                quals=(("A", "教员"), ("B", "教员")),
                completed=("missionA-1", "missionB-1"),
            ),
            make_person(
                "P05",
                identity="学员",
                quals=(("A", "单飞"), ("B", "带飞")),
                completed=("missionA-1",),
            ),
        ),
        aircraft=(
            IngestedAircraft(
                aircraft_id="AC10",
                aircraft_type="JL-8",
                seats=2,
                daily_window_start=time(6, 0),
                daily_window_end=time(18, 0),
                turnaround_minutes=30,
                capable_missions=("missionA-1", "missionB-1"),
            ),
        ),
        airspaces=(
            IngestedAirspace(
                airspace_id="SAA",
                name="Small Area A",
                capacity=2,
                bound_missions=("missionA-1",),
            ),
            IngestedAirspace(
                airspace_id="RT2",
                name="Route 2",
                capacity=1,
                bound_missions=("missionB-1",),
            ),
        ),
        missions=missions,
        runways=(IngestedRunway(runway_id="RWY-1", name="跑道 1", aircraft_types=("JL-8",)),),
        rules=(
            IngestedRule(
                rule_id=1,
                title="时间一致性",
                hard_soft="硬约束",
                text="约束1(时间一致性)【硬约束】:测试。",
            ),
        ),
    )


def maintenance_fixture() -> IngestedMaintenance:
    return IngestedMaintenance(
        aircraft_id="AC73",
        start_ts=datetime(2026, 1, 9, 0, 0),
        end_ts=datetime(2026, 1, 9, 23, 59, 59),
        kind="定检维护",
        all_day=True,
    )


__all__ = [
    "maintenance_fixture",
    "make_mission",
    "make_person",
    "minimal_facts",
]
