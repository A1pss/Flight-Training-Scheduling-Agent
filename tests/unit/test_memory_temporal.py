"""记忆时效性与冲突消解（v6 §6.4）。

> 长期记忆最大的坑不是「召回不到」，而是「召回到过期版本」——
> 召回到过期资质会直接导致排班违规。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from backend.core.errors import DataConflictError, ErrorCode
from backend.memory.temporal import (
    DEFAULT_RETENTION_CYCLES,
    SOURCE_CONVERSATION,
    SOURCE_PG_FACT,
    SOURCE_PLAN_CONFIRMED,
    active_at,
    archive_horizon,
    detect_conflict,
    is_active_at,
    latest_version,
    rank_by_trust,
    trust_of,
)


@dataclass
class Row:
    """`Versioned` 的最小实现（`EpisodicMemory` / `ProceduralMemory` 同形）。"""

    memory_id: str
    valid_from: datetime
    valid_to: datetime | None = None
    superseded_by: str | None = None


T0 = datetime(2026, 1, 1)
T1 = datetime(2026, 1, 7)
T2 = datetime(2026, 1, 14)


# ─────────────────────────────────────────────────────────────────────
# ① 时间过滤
# ─────────────────────────────────────────────────────────────────────
def test_validity_window_is_half_open() -> None:
    """半开 `[from, to)` —— 上一版的 `to` = 下一版的 `from` 时不会两版同时有效。"""
    row = Row("m1", valid_from=T0, valid_to=T1)
    assert is_active_at(row, T0) is True
    assert is_active_at(row, T1 - timedelta(seconds=1)) is True
    assert is_active_at(row, T1) is False


def test_superseded_by_is_a_link_not_a_tombstone() -> None:
    """被取代的版本在**它自己那段时间里**依然是当时的正确答案。

    这正是「同一问题两个时点不同答案」的机制（刘斌的 C 类资质）。
    """
    row = Row("m1", valid_from=T0, valid_to=T1, superseded_by="m2")
    assert is_active_at(row, T0 + timedelta(days=1)) is True
    assert is_active_at(row, T1) is False


def test_superseded_without_an_end_date_is_treated_as_invalid() -> None:
    """说不清自己什么时候不再成立的行，一律判无效 —— 八成是只改了一半。"""
    row = Row("m1", valid_from=T0, valid_to=None, superseded_by="m2")
    assert is_active_at(row, T1) is False


def test_a_plain_date_is_accepted_as_the_moment() -> None:
    assert is_active_at(Row("m1", valid_from=T0), date(2026, 1, 5)) is True


def test_timezone_aware_rows_compare_against_naive_moments() -> None:
    """PG 的 TIMESTAMPTZ 读回来带 tzinfo，调用方常给 naive —— 混着比会抛。"""
    row = Row("m1", valid_from=datetime(2026, 1, 1, tzinfo=UTC))
    assert is_active_at(row, datetime(2026, 1, 5)) is True


def test_active_at_filters_a_batch() -> None:
    rows = [Row("a", T0, T1), Row("b", T1, None), Row("c", T0, None, superseded_by="b")]
    assert [r.memory_id for r in active_at(rows, T2)] == ["b"]
    # 回到 a 的区间里，a 才是当时的答案
    assert [r.memory_id for r in active_at(rows, T0)] == ["a"]


# ─────────────────────────────────────────────────────────────────────
# ② 同 key 多版本
# ─────────────────────────────────────────────────────────────────────
def test_latest_version_returns_the_newest_and_counts_the_history() -> None:
    """§6.4：最新有效版本 + **显式标注历史版本数量**。"""
    rows = [Row("v1", T0, T1, superseded_by="v2"), Row("v2", T1, None)]
    view = latest_version(rows, T2)
    assert view.current is not None and view.current.memory_id == "v2"
    assert view.history_count == 1
    assert view.has_history
    assert "1 个历史版本" in view.note()


def test_no_history_means_no_note() -> None:
    assert latest_version([Row("v1", T0)], T2).note() == ""


def test_all_versions_expired_yields_no_current_but_still_counts() -> None:
    rows = [Row("v1", T0, T1), Row("v2", T1, T2)]
    view = latest_version(rows, T2 + timedelta(days=1))
    assert view.current is None
    assert view.history_count == 2


def test_ties_break_on_memory_id_so_the_result_is_reproducible() -> None:
    """铁律 9：任何未固定的顺序都是 bug。"""
    rows = [Row("b", T0), Row("a", T0)]
    assert latest_version(rows, T2).current.memory_id == "b"  # type: ignore[union-attr]


# ─────────────────────────────────────────────────────────────────────
# ③ 写入冲突
# ─────────────────────────────────────────────────────────────────────
def test_trust_order_is_pg_fact_then_plan_then_conversation() -> None:
    assert trust_of(SOURCE_PG_FACT) > trust_of(SOURCE_PLAN_CONFIRMED)
    assert trust_of(SOURCE_PLAN_CONFIRMED) > trust_of(SOURCE_CONVERSATION)


def test_unknown_source_gets_the_lowest_trust_not_the_highest() -> None:
    """一个拼错的来源名不该获得覆盖 PG 事实的权力。"""
    assert trust_of("我瞎写的") < trust_of(SOURCE_CONVERSATION)


def test_rank_by_trust_is_descending_and_stable() -> None:
    assert rank_by_trust([SOURCE_CONVERSATION, SOURCE_PG_FACT, SOURCE_PLAN_CONFIRMED]) == [
        SOURCE_PG_FACT,
        SOURCE_PLAN_CONFIRMED,
        SOURCE_CONVERSATION,
    ]


def test_same_value_is_not_a_conflict() -> None:
    assert (
        detect_conflict(
            key="k",
            existing_id="m1",
            existing_source=SOURCE_CONVERSATION,
            existing_value={"tier": 1},
            incoming_source=SOURCE_PG_FACT,
            incoming_value={"tier": 1},
        )
        is None
    )


def test_dict_key_order_is_not_a_conflict() -> None:
    assert (
        detect_conflict(
            key="k",
            existing_id="m1",
            existing_source=SOURCE_PG_FACT,
            existing_value={"a": 1, "b": 2},
            incoming_source=SOURCE_PG_FACT,
            incoming_value={"b": 2, "a": 1},
        )
        is None
    )


def test_higher_trust_supersedes() -> None:
    conflict = detect_conflict(
        key="qual/P04/C",
        existing_id="m1",
        existing_source=SOURCE_CONVERSATION,
        existing_value="2026-02-07",
        incoming_source=SOURCE_PG_FACT,
        incoming_value="2026-01-07",
    )
    assert conflict is not None and conflict.resolution == "supersede"
    assert conflict.needs_human is False


def test_lower_trust_is_rejected_but_recorded() -> None:
    """拒绝不等于静默丢弃 —— 冲突要留痕。"""
    conflict = detect_conflict(
        key="qual/P04/C",
        existing_id="m1",
        existing_source=SOURCE_PG_FACT,
        existing_value="2026-01-07",
        incoming_source=SOURCE_CONVERSATION,
        incoming_value="2026-02-07",
    )
    assert conflict is not None and conflict.resolution == "reject"
    assert "2026-01-07" in conflict.describe() and "2026-02-07" in conflict.describe()


def test_same_tier_escalates_to_a_human() -> None:
    """两条同样可信、内容互斥 —— 系统没有裁决依据，挑一个就是猜。"""
    conflict = detect_conflict(
        key="relaxation/preferred_tier",
        existing_id="m1",
        existing_source=SOURCE_PLAN_CONFIRMED,
        existing_value={"tier": 1},
        incoming_source=SOURCE_PLAN_CONFIRMED,
        incoming_value={"tier": 2},
    )
    assert conflict is not None and conflict.resolution == "escalate"
    assert conflict.needs_human
    error = conflict.as_error()
    assert isinstance(error, DataConflictError)
    assert error.code is ErrorCode.DATA_INTEGRITY_OR_CONFLICT


def test_as_error_refuses_to_run_on_a_resolvable_conflict() -> None:
    conflict = detect_conflict(
        key="k",
        existing_id="m1",
        existing_source=SOURCE_CONVERSATION,
        existing_value="a",
        incoming_source=SOURCE_PG_FACT,
        incoming_value="b",
    )
    assert conflict is not None
    with pytest.raises(ValueError, match="不需要升级人工"):
        conflict.as_error()


# ─────────────────────────────────────────────────────────────────────
# ④ 遗忘策略
# ─────────────────────────────────────────────────────────────────────
def test_retention_default_is_three_training_cycles() -> None:
    assert DEFAULT_RETENTION_CYCLES == 3


def test_archive_horizon_is_now_minus_cycles_times_cycle_weeks() -> None:
    horizon = archive_horizon(datetime(2026, 6, 1), cycle_weeks=20, cycles=3)
    assert horizon == datetime(2026, 6, 1) - timedelta(weeks=60)


@pytest.mark.parametrize(("weeks", "cycles"), [(0, 3), (-1, 3), (20, 0), (20, -1)])
def test_non_positive_parameters_raise_instead_of_archiving_everything(
    weeks: int, cycles: int
) -> None:
    with pytest.raises(ValueError):
        archive_horizon(datetime(2026, 6, 1), cycle_weeks=weeks, cycles=cycles)
