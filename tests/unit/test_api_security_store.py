"""认证、键值后端、分布式锁、幂等键的单测。

`_store_contract` 被内存后端与 Redis 后端**共用**：`tests/integration/test_api_live.py`
直接 import 它，用真 Redis 跑一遍同样的断言 —— 不存在「内存版宽松、Redis 版严格」
的缝（与 `harness/cache.py` 当初的处置同一条）。
"""

from __future__ import annotations

from datetime import date

import pytest

from backend.api.idempotency import IdempotencyStore, body_fingerprint, file_digest
from backend.api.locks import LockManager, iso_week_of, schedule_lock_key, snapshot_lock_key
from backend.api.security import AuthError, Principal, TokenTable, parse_bearer, require_role
from backend.api.store import InMemoryStore, KeyValueStore
from backend.core.errors import ErrorCode, ScheduleLockedError


# ─────────────────────────────────────────────────────────────────────
# 认证与鉴权
# ─────────────────────────────────────────────────────────────────────
def test_token_table_parses_three_field_entries() -> None:
    table = TokenTable.parse("a:P01:director, b:P02:scheduler")
    assert len(table) == 2
    assert table.resolve("a") == Principal(user_id="P01", role="director")
    assert table.resolve("b").role == "scheduler"


def test_token_table_accepts_chinese_role_labels() -> None:
    """`planner/authority.py` 的中文角色名同样认（两处不该有两套写法）。"""
    table = TokenTable.parse("t:P01:训练主任")
    assert table.resolve("t").role == "director"


def test_malformed_entry_raises_rather_than_being_skipped() -> None:
    """跳过 = 某个人的 token 悄悄失效，而他半夜才发现且日志里什么都没有。"""
    with pytest.raises(ValueError, match="token:user_id:role"):
        TokenTable.parse("a:P01")
    with pytest.raises(ValueError, match="不得为空"):
        TokenTable.parse(":P01:director")


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知角色"):
        TokenTable.parse("a:P01:superuser")


def test_empty_token_table_denies_everything() -> None:
    """没配 = 全部拒绝，**不是**全部放行。"""
    table = TokenTable.parse("")
    assert table.empty
    with pytest.raises(AuthError) as excinfo:
        table.resolve("whatever")
    assert excinfo.value.status_code == 401
    assert "API_TOKENS" in excinfo.value.message


def test_wrong_token_is_401() -> None:
    table = TokenTable.parse("a:P01:director")
    with pytest.raises(AuthError) as excinfo:
        table.resolve("b")
    assert excinfo.value.status_code == 401


@pytest.mark.parametrize(
    ("header", "expected"),
    [("Bearer abc", "abc"), ("bearer abc", "abc"), ("Bearer  abc ", "abc")],
)
def test_parse_bearer_accepts_case_and_spacing(header: str, expected: str) -> None:
    assert parse_bearer(header) == expected


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "Bearer   "])
def test_parse_bearer_rejects_everything_else(header: str | None) -> None:
    with pytest.raises(AuthError):
        parse_bearer(header)


def test_role_hierarchy() -> None:
    viewer = Principal(user_id="P03", role="viewer")
    scheduler = Principal(user_id="P02", role="scheduler")
    director = Principal(user_id="P01", role="director")
    assert not viewer.can("scheduler")
    assert scheduler.can("scheduler") and not scheduler.can("director")
    assert director.can("director") and director.can("viewer")


def test_tier_authority_matches_v6_ladder() -> None:
    """Tier 3 需训练主任（v6 §3.10 / §3.9.3 的「授权」列）。"""
    scheduler = Principal(user_id="P02", role="scheduler")
    director = Principal(user_id="P01", role="director")
    assert scheduler.can_authorize_tier(1) and scheduler.can_authorize_tier(2)
    assert not scheduler.can_authorize_tier(3)
    assert director.can_authorize_tier(3)
    assert not scheduler.can_authorize_tier(9)


def test_require_role_message_names_all_three_things() -> None:
    with pytest.raises(AuthError) as excinfo:
        require_role(Principal(user_id="P03", role="viewer"), "director", action="归档")
    message = excinfo.value.message
    assert "P03" in message and "viewer" in message and "director" in message
    assert excinfo.value.status_code == 403


# ─────────────────────────────────────────────────────────────────────
# 键值后端（内存版与 Redis 版共用这一套断言）
# ─────────────────────────────────────────────────────────────────────
def _store_contract(store: KeyValueStore, prefix: str) -> None:
    key = f"{prefix}:k1"
    assert store.get(key) is None
    assert store.ttl(key) == -2

    store.set(key, "v1", 60)
    assert store.get(key) == "v1"
    assert 0 < store.ttl(key) <= 60

    assert store.set_if_absent(key, "v2", 60) is False
    assert store.get(key) == "v1"

    assert store.delete_if_value(key, "wrong") is False
    assert store.get(key) == "v1"
    assert store.delete_if_value(key, "v1") is True
    assert store.get(key) is None

    assert store.set_if_absent(key, "v3", 60) is True
    assert key in store.scan(f"{prefix}:*")


def test_in_memory_store_contract() -> None:
    _store_contract(InMemoryStore(), "unit")


def test_in_memory_store_expires() -> None:
    now = [0.0]
    store = InMemoryStore(clock=lambda: now[0])
    store.set("k", "v", 1)
    assert store.get("k") == "v"
    now[0] = 5.0
    assert store.get("k") is None, "TTL 不生效的话锁就永远不会自动释放"
    assert store.ttl("k") == -2


# ─────────────────────────────────────────────────────────────────────
# 分布式锁（v6 §9.2）
# ─────────────────────────────────────────────────────────────────────
def test_second_holder_is_rejected_with_fts_4005() -> None:
    locks = LockManager(InMemoryStore())
    locks.acquire_schedule("default", "2026W02", holder="P01")
    with pytest.raises(ScheduleLockedError) as excinfo:
        locks.acquire_schedule("default", "2026W02", holder="P02")
    error = excinfo.value
    assert error.code is ErrorCode.SCHEDULE_LOCKED
    assert error.retryable is True
    assert error.details["holder"] == "P01"
    assert error.details["lock_key"] == schedule_lock_key("default", "2026W02")
    assert error.details["ttl_s"] > 0


def test_different_weeks_do_not_block_each_other() -> None:
    locks = LockManager(InMemoryStore())
    locks.acquire_schedule("default", "2026W02", holder="P01")
    locks.acquire_schedule("default", "2026W03", holder="P02")  # 不抛即通过


def test_snapshot_lock_is_what_covers_the_cross_week_gap() -> None:
    """`Z-24`：同快照的不同周并发会在 training_progress 上死锁，靠这把锁串行。"""
    locks = LockManager(InMemoryStore())
    locks.acquire_snapshot("snap_x", holder="job:1")
    with pytest.raises(ScheduleLockedError) as excinfo:
        locks.acquire_snapshot("snap_x", holder="job:2")
    assert excinfo.value.details["lock_key"] == snapshot_lock_key("snap_x")


def test_release_requires_matching_token() -> None:
    """不比对就会误删别人的锁 —— 那正是这把锁要防的事。"""
    store = InMemoryStore()
    locks = LockManager(store)
    handle = locks.acquire_schedule("default", "2026W02", holder="P01")
    forged = type(handle)(key=handle.key, token="forged", holder="P01", ttl_s=1)
    assert locks.release(forged) is False
    assert locks.holder_of(handle.key) == "P01"
    assert locks.release(handle) is True
    assert locks.holder_of(handle.key) is None


def test_release_of_none_is_a_noop() -> None:
    assert LockManager(InMemoryStore()).release(None) is False


@pytest.mark.parametrize(
    ("day", "expected"),
    [(date(2026, 1, 5), "2026W02"), (date(2026, 1, 11), "2026W02"), (date(2026, 1, 12), "2026W03")],
)
def test_iso_week_of(day: date, expected: str) -> None:
    assert iso_week_of(day) == expected


def test_lock_context_manager_releases() -> None:
    store = InMemoryStore()
    locks = LockManager(store)
    with locks.held(schedule_lock_key("t", "2026W02"), holder="P01", subject="2026W02"):
        assert locks.holder_of(schedule_lock_key("t", "2026W02")) == "P01"
    assert locks.holder_of(schedule_lock_key("t", "2026W02")) is None


# ─────────────────────────────────────────────────────────────────────
# 幂等键
# ─────────────────────────────────────────────────────────────────────
def test_idempotency_replays_the_stored_response() -> None:
    idem = IdempotencyStore(InMemoryStore())
    assert idem.lookup("chat", "default", "uuid-1") is None
    idem.remember("chat", "default", "uuid-1", {"job_id": "j1"})
    assert idem.lookup("chat", "default", "uuid-1") == {"job_id": "j1"}


def test_idempotency_is_scoped_per_endpoint_and_tenant() -> None:
    idem = IdempotencyStore(InMemoryStore())
    idem.remember("chat", "default", "k", {"job_id": "j1"})
    assert idem.lookup("schedule", "default", "k") is None
    assert idem.lookup("chat", "other", "k") is None


def test_body_fingerprint_ignores_key_order() -> None:
    """键序不固定的话幂等就成了随机数（铁律 9 的同一条）。"""
    assert body_fingerprint({"a": 1, "b": 2}) == body_fingerprint({"b": 2, "a": 1})
    assert body_fingerprint({"a": 1}) != body_fingerprint({"a": 2})


def test_file_digest_is_sha256_hex() -> None:
    digest = file_digest(b"hello")
    assert len(digest) == 64 and digest == file_digest(b"hello")
    assert digest != file_digest(b"hell0")
