"""API 层的键值后端：任务状态 / 幂等键 / 分布式锁共用一套。

## 为什么是一个后端而不是三个

三者的存储语义完全一样（带 TTL 的字符串 + 一个「不存在才写」的原子操作），
分成三套只会得到三份「内存版能过、Redis 版不行」的缝。`backend/harness/cache.py`
已经用同一手法处理过工具结果缓存——那里的注释写得明白：两者共用同一套键计算
与失效逻辑，不存在两版行为不一致的可能。

## 为什么内存版仍然存在

单测里不该起 Redis（CLAUDE.md §11 反模式的同族）。`InMemoryStore` 让
`tests/unit/test_api_*.py` 能直接跑；真 Redis 的行为由
`tests/integration/test_api_live.py` 验一遍。**两者跑的是同一套测试断言**
（`tests/unit/test_api_store.py::_store_contract` 被两边共用），所以不存在
「内存版宽松、Redis 版严格」的情况。

## TTL 不是可选项

任务状态、幂等记录、锁，三者都必须过期：
- 锁不过期 → 一个进程崩了，那一周从此再也排不了（v6 §9.2 的锁是**可用性**
  设施，不是正确性设施——正确性由 `content_sha256` 与校验器保证）；
- 幂等记录不过期 → Redis 无限膨胀；
- 任务状态不过期 → 同上，而且陈旧任务会一直出现在轮询里。
"""

from __future__ import annotations

import fnmatch
import time
from typing import Any, Final, Protocol

from backend.core.config import Settings, get_settings

#: 全部键的命名空间前缀。与 `fts:harness:*`（工具结果缓存）平级、互不覆盖。
KEY_PREFIX: Final[str] = "fts:api"


class KeyValueStore(Protocol):
    """API 层要的最小键值接口。"""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_s: int) -> None: ...

    def set_if_absent(self, key: str, value: str, ttl_s: int) -> bool:
        """不存在才写。返回 True 表示本次写成功（= 拿到了锁 / 是第一次提交）。"""
        ...

    def delete_if_value(self, key: str, value: str) -> bool:
        """值匹配才删。返回 True 表示删掉了。

        **锁的释放必须带值比对**：不比对的话，A 的锁超时被 B 抢到之后，
        A 跑完会把 B 的锁删掉——两个人于是同时排同一周，而这正是这把锁要防的事。
        """
        ...

    def ttl(self, key: str) -> int:
        """剩余秒数。键不存在返回 -2，无过期返回 -1（沿用 Redis 语义）。"""
        ...

    def scan(self, pattern: str) -> list[str]:
        """按 glob 取键。只用于列任务，不在热路径上。"""
        ...


class InMemoryStore:
    """进程内后端（单测用）。**实现过期**——不实现的话 TTL 相关断言全是假的。"""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}
        self._clock = clock

    def _live(self, key: str) -> tuple[str, float | None] | None:
        item = self._data.get(key)
        if item is None:
            return None
        _, expires = item
        if expires is not None and self._clock() >= expires:
            self._data.pop(key, None)
            return None
        return item

    def get(self, key: str) -> str | None:
        item = self._live(key)
        return None if item is None else item[0]

    def set(self, key: str, value: str, ttl_s: int) -> None:
        self._data[key] = (value, self._clock() + ttl_s if ttl_s > 0 else None)

    def set_if_absent(self, key: str, value: str, ttl_s: int) -> bool:
        if self._live(key) is not None:
            return False
        self.set(key, value, ttl_s)
        return True

    def delete_if_value(self, key: str, value: str) -> bool:
        item = self._live(key)
        if item is None or item[0] != value:
            return False
        self._data.pop(key, None)
        return True

    def ttl(self, key: str) -> int:
        item = self._live(key)
        if item is None:
            return -2
        expires = item[1]
        if expires is None:
            return -1
        return max(0, int(expires - self._clock()))

    def scan(self, pattern: str) -> list[str]:
        return sorted(k for k in list(self._data) if self._live(k) and fnmatch.fnmatch(k, pattern))


#: 「值匹配才删」的原子实现。放 Lua 里是因为 GET+DEL 两步之间锁可能已经易主。
_RELEASE_SCRIPT: Final[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class RedisStore:
    """Redis 7 后端（裸装 127.0.0.1:6380，v6 §11.1）。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_settings(cls, cfg: Settings | None = None) -> RedisStore:
        import redis

        settings = cfg or get_settings()
        return cls(redis.Redis.from_url(settings.REDIS_URL, decode_responses=True))

    def get(self, key: str) -> str | None:
        value = self._client.get(key)
        return str(value) if value is not None else None

    def set(self, key: str, value: str, ttl_s: int) -> None:
        # `set(..., ex=)` 而非 `setex`：后者在 redis-py ≥2.6.12 已标废弃，
        # 而本仓库把 DeprecationWarning 当错误（pyproject filterwarnings）
        self._client.set(key, value, ex=ttl_s)

    def set_if_absent(self, key: str, value: str, ttl_s: int) -> bool:
        return bool(self._client.set(key, value, ex=ttl_s, nx=True))

    def delete_if_value(self, key: str, value: str) -> bool:
        return bool(int(self._client.eval(_RELEASE_SCRIPT, 1, key, value)))

    def ttl(self, key: str) -> int:
        return int(self._client.ttl(key))

    def scan(self, pattern: str) -> list[str]:
        return sorted(str(k) for k in self._client.scan_iter(match=pattern, count=200))


def build_store(settings: Settings | None = None) -> KeyValueStore:
    """按配置造后端。**连不上 Redis 时抛，不静默退回内存版。**

    静默退回是这里最危险的写法：内存版的锁在多 worker 下形同虚设，而
    「排班时两个人同时排同一周」不会报错，只会产出两份互相矛盾的计划。
    """
    return RedisStore.from_settings(settings)


__all__ = ["KEY_PREFIX", "InMemoryStore", "KeyValueStore", "RedisStore", "build_store"]
