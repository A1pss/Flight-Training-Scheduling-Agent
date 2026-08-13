"""确定性工具的结果缓存（v6 §7.7.1 第 6 行）。

> 确定性工具（同 `snapshot_id` + 同参数）结果缓存到 Redis，**TTL 绑定 snapshot
> 生命周期**。

三个设计点：

1. **键里必须有 `snapshot_id`。** 「何超的先修达标情况」这个答案只在某个快照下
   成立；漏掉快照维度，换一份数据后拿到的是上一版的答案 —— 而这类错误不会
   报错，只会悄悄排错班。
2. **只缓存 `deterministic=True` 的工具。** `ask_user` / `probe_solve` /
   `memory.write` 都不缓存：前者每次都要真问人，后两者本来就不满足「同参数同
   结果」。
3. **TTL 绑定快照生命周期**，不是拍一个「反正 1 小时够了」的数：每个快照维护
   一个键集合，快照失效时 `invalidate_snapshot()` 一把清干净；`CACHE_TTL_S`
   只是兜底上限，防止没人来清时无限堆积。

后端抽象成 `CacheBackend` 是为了让单测不必起 Redis：`InMemoryCacheBackend`
在单测里跑，`RedisCacheBackend` 由 `tests/integration` 连真 Redis 验一遍。
两者共用同一套键计算与失效逻辑，不存在「内存版能过、Redis 版不行」的缝。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, Final, Protocol

from backend.core.config import Settings, get_settings
from backend.harness.types import ToolResult, ToolSpec

#: Redis 键前缀。带 `fts:` 命名空间，避免与 rq 队列、排班锁撞名。
KEY_PREFIX: Final[str] = "fts:harness:tool"
#: 快照 → 其下全部缓存键的集合，供整体失效。
SNAPSHOT_INDEX_PREFIX: Final[str] = "fts:harness:snapshot"


def cache_key(tool: str, arguments: dict[str, Any], snapshot_id: str) -> str:
    """`(工具, 参数, 快照)` 的确定性键。

    参数按键排序后序列化——字典序不固定的话，同一次调用在两个进程里会算出两个
    键，缓存命中率变成随机数（铁律 9 的同一条要求）。
    """
    payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{KEY_PREFIX}:{snapshot_id or 'nosnap'}:{tool}:{digest}"


class CacheBackend(Protocol):
    """缓存后端的最小接口。"""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_s: int) -> None: ...

    def add_to_index(self, index_key: str, member: str, ttl_s: int) -> None: ...

    def drop_index(self, index_key: str) -> int:
        """删掉索引下的全部键与索引本身，返回删除的键数。"""
        ...


class InMemoryCacheBackend:
    """进程内后端（单测用）。不做过期淘汰——单测里没有「等 TTL」这回事。"""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._index: dict[str, set[str]] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str, ttl_s: int) -> None:  # noqa: ARG002 —— 内存版不过期
        self._data[key] = value

    def add_to_index(self, index_key: str, member: str, ttl_s: int) -> None:  # noqa: ARG002
        self._index.setdefault(index_key, set()).add(member)

    def drop_index(self, index_key: str) -> int:
        members = self._index.pop(index_key, set())
        removed = 0
        for member in members:
            if self._data.pop(member, None) is not None:
                removed += 1
        return removed


class RedisCacheBackend:
    """Redis 7 后端（v6 §11.1，裸装 127.0.0.1:6380）。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, cfg: Settings | None = None) -> RedisCacheBackend:
        import redis

        settings = cfg or get_settings()
        return cls(redis.Redis.from_url(settings.REDIS_URL, decode_responses=True))

    def get(self, key: str) -> str | None:
        value = self._client.get(key)
        return str(value) if value is not None else None

    def set(self, key: str, value: str, ttl_s: int) -> None:
        # 用 `set(..., ex=)` 而不是 `setex`：redis-py ≥2.6.12 把后者标了废弃，
        # 而本仓库把 `DeprecationWarning` 当错误（pyproject 的 filterwarnings）。
        self._client.set(key, value, ex=ttl_s)

    def add_to_index(self, index_key: str, member: str, ttl_s: int) -> None:
        self._client.sadd(index_key, member)
        # 索引本身也要过期，否则快照删了索引还在，堆成一堆孤儿集合。
        # 比条目多留一截，保证「条目还在、索引没了」不会发生。
        self._client.expire(index_key, ttl_s * 2)

    def drop_index(self, index_key: str) -> int:
        members = [str(m) for m in self._client.smembers(index_key)]
        removed = 0
        if members:
            removed = int(self._client.delete(*members))
        self._client.delete(index_key)
        return removed


class ToolResultCache:
    """工具结果缓存的门面。"""

    def __init__(
        self,
        backend: CacheBackend | None = None,
        *,
        ttl_s: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self._backend: CacheBackend = backend or InMemoryCacheBackend()
        self._ttl = ttl_s if ttl_s is not None else cfg.HARNESS_CACHE_TTL_S
        self.hits = 0
        self.misses = 0

    @property
    def ttl_s(self) -> int:
        return self._ttl

    def get_or_exec(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        snapshot_id: str,
        execute: Callable[[], Any],
    ) -> ToolResult:
        """确定性工具查缓存，其余直接执行。"""
        if not spec.deterministic:
            return ToolResult(tool=spec.name, ok=True, value=execute(), cached=False)

        key = cache_key(spec.name, arguments, snapshot_id)
        cached = self._backend.get(key)
        if cached is not None:
            self.hits += 1
            return ToolResult(tool=spec.name, ok=True, value=json.loads(cached), cached=True)

        self.misses += 1
        value = execute()
        self._backend.set(key, json.dumps(value, ensure_ascii=False, sort_keys=True), self._ttl)
        self._backend.add_to_index(self._index_key(snapshot_id), key, self._ttl)
        return ToolResult(tool=spec.name, ok=True, value=value, cached=False)

    def invalidate_snapshot(self, snapshot_id: str) -> int:
        """快照失效时清掉它名下的全部缓存，返回清掉的条目数。"""
        return self._backend.drop_index(self._index_key(snapshot_id))

    @staticmethod
    def _index_key(snapshot_id: str) -> str:
        return f"{SNAPSHOT_INDEX_PREFIX}:{snapshot_id or 'nosnap'}:keys"


__all__ = [
    "KEY_PREFIX",
    "SNAPSHOT_INDEX_PREFIX",
    "CacheBackend",
    "InMemoryCacheBackend",
    "RedisCacheBackend",
    "ToolResultCache",
    "cache_key",
]
