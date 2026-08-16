"""三类长期记忆的真库实测（v6 §6.2 / §6.4）。

覆盖出口标准的三条：

- **时间过滤有效**：构造 superseded 的资质记录，确认默认召回返回新版本并标注
  历史版本数；
- **MemoryConflict 检测实测**：写入与现存记忆矛盾的条目，确认按可信度排序或
  升级人工；
- **遗忘策略**：情景记忆超 3 个训练周期归档到冷表，可检索但不参与默认召回。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from backend.core.db import session_scope
from backend.core.errors import DataConflictError
from backend.memory.episodic import (
    EpisodeRecord,
    archive_stale_episodes,
    record_episode,
    record_hitl_decision,
    retention_cycle_weeks,
    search_episodes,
)
from backend.memory.procedural import (
    NAMESPACE_RELAXATION,
    distill,
    get_preference,
    list_preferences,
    preference_with_history,
    put_preference,
)
from backend.memory.semantic import (
    aircraft_fact,
    completed_missions,
    mission_fact,
    person_fact,
    qualification_facts,
)
from backend.memory.temporal import (
    SOURCE_CONVERSATION,
    SOURCE_PG_FACT,
    SOURCE_PLAN_CONFIRMED,
)
from backend.models.memory import EpisodicMemory, ProceduralMemory
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot

pytestmark = pytest.mark.integration

NOW = datetime(2026, 1, 5, 9, 0)
SESSION_ID = "m5-memory-live"


@pytest.fixture(scope="module")
def snapshot() -> str:
    with session_scope() as session:
        snapshot_id = ensure_baseline_snapshot(session)
        session.commit()
    return snapshot_id


@pytest.fixture
def session(snapshot: str) -> Session:
    """每个用例一个干净的记忆表 —— 本文件写记忆，用例之间不许互相看见。"""
    with session_scope() as s:
        s.execute(delete(ProceduralMemory))
        s.execute(delete(EpisodicMemory).where(EpisodicMemory.session_id.startswith("m5-")))
        s.flush()
        yield s
        s.rollback()


# ─────────────────────────────────────────────────────────────────────
# 语义记忆：精确查询，不走向量
# ─────────────────────────────────────────────────────────────────────
def test_semantic_memory_answers_the_four_probe_facts_exactly(
    session: Session, snapshot: str
) -> None:
    """M1~M4 的事实来源。**一条 SQL，不存在近音混淆。**"""
    liu = qualification_facts(session, snapshot, "P04", mission_class="C")
    assert [q.expiry_date.isoformat() for q in liu if q.expiry_date] == ["2026-01-07"]

    ac73 = aircraft_fact(session, snapshot, "AC73")
    assert ac73 is not None and ac73.aircraft_type == "JL-8"

    a1 = mission_fact(session, snapshot, "missionA-1")
    assert a1 is not None and a1.dual_required is False

    assert "missionA-2" not in completed_missions(session, snapshot, "P08")


def test_the_two_chao_are_two_rows(session: Session, snapshot: str) -> None:
    he = person_fact(session, snapshot, "P08")
    gao = person_fact(session, snapshot, "P02")
    assert he is not None and gao is not None
    assert he.name == "何超" and he.identity == "学员"
    assert gao.name == "高超" and gao.identity == "教员"


def test_missing_entity_returns_none_not_an_empty_shell(session: Session, snapshot: str) -> None:
    assert person_fact(session, snapshot, "P999") is None
    assert aircraft_fact(session, snapshot, "AC999") is None


# ─────────────────────────────────────────────────────────────────────
# 情景记忆
# ─────────────────────────────────────────────────────────────────────
def test_hitl_decisions_are_recorded_with_the_conflict_and_the_tier(session: Session) -> None:
    """§6.2 的写入时机：**每次 HITL 决策后**。"""
    row = record_hitl_decision(
        session,
        session_id=SESSION_ID,
        decision="APPROVE",
        user_id="alps",
        role="训练主任",
        comment="就这样",
        occurred_at=NOW,
        plan_id="2026W02-abc123",
        relaxation_tier=1,
        conflicts=["C13:P08:missionB-1"],
    )
    assert row.kind == "approval"
    assert row.content["relaxation_tier"] == 1
    assert row.content["conflicts"] == ["C13:P08:missionB-1"]
    assert "Tier 1" in row.summary


@pytest.mark.parametrize(
    ("decision", "kind"),
    [("APPROVE", "approval"), ("REVISE", "user_revision"), ("REJECT", "user_rejection")],
)
def test_each_decision_maps_to_its_own_episodic_kind(
    session: Session, decision: str, kind: str
) -> None:
    row = record_hitl_decision(
        session,
        session_id=SESSION_ID,
        decision=decision,
        user_id="alps",
        role="scheduler",
        comment="",
        occurred_at=NOW,
    )
    assert row.kind == kind


def test_recording_the_same_episode_twice_is_idempotent(session: Session) -> None:
    """重放同一条轨迹不该让「用户驳回过几次」随重放次数增长。"""
    record = EpisodeRecord(
        session_id=SESSION_ID,
        kind="user_rejection",
        summary="驳回",
        content={"reason": "周三太挤"},
        occurred_at=NOW,
    )
    first = record_episode(session, record)
    second = record_episode(session, record)
    assert first.memory_id == second.memory_id
    assert len(search_episodes(session, at=NOW, session_id=SESSION_ID)) == 1


def test_unknown_episodic_kind_is_rejected(session: Session) -> None:
    with pytest.raises(ValueError, match="未登记"):
        record_episode(
            session,
            EpisodeRecord(
                session_id=SESSION_ID, kind="随便编的", summary="x", content={}, occurred_at=NOW
            ),
        )


# ─────────────────────────────────────────────────────────────────────
# §6.4 遗忘策略
# ─────────────────────────────────────────────────────────────────────
def test_retention_period_is_the_longest_cycle_in_the_snapshot(
    session: Session, snapshot: str
) -> None:
    """保守选择：宁可晚归档，不早归档（G/H 类 20 周是基准数据里最长的）。"""
    assert retention_cycle_weeks(session, snapshot) == 20


def test_episodes_older_than_three_cycles_are_archived_but_still_retrievable(
    session: Session,
) -> None:
    """§6.4：归档到冷表，**仍可检索但不参与默认召回**。"""
    old = NOW - timedelta(weeks=61)  # 3 × 20 周之前
    record_episode(
        session,
        EpisodeRecord(
            session_id=SESSION_ID, kind="approval", summary="很久以前", content={}, occurred_at=old
        ),
    )
    record_episode(
        session,
        EpisodeRecord(
            session_id=SESSION_ID, kind="approval", summary="最近", content={}, occurred_at=NOW
        ),
    )

    archived = archive_stale_episodes(session, now=NOW, cycle_weeks=20, cycles=3)
    assert archived == 1

    default = search_episodes(session, at=NOW, session_id=SESSION_ID)
    assert [e.summary for e in default] == ["最近"], "归档条目不参与默认召回"

    everything = search_episodes(session, at=NOW, session_id=SESSION_ID, include_archived=True)
    assert {e.summary for e in everything} == {"最近", "很久以前"}, "但仍然检索得到"


# ─────────────────────────────────────────────────────────────────────
# §6.4 时效性：同 key 多版本
# ─────────────────────────────────────────────────────────────────────
def test_a_superseded_version_is_replaced_and_the_history_is_counted(session: Session) -> None:
    """出口标准：默认召回返回**新版本**并标注**历史版本数**。"""
    put_preference(
        session,
        namespace=NAMESPACE_RELAXATION,
        key="preferred_tier",
        value={"tier": 1},
        source=SOURCE_CONVERSATION,
        at=NOW,
    )
    later = NOW + timedelta(days=7)
    outcome = put_preference(
        session,
        namespace=NAMESPACE_RELAXATION,
        key="preferred_tier",
        value={"tier": 2},
        source=SOURCE_PLAN_CONFIRMED,  # 更高可信度 → 覆盖
        at=later,
    )
    assert outcome.written

    current, history = preference_with_history(
        session, namespace=NAMESPACE_RELAXATION, key="preferred_tier", at=later
    )
    assert current is not None and current.value["tier"] == 2, "默认召回返回最新有效版本"
    assert history == 1, "并显式标注历史版本数量"

    # 旧版本两件事一起改：valid_to 与 superseded_by
    rows = list(session.scalars(session.query(ProceduralMemory).statement))
    old = next(r for r in rows if r.value["tier"] == 1)
    # PG 的 TIMESTAMPTZ 读回来带 tzinfo，写进去的是 naive —— 比较前统一
    assert old.valid_to is not None and old.valid_to.replace(tzinfo=None) == later
    assert old.superseded_by == current.memory_id


def test_the_old_version_is_still_the_answer_before_the_supersede_date(session: Session) -> None:
    """时间过滤按 `as_of` 走 —— 这正是刘斌资质那对样本的机制。"""
    put_preference(
        session,
        namespace=NAMESPACE_RELAXATION,
        key="preferred_tier",
        value={"tier": 1},
        source=SOURCE_CONVERSATION,
        at=NOW,
    )
    later = NOW + timedelta(days=7)
    put_preference(
        session,
        namespace=NAMESPACE_RELAXATION,
        key="preferred_tier",
        value={"tier": 2},
        source=SOURCE_PLAN_CONFIRMED,
        at=later,
    )
    before = get_preference(
        session, namespace=NAMESPACE_RELAXATION, key="preferred_tier", at=NOW + timedelta(days=1)
    )
    assert before is not None and before.value["tier"] == 1


# ─────────────────────────────────────────────────────────────────────
# §6.4 写入冲突
# ─────────────────────────────────────────────────────────────────────
def test_a_lower_trust_write_is_rejected_and_recorded(session: Session) -> None:
    """对话推断改不动 PG 事实 —— 但拒绝要留痕，不是静默丢弃。"""
    put_preference(
        session,
        namespace="qualification",
        key="P04/C/expiry",
        value={"date": "2026-01-07"},
        source=SOURCE_PG_FACT,
        at=NOW,
    )
    outcome = put_preference(
        session,
        namespace="qualification",
        key="P04/C/expiry",
        value={"date": "2026-02-07"},  # ← §5.5 X1 那个笔误
        source=SOURCE_CONVERSATION,
        at=NOW + timedelta(days=1),
    )
    assert outcome.written is False
    assert outcome.conflict is not None and outcome.conflict.resolution == "reject"
    assert outcome.needs_human is False

    current = get_preference(
        session, namespace="qualification", key="P04/C/expiry", at=NOW + timedelta(days=2)
    )
    assert current is not None and current.value["date"] == "2026-01-07", "PG 事实纹丝不动"


def test_a_same_trust_contradiction_escalates_to_a_human(session: Session) -> None:
    """两条同样可信、内容互斥 —— 系统没有裁决依据（FTS-2001）。"""
    put_preference(
        session,
        namespace=NAMESPACE_RELAXATION,
        key="preferred_tier",
        value={"tier": 1},
        source=SOURCE_PLAN_CONFIRMED,
        at=NOW,
    )
    outcome = put_preference(
        session,
        namespace=NAMESPACE_RELAXATION,
        key="preferred_tier",
        value={"tier": 3},
        source=SOURCE_PLAN_CONFIRMED,
        at=NOW + timedelta(days=1),
    )
    assert outcome.written is False
    assert outcome.needs_human is True
    assert isinstance(outcome.conflict.as_error(), DataConflictError)  # type: ignore[union-attr]


def test_writing_the_same_value_twice_creates_no_new_version(session: Session) -> None:
    put_preference(
        session,
        namespace=NAMESPACE_RELAXATION,
        key="preferred_tier",
        value={"tier": 1},
        source=SOURCE_CONVERSATION,
        at=NOW,
    )
    outcome = put_preference(
        session,
        namespace=NAMESPACE_RELAXATION,
        key="preferred_tier",
        value={"tier": 1},
        source=SOURCE_CONVERSATION,
        at=NOW + timedelta(days=1),
    )
    assert outcome.written
    _, history = preference_with_history(
        session, namespace=NAMESPACE_RELAXATION, key="preferred_tier", at=NOW + timedelta(days=2)
    )
    assert history == 0, "值没变就不该长出一个版本"


# ─────────────────────────────────────────────────────────────────────
# 程序记忆：从情景记忆蒸馏
# ─────────────────────────────────────────────────────────────────────
def test_distillation_needs_at_least_two_occurrences(session: Session) -> None:
    """**一次不算习惯。** 把单次行为固化成偏好，下次就会拿它影响排班。"""
    record_hitl_decision(
        session,
        session_id=SESSION_ID,
        decision="APPROVE",
        user_id="alps",
        role="训练主任",
        comment="",
        occurred_at=NOW,
        relaxation_tier=2,
    )
    report = distill(session, at=NOW + timedelta(days=1))
    assert report.written == ()
    assert any("支持度 1" in s for s in report.skipped)


def test_a_repeated_choice_becomes_a_preference(session: Session) -> None:
    for day in (1, 8):
        record_hitl_decision(
            session,
            session_id=SESSION_ID,
            decision="APPROVE",
            user_id="alps",
            role="训练主任",
            comment="",
            occurred_at=NOW + timedelta(days=day),
            plan_id=f"plan-{day}",
            relaxation_tier=2,
        )
    report = distill(session, at=NOW + timedelta(days=30))
    assert f"{NAMESPACE_RELAXATION}/preferred_tier" in report.written

    pref = get_preference(
        session,
        namespace=NAMESPACE_RELAXATION,
        key="preferred_tier",
        at=NOW + timedelta(days=30),
    )
    assert pref is not None
    assert pref.value == {"tier": 2, "support": 2}
    assert pref.source == SOURCE_PLAN_CONFIRMED, "蒸馏出来的偏好压不过 PG 事实"


def test_distilled_preferences_are_listed_by_namespace_prefix(session: Session) -> None:
    for day in (1, 8):
        record_hitl_decision(
            session,
            session_id=SESSION_ID,
            decision="APPROVE",
            user_id="alps",
            role="训练主任",
            comment="",
            occurred_at=NOW + timedelta(days=day),
            plan_id=f"plan-{day}",
            relaxation_tier=1,
        )
    distill(session, at=NOW + timedelta(days=30))
    rows = list_preferences(session, prefix=NAMESPACE_RELAXATION, at=NOW + timedelta(days=30))
    assert [r.key for r in rows] == ["preferred_tier"]


def test_distillation_is_idempotent(session: Session) -> None:
    """跑两遍不该长出第二个版本（值没变 → 不是冲突，也不是新版本）。"""
    for day in (1, 8):
        record_hitl_decision(
            session,
            session_id=SESSION_ID,
            decision="APPROVE",
            user_id="alps",
            role="训练主任",
            comment="",
            occurred_at=NOW + timedelta(days=day),
            plan_id=f"plan-{day}",
            relaxation_tier=2,
        )
    distill(session, at=NOW + timedelta(days=30))
    distill(session, at=NOW + timedelta(days=31))
    _, history = preference_with_history(
        session,
        namespace=NAMESPACE_RELAXATION,
        key="preferred_tier",
        at=NOW + timedelta(days=32),
    )
    assert history == 0
