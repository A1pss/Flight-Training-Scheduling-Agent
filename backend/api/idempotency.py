"""幂等键（v6 §9.1 幂等键那一列）。

## 三种幂等键，一套机制

| 端点 | 键 | 为什么是它 |
|---|---|---|
| `POST /ingest` | **文件 SHA256** | 同一份文件重传就该是同一次摄取——文件内容是它唯一的身份 |
| `POST /chat` | **客户端 UUID** | 同一句话可能真要说两遍（「再排一次」），所以身份得由客户端给 |
| `POST /schedule` `/confirm` `/approve` `/reject` | 客户端 UUID，缺省时按**请求体规范化哈希** | 程序调用方未必生成 UUID；退化路径保证「同样的请求体」至少不会排两次班 |

## 存的是「结果」，不是「见过」

只记「这个键见过」的话，重复提交只能回一个 409——而客户端重试的原因往往是
**上一次的响应没收到**，它要的正是那个响应体。所以这里存的是**完整响应 JSON**，
重复提交原样回放，`idempotent_hit=True` 让调用方知道这是回放。

⚠️ **写入必须在处理成功之后**。先占位再处理的话，一次失败的提交会把键占死，
用户重试拿到的是那次失败的空壳。这里的顺序是：`lookup` → 处理 → `remember`。
代价是两个并发的同键请求都会真跑一次（各自拿到自己的结果，且后者覆盖前者的
记录）—— 排班那条路上这件事由**分布式锁**兜住，摄取那条路上重复解析同一份
文件是幂等的（只读，不落库）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

from backend.api.store import KEY_PREFIX, KeyValueStore

IDEMPOTENCY_PREFIX: Final[str] = f"{KEY_PREFIX}:idem"
#: 幂等记录保留 24 小时。客户端重试窗口以秒~分钟计，24 小时是两个数量级的余量。
IDEMPOTENCY_TTL_S: Final[int] = 24 * 3600


def body_fingerprint(payload: Any) -> str:
    """请求体的规范化指纹（键排序 + 紧凑分隔符）。

    键序不固定的话，同一个请求在两次序列化下会得到两个指纹，幂等就成了随机数
    ——与 `harness/cache.py::cache_key` 同一条理由（铁律 9）。
    """
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def file_digest(data: bytes) -> str:
    """文件 SHA256 —— `POST /ingest` 的幂等键本身。"""
    return hashlib.sha256(data).hexdigest()


class IdempotencyStore:
    """`(scope, key) → 响应 JSON` 的记录。"""

    def __init__(self, store: KeyValueStore, *, ttl_s: int = IDEMPOTENCY_TTL_S) -> None:
        self._store = store
        self._ttl = ttl_s

    @staticmethod
    def key(scope: str, tenant_id: str, token: str) -> str:
        return f"{IDEMPOTENCY_PREFIX}:{scope}:{tenant_id}:{token}"

    def lookup(self, scope: str, tenant_id: str, token: str) -> dict[str, Any] | None:
        raw = self._store.get(self.key(scope, tenant_id, token))
        if not raw:
            return None
        loaded = json.loads(raw)
        return dict(loaded) if isinstance(loaded, dict) else None

    def remember(
        self, scope: str, tenant_id: str, token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._store.set(
            self.key(scope, tenant_id, token),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
            self._ttl,
        )
        return payload


__all__ = [
    "IDEMPOTENCY_PREFIX",
    "IDEMPOTENCY_TTL_S",
    "IdempotencyStore",
    "body_fingerprint",
    "file_digest",
]
