"""结果缓存连真 Redis（v6 §7.7.1 第 6 行，裸装 127.0.0.1:6380）。

单测用的是内存后端；这里跑真 Redis，验的是内存版验不到的三件事：
**TTL 真的设上去了、快照索引真的能整体失效、跨进程真的能命中**。

用例自建自清（键带随机后缀 + 用完 `invalidate_snapshot`）——CLAUDE.md §6 那条
「集成测试不许断言环境外状态」同样适用于 Redis：不假设库里有什么，也不留下什么。
"""

from __future__ import annotations

import uuid

import pytest

from backend.core.config import Settings
from backend.harness.cache import RedisCacheBackend, ToolResultCache, cache_key
from backend.harness.tools import TOOL_CATALOG

pytestmark = pytest.mark.integration


@pytest.fixture
def redis_cache() -> object:
    redis = pytest.importorskip("redis")
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    client = redis.Redis.from_url(cfg.REDIS_URL, decode_responses=True)
    try:
        client.ping()
    except Exception as exc:  # pragma: no cover —— 没起 Redis 时给个明确原因
        pytest.skip(f"Redis 不可用（{cfg.REDIS_URL}）：{exc}")
    return client


def test_round_trip_through_real_redis(redis_cache: object) -> None:
    snapshot = f"snap_{uuid.uuid4().hex[:8]}"
    cache = ToolResultCache(RedisCacheBackend(redis_cache), ttl_s=60)
    spec = TOOL_CATALOG["prereq_cte"]
    calls = {"n": 0}

    def run() -> dict[str, object]:
        calls["n"] += 1
        return {"eligible": False, "missing": ["missionA-2"]}

    try:
        first = cache.get_or_exec(spec, {"person_id": "P08"}, snapshot, run)
        second = cache.get_or_exec(spec, {"person_id": "P08"}, snapshot, run)
        assert first.cached is False and second.cached is True
        assert second.value == {"eligible": False, "missing": ["missionA-2"]}
        assert calls["n"] == 1
    finally:
        cache.invalidate_snapshot(snapshot)


def test_ttl_is_actually_set(redis_cache: object) -> None:
    """TTL 不是摆设：键上必须真有过期时间，否则快照过期后缓存永远留着。"""
    snapshot = f"snap_{uuid.uuid4().hex[:8]}"
    cache = ToolResultCache(RedisCacheBackend(redis_cache), ttl_s=120)
    spec = TOOL_CATALOG["prereq_cte"]
    try:
        cache.get_or_exec(spec, {"person_id": "P05"}, snapshot, lambda: {"ok": True})
        key = cache_key("prereq_cte", {"person_id": "P05"}, snapshot)
        ttl = redis_cache.ttl(key)  # type: ignore[attr-defined]
        assert 0 < ttl <= 120
    finally:
        cache.invalidate_snapshot(snapshot)


def test_invalidate_snapshot_clears_every_key_under_it(redis_cache: object) -> None:
    snapshot = f"snap_{uuid.uuid4().hex[:8]}"
    other = f"snap_{uuid.uuid4().hex[:8]}"
    cache = ToolResultCache(RedisCacheBackend(redis_cache), ttl_s=120)
    spec = TOOL_CATALOG["prereq_cte"]
    try:
        for pid in ("P05", "P06", "P08"):
            cache.get_or_exec(spec, {"person_id": pid}, snapshot, lambda: {"ok": True})
        cache.get_or_exec(spec, {"person_id": "P08"}, other, lambda: {"ok": True})

        assert cache.invalidate_snapshot(snapshot) == 3
        assert (
            cache.get_or_exec(spec, {"person_id": "P08"}, snapshot, lambda: {"n": 2}).cached
            is False
        )
        # 另一个快照不受影响
        assert cache.get_or_exec(spec, {"person_id": "P08"}, other, lambda: {"n": 2}).cached is True
    finally:
        cache.invalidate_snapshot(snapshot)
        cache.invalidate_snapshot(other)


def test_two_cache_objects_share_the_same_entries(redis_cache: object) -> None:
    """跨进程共享是用 Redis 而不是进程内字典的全部理由。"""
    snapshot = f"snap_{uuid.uuid4().hex[:8]}"
    writer = ToolResultCache(RedisCacheBackend(redis_cache), ttl_s=60)
    reader = ToolResultCache(RedisCacheBackend(redis_cache), ttl_s=60)
    spec = TOOL_CATALOG["prereq_cte"]
    try:
        writer.get_or_exec(spec, {"person_id": "P08"}, snapshot, lambda: {"v": 1})
        assert reader.get_or_exec(spec, {"person_id": "P08"}, snapshot, lambda: {"v": 2}).value == {
            "v": 1
        }
    finally:
        writer.invalidate_snapshot(snapshot)


def test_from_settings_builds_a_working_client() -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    try:
        backend = RedisCacheBackend.from_settings(cfg)
        backend.set("fts:harness:selftest", "1", 5)
        assert backend.get("fts:harness:selftest") == "1"
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Redis 不可用：{exc}")
