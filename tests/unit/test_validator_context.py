"""校验器只读事实视图的单元测试（`backend/validator/context.py`）。

这份 context 是**校验器自己装配的**，与求解侧各读各的（v6 §4.1）。所以这里要测
的不只是「字段搬对了」，还有几处会直接影响判定的语义：多轮周期取哪一行、
`week_start` 必须是周一、以及派生查询不许把基准数据写死成常量。
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from backend.core.errors import RequiredInputMissingError
from backend.models.progress import TrainingProgress
from backend.validator.context import (
    ContextRows,
    MaintenanceWindow,
    ValidationContext,
    context_from_rows,
)
from tests.fixtures.validator_facts import SNAPSHOT, WEEK_START, baseline_context, baseline_rows


@pytest.fixture(scope="module")
def ctx() -> ValidationContext:
    return baseline_context()


def test_entities_match_the_v6_baseline_panorama(ctx: ValidationContext) -> None:
    """v6 §1.3 的实体全景（**基准回归护栏**，不是系统上限）。"""
    assert len(ctx.persons) == 8
    assert len(ctx.aircraft) == 8
    assert len(ctx.missions) == 12
    assert len(ctx.airspaces) == 6
    assert len(ctx.runways) == 2
    identities = sorted(p.identity for p in ctx.persons.values())
    assert identities.count("教员") == 3
    assert identities.count("成熟飞行员") == 1
    assert identities.count("学员") == 4


def test_ac73_is_a_jl8(ctx: ValidationContext) -> None:
    """⚠️ AC73 是 JL-8，不是 JL-9；JL-9 只有 AC84/AC95 两架（v6 F1/F2）。"""
    assert ctx.aircraft["AC73"].aircraft_type == "JL-8"
    jl9 = sorted(a.aircraft_id for a in ctx.aircraft.values() if a.aircraft_type == "JL-9")
    assert jl9 == ["AC84", "AC95"]


def test_runway_service_types_are_not_one_per_type(ctx: ValidationContext) -> None:
    """RWY-1 服务 JL-8 与 JL-9；RWY-2 只服务 JL-8。"""
    assert ctx.runways["RWY-1"].aircraft_types == frozenset({"JL-8", "JL-9"})
    assert ctx.runways["RWY-2"].aircraft_types == frozenset({"JL-8"})
    assert ctx.runways_for_type("JL-9") == frozenset({"RWY-1"})
    assert ctx.runways_for_type("JL-8") == frozenset({"RWY-1", "RWY-2"})


def test_turnaround_and_capacity_come_from_the_data(ctx: ValidationContext) -> None:
    assert ctx.aircraft["AC10"].turnaround_minutes == 30
    assert ctx.aircraft["AC84"].turnaround_minutes == 40
    assert [ctx.airspaces[a].capacity for a in ("SAA", "SAB", "IFR", "RT1", "RT2", "RNG")] == [
        2,
        2,
        1,
        1,
        1,
        1,
    ]


def test_weekly_required_classes_are_derived_from_missions(ctx: ValidationContext) -> None:
    """约束3 的适用类别来自课目表，不是写死的「A 类」。"""
    assert ctx.weekly_required_classes() == ("A",)
    assert ctx.missions_of_class("A") == ("missionA-1", "missionA-2")


def test_person_facts_carry_quals_unavailability_and_completions(ctx: ValidationContext) -> None:
    liu = ctx.persons["P04"]
    assert liu.identity == "成熟飞行员"
    assert liu.qualification_of("C") is not None
    assert liu.qualification_of("C").expiry_date == date(2026, 1, 7)  # type: ignore[union-attr]
    assert ctx.persons["P03"].unavailable_dates == frozenset({date(2026, 1, 5)})
    assert ctx.persons["P08"].completed_missions == frozenset({"missionA-1"})
    assert not ctx.persons["P01"].is_student and ctx.persons["P08"].is_student


def test_progress_carries_s11_and_s12_state(ctx: ValidationContext) -> None:
    recurrent = ctx.progress_of("P04", "missionC-1")
    assert recurrent is not None
    assert recurrent.is_recurrent and recurrent.recurrent_since == date(2026, 1, 8)
    assert recurrent.completed  # 12 门全完成，但 S-11 让它仍受约束13 管辖
    blocked = ctx.progress_of("P08", "missionC-1")
    assert blocked is not None and not blocked.prereq_met
    assert blocked.last_done_date is None  # S-12：原始 PDF 没有这个字段


def test_airspace_of_uses_the_mission_table(ctx: ValidationContext) -> None:
    assert ctx.airspace_of("missionA-2") == "SAB"
    assert ctx.airspace_of("missionZ-9") is None


def test_day_offset_and_sorted_persons(ctx: ValidationContext) -> None:
    assert ctx.day_offset(date(2026, 1, 5)) == 0
    assert ctx.day_offset(date(2026, 1, 11)) == 6
    assert [p.person_id for p in ctx.sorted_persons()][:3] == ["P01", "P02", "P03"]
    assert [p.person_id for p in ctx.students()] == ["P05", "P06", "P07", "P08"]


def test_week_start_must_be_monday() -> None:
    with pytest.raises(RequiredInputMissingError, match="week_start 必须是周一"):
        context_from_rows(baseline_rows(), week_start=date(2026, 1, 7), snapshot_id=SNAPSHOT)


def test_latest_cycle_not_after_week_start_wins() -> None:
    """同一 (人, 课目) 有多轮周期时取 `cycle_start ≤ week_start` 的最后一轮。"""
    rows = baseline_rows()
    for extra_start, status in (
        (date(2025, 12, 1), "IN_PROGRESS"),
        (date(2026, 2, 2), "COMPLETED"),
    ):
        rows.progress.append(  # type: ignore[attr-defined]
            TrainingProgress(
                person_id="P05",
                mission_id="missionC-2",
                cycle_start=extra_start,
                status=status,
                completed_count=0,
                last_done_date=None,
                cycle_weeks=16,
                debt_count=0,
                prereq_met=True,
                blocked_reason=None,
                is_recurrent=False,
                recurrent_since=None,
                snapshot_id=SNAPSHOT,
            )
        )
    ctx = context_from_rows(rows, week_start=WEEK_START, snapshot_id=SNAPSHOT)
    chosen = ctx.progress_of("P05", "missionC-2")
    assert chosen is not None
    assert chosen.cycle_start == WEEK_START  # 不是 2025-12-01，也不是 2026-02-02


def test_maintenance_window_overlap_is_half_open() -> None:
    window = MaintenanceWindow(start=datetime(2026, 1, 9, 8, 0), end=datetime(2026, 1, 9, 10, 0))
    day = date(2026, 1, 9)
    assert window.overlaps(day, time(9, 0), time(9, 30))
    assert window.overlaps(day, time(7, 30), time(8, 30))
    assert not window.overlaps(day, time(10, 0), time(10, 30))  # 贴边不算
    assert not window.overlaps(day, time(7, 0), time(8, 0))
    assert not window.overlaps(date(2026, 1, 10), time(9, 0), time(9, 30))


def test_empty_context_is_constructible() -> None:
    """空快照也要能装配 —— 校验器不该在「什么都没有」时炸掉。"""
    ctx = context_from_rows(ContextRows(), week_start=WEEK_START)
    assert ctx.persons == {} and ctx.weekly_required_classes() == ()
