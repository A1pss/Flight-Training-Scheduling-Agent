"""集成测试：PG → `ValidationContext` 的真实装配路径（v6 §12.1）。

**直连裸装 PG（127.0.0.1:5433），不用 testcontainers**。用例在**自己建的**独立
snapshot 下跑、跑完清干净 —— 不假设「库里已经有一个 ACTIVE 快照」这种环境外状态
（CLAUDE.md §6 的那条踩坑记录）。

单元测试用 `context_from_rows` 覆盖装配逻辑；这里补的是 `fetch_rows` 的那一段
SQL：列名写错、外键取错快照、多值属性没聚合，只有真连库才会现形。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.core.db import session_scope
from backend.models.versioning import DataSnapshot
from backend.validator.checks import run_all_checks
from backend.validator.context import ContextRows, load_context
from tests.fixtures.validator_facts import WEEK_START, baseline_rows, compliant_plan

pytestmark = pytest.mark.integration

#: 落库顺序（外键依赖：空域 → 课目 → 人员/飞机/跑道 → 各从表 → 进度）
_INSERT_ORDER: tuple[str, ...] = (
    "airspaces",
    "missions",
    "mission_aircraft_types",
    "mission_prereq",
    "persons",
    "person_aircraft_types",
    "person_qualifications",
    "person_unavailability",
    "person_completed",
    "aircraft",
    "aircraft_capability",
    "maintenance",
    "runways",
    "runway_aircraft_types",
    "progress",
)

#: 见 `loaded_snapshot` 里的说明：避开 `training_progress` 的跨快照主键冲突
_TEST_CYCLE_START = date(2020, 1, 6)

#: 清理顺序（落库顺序的逆序）
_CLEANUP_TABLES: tuple[str, ...] = (
    "training_progress",
    "runway_aircraft_types",
    "runways",
    "aircraft_maintenance",
    "aircraft_mission_capability",
    "aircraft",
    "person_unavailability",
    "person_completed_missions",
    "person_qualifications",
    "person_aircraft_types",
    "persons",
    "mission_prereq",
    "mission_aircraft_types",
    "missions",
    "airspaces",
)


def _purge(session: Session, snapshot_id: str) -> None:
    for table in _CLEANUP_TABLES:
        session.execute(text(f"DELETE FROM {table} WHERE snapshot_id = :s"), {"s": snapshot_id})
    session.execute(text("DELETE FROM data_snapshots WHERE snapshot_id = :s"), {"s": snapshot_id})
    session.flush()


@pytest.fixture
def loaded_snapshot() -> Iterator[str]:
    """把手工事实写进一个独立快照，跑完删掉。"""
    snapshot_id = f"snap_m2b_{uuid.uuid4().hex[:10]}"
    rows: ContextRows = baseline_rows()
    try:
        with session_scope() as session:
            session.add(
                DataSnapshot(
                    snapshot_id=snapshot_id,
                    status="ACTIVE",
                    source_manifest={},
                    content_sha256="0" * 64,
                    normalized_facts={},
                    note="M2-B 集成测试用",
                )
            )
            session.flush()
            for field in _INSERT_ORDER:
                for row in getattr(rows, field):
                    row.snapshot_id = snapshot_id
                    if field == "progress":
                        # ⚠️ `training_progress` 的主键是 (person_id, mission_id,
                        # cycle_start)，**不含 snapshot_id**（v6 §6.3 的 DDL 如此）。
                        # 于是同一 (人, 课目, 周期) 在不同快照之间会撞主键 —— 本地
                        # 库里若已有 `--baseline` 跑出来的进度行，直接插就是
                        # UniqueViolation。改用一个远早于基准周的 cycle_start 避开，
                        # 语义不受影响（装配只按 `cycle_start ≤ week_start` 取最新一轮）。
                        row.cycle_start = _TEST_CYCLE_START
                    session.add(row)
                session.flush()
        yield snapshot_id
    finally:
        with session_scope() as session:
            _purge(session, snapshot_id)


def test_load_context_from_postgres(loaded_snapshot: str) -> None:
    with session_scope() as session:
        ctx = load_context(session, snapshot_id=loaded_snapshot, week_start=WEEK_START)

    assert ctx.snapshot_id == loaded_snapshot
    assert len(ctx.persons) == 8
    assert len(ctx.aircraft) == 8
    assert len(ctx.missions) == 12
    assert len(ctx.airspaces) == 6
    assert len(ctx.runways) == 2
    # 多值属性必须聚合到位（各自独立一张从表）
    assert ctx.persons["P01"].aircraft_types == frozenset({"JL-8", "JL-9"})
    assert ctx.persons["P05"].aircraft_types == frozenset({"JL-8"})
    assert ctx.runways["RWY-2"].aircraft_types == frozenset({"JL-8"})
    assert ctx.aircraft["AC73"].aircraft_type == "JL-8"
    assert ctx.aircraft["AC73"].maintenance and ctx.aircraft["AC73"].maintenance[0].all_day
    assert ctx.persons["P03"].unavailable_dates
    assert ctx.progress_of("P04", "missionC-1") is not None


def test_checks_run_against_a_database_backed_context(loaded_snapshot: str) -> None:
    """闸门1 跑在真库装出来的 context 上 —— 手工事实与库里回读的必须等价。"""
    with session_scope() as session:
        ctx = load_context(session, snapshot_id=loaded_snapshot, week_start=WEEK_START)
    report = run_all_checks(compliant_plan(), ctx)
    assert report.all_passed, [v.detail for v in report.all_violations()]
    assert report.total_checked_items > 0


def test_context_is_scoped_to_its_snapshot(loaded_snapshot: str) -> None:
    """按快照隔离：不存在的快照装出来就是空的，不会串到别的快照。"""
    with session_scope() as session:
        empty = load_context(
            session, snapshot_id=f"{loaded_snapshot}_missing", week_start=WEEK_START
        )
    assert empty.persons == {}
    assert empty.missions == {}
