"""`commit_plan_node` 的四件事，逐件实测（v6 §7.2.4）。

归档 + 推进进度 + 结算欠账 + **写 `last_done_date` 锚点**，全在一个事务里。

本文件盯两条最要紧的：

| 断言 | 为什么要紧 |
|---|---|
| `last_done_date` 被写入 | **R19 的唯一缓解措施**。S-12 只在首次排班成立，第二周起 `gap` 必须是真值 |
| 攒满完整周期 → `COMPLETED`，**且事实表同步** | 业务方 2026-08-14 裁定（`Z-16`）。只翻 `status` 不写事实表，先修永远解锁不了 |

**不跑求解**：这里验的是归档那一步的账怎么记，方案用夹具手搭即可。会话跑完
`rollback()`，库不被污染。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.db import get_session_factory, session_scope
from backend.core.ruleset import cycle_required_for
from backend.models.entities import Mission as MissionRow
from backend.models.entities import PersonCompletedMission as PersonCompletedMissionRow
from backend.models.progress import TrainingProgress
from backend.nodes.commit_plan import advance_progress, flown_counts
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot
from tests.fixtures.graph_fixtures import plan, sortie

pytestmark = pytest.mark.integration

#: 拿一个基准数据里**未完成**的 (学员, 课目) 组合做主角。
#: 张勇(P06) 只完成了 A-1/A-2，missionB-1 对他是 NOT_STARTED（v6 §1.3.1）。
SUBJECT = ("P06", "missionB-1")


@contextmanager
def shared_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(scope="module")
def snapshot() -> str:
    with session_scope() as session:
        return ensure_baseline_snapshot(session)


def _row(session: Session, snapshot_id: str) -> TrainingProgress:
    row = session.execute(
        select(TrainingProgress).where(
            TrainingProgress.snapshot_id == snapshot_id,
            TrainingProgress.person_id == SUBJECT[0],
            TrainingProgress.mission_id == SUBJECT[1],
        )
    ).scalar_one()
    return row


def _required(session: Session, snapshot_id: str, row: TrainingProgress) -> int:
    mission = session.execute(
        select(MissionRow).where(
            MissionRow.snapshot_id == snapshot_id, MissionRow.mission_id == SUBJECT[1]
        )
    ).scalar_one()
    return cycle_required_for(row.cycle_weeks, mission.freq_days)


def _one_sortie_plan(snapshot_id: str) -> object:
    return plan(
        [
            sortie(
                "S000001",
                day=0,
                takeoff=time(8, 0),
                minutes=52,
                mission_id=SUBJECT[1],
                aircraft_id="AC10",
                airspace_id="RT2",
                crew=(("P01", "教员"), (SUBJECT[0], "学员")),
            )
        ]
    ).model_copy(update={"snapshot_id": snapshot_id})


# ─────────────────────────────────────────────────────────────────────
# flown_counts
# ─────────────────────────────────────────────────────────────────────
def test_flown_counts_counts_both_crew_members(snapshot: str) -> None:
    """教员带飞的那一趟对教员也是一次执行 —— 与约束11/12 的数法一致。"""
    counts = flown_counts(_one_sortie_plan(snapshot))  # type: ignore[arg-type]
    assert set(counts) == {("P01", SUBJECT[1]), (SUBJECT[0], SUBJECT[1])}


# ─────────────────────────────────────────────────────────────────────
# 锚点（R19）
# ─────────────────────────────────────────────────────────────────────
def test_anchor_is_written_and_takes_the_latest_flight(snapshot: str) -> None:
    with shared_session() as session:
        before = _row(session, snapshot)
        assert before.last_done_date is None, "基准快照的锚点本应为空（S-12）"

        advances = advance_progress(session, _one_sortie_plan(snapshot), snapshot_id=snapshot)  # type: ignore[arg-type]
        subject = next(a for a in advances if (a.person_id, a.mission_id) == SUBJECT)

        assert subject.last_done_date is not None
        assert subject.last_done_date == _one_sortie_plan(snapshot).sorties[0].date  # type: ignore[attr-defined]
        assert _row(session, snapshot).last_done_date == subject.last_done_date


# ─────────────────────────────────────────────────────────────────────
# `Z-16`：一门课飞完完整周期才算完成
# ─────────────────────────────────────────────────────────────────────
def test_one_flight_does_not_complete_a_mission(snapshot: str) -> None:
    """飞一次不算完成 —— 否则何超飞一次 A-2 就把 B 类课目全解锁了。"""
    with shared_session() as session:
        advances = advance_progress(session, _one_sortie_plan(snapshot), snapshot_id=snapshot)  # type: ignore[arg-type]
        subject = next(a for a in advances if (a.person_id, a.mission_id) == SUBJECT)
        assert subject.status_before == "NOT_STARTED"
        assert subject.status_after == "IN_PROGRESS"
        assert subject.newly_completed is False
        assert subject.cycle_required > 1


def test_cycle_required_matches_the_baseline_syllabus(snapshot: str) -> None:
    """基准数据代入：B~F 类 16 周 / 每 7 天 ≥1 次 → 16 次。"""
    with shared_session() as session:
        row = _row(session, snapshot)
        assert _required(session, snapshot, row) == 16


def test_completing_the_cycle_flips_status_and_writes_the_fact(snapshot: str) -> None:
    """★ 攒满完整周期 → `COMPLETED`，**并且**往 `person_completed_missions` 写一行。

    第二件事是关键：先修判定读的是事实表（v6 §6.1），只翻 `status` 会出现
    「这门课显示已完成，但它作为先修的那几门课还是解锁不了」。
    """
    with shared_session() as session:
        row = _row(session, snapshot)
        required = _required(session, snapshot, row)
        # 把它推到「只差一次」——省掉 15 周的真排班，验的是那一次的判定
        row.completed_count = required - 1
        session.flush()

        facts_before = _completed_fact_exists(session, snapshot)
        assert facts_before is False

        advances = advance_progress(session, _one_sortie_plan(snapshot), snapshot_id=snapshot)  # type: ignore[arg-type]
        subject = next(a for a in advances if (a.person_id, a.mission_id) == SUBJECT)

        assert subject.completed_count == required
        assert subject.status_after == "COMPLETED"
        assert subject.newly_completed is True
        assert _row(session, snapshot).status == "COMPLETED"
        assert _completed_fact_exists(session, snapshot) is True


def test_completion_is_never_downgraded(snapshot: str) -> None:
    """⚠️ 反过来不成立：摄取期读进来的 `COMPLETED`（`completed_count=1`）
    远小于一个完整周期，但它是**业务方直接给的事实**，不许被计次公式推翻。"""
    with shared_session() as session:
        row = _row(session, snapshot)
        row.status = "COMPLETED"
        row.completed_count = 1
        session.flush()

        advances = advance_progress(session, _one_sortie_plan(snapshot), snapshot_id=snapshot)  # type: ignore[arg-type]
        subject = next(a for a in advances if (a.person_id, a.mission_id) == SUBJECT)
        assert subject.status_after == "COMPLETED"
        assert subject.newly_completed is False  # 本来就是完成态，不算「新完成」


def test_overshooting_the_cycle_is_still_completed(snapshot: str) -> None:
    with shared_session() as session:
        row = _row(session, snapshot)
        row.completed_count = _required(session, snapshot, row) + 5
        session.flush()
        advances = advance_progress(session, _one_sortie_plan(snapshot), snapshot_id=snapshot)  # type: ignore[arg-type]
        subject = next(a for a in advances if (a.person_id, a.mission_id) == SUBJECT)
        assert subject.status_after == "COMPLETED"


def _completed_fact_exists(session: Session, snapshot_id: str) -> bool:
    return (
        session.execute(
            select(PersonCompletedMissionRow).where(
                PersonCompletedMissionRow.snapshot_id == snapshot_id,
                PersonCompletedMissionRow.person_id == SUBJECT[0],
                PersonCompletedMissionRow.mission_id == SUBJECT[1],
            )
        ).scalar_one_or_none()
        is not None
    )
