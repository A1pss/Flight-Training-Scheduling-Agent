"""分布式锁（v6 §9.2）。

## 两把锁，各管一件事

| 锁 | 键 | 防的是 |
|---|---|---|
| 排班锁 | `fts:api:lock:schedule:{tenant}:{iso_week}` | 两人同时排同一周，产出两份都「合规」但互相矛盾的计划 |
| 快照锁 | `fts:api:lock:snapshot:{snapshot_id}` | `materialize_progress` 在 `training_progress` 上死锁（M5 §9.1 第 7 条，有实测证据） |

**第二把是 M6 补的**（`Z-24`）。M5 收工报告记了这样一段 PG 输出：

```
psycopg.errors.DeadlockDetected: deadlock detected
CONTEXT: while deleting tuple (6,51) in relation "training_progress"
[SQL: DELETE FROM training_progress WHERE person_id=%s AND mission_id=%s AND cycle_start=%s]
```

根因是 `training_progress` 是物化视图、主键**不含 `snapshot_id`**（v6 §6.3.2），
`compile_spec` 每次排班按主键 DELETE 再 INSERT。两个请求排**同一快照的不同周**
时，`(tenant, week)` 锁互不冲突，两边照样以不同顺序删同一批行 → 死锁。

业务方 2026-08-18 选定的处置是「worker 内再取一把 snapshot 级锁」，
**不动 `compile_spec` 这个确定性节点的事务边界**。代价写在明处：同一快照的
排班全局串行。这在业务上可接受——排一周班是分钟级的重操作，不是并发热点。

## 拿不到锁就拒绝，不排队

排队的问题是它**看起来成功了**：用户拿到 job_id、进度条转着，实际上在等一个
可能十分钟都不结束的求解。`FTS-4005` 明确告诉他「谁在排、还剩多久」，
让他自己决定等还是改时间（v6 §9.3 `Z-24`）。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

from backend.api.store import KEY_PREFIX, KeyValueStore
from backend.core.errors import ScheduleLockedError
from backend.routing.entities import iso_week_of

SCHEDULE_LOCK_PREFIX: Final[str] = f"{KEY_PREFIX}:lock:schedule"
SNAPSHOT_LOCK_PREFIX: Final[str] = f"{KEY_PREFIX}:lock:snapshot"

#: 锁的默认存活时间。取值理由：一次基准周求解 ~21 s（v6 §3.11 / CLAUDE.md §4），
#: 常规档预算 60 s，诊断档 300 s，再加报表与归档——1800 s 给足两个数量级的余量。
#: **锁必须会过期**：进程被 kill 时没人来释放，不过期就等于那一周从此排不了。
DEFAULT_LOCK_TTL_S: Final[int] = 1800


def schedule_lock_key(tenant_id: str, iso_week: str) -> str:
    return f"{SCHEDULE_LOCK_PREFIX}:{tenant_id}:{iso_week}"


def snapshot_lock_key(snapshot_id: str) -> str:
    return f"{SNAPSHOT_LOCK_PREFIX}:{snapshot_id}"


@dataclass(frozen=True)
class LockHandle:
    """一把已持有的锁。`token` 是释放时的凭据（不比对就会误删别人的锁）。"""

    key: str
    token: str
    holder: str
    ttl_s: int


class LockManager:
    """两把锁共用一套获取/释放逻辑。"""

    def __init__(self, store: KeyValueStore, *, ttl_s: int = DEFAULT_LOCK_TTL_S) -> None:
        self._store = store
        self._ttl = ttl_s

    def acquire(self, key: str, *, holder: str, subject: str) -> LockHandle:
        """拿锁。拿不到抛 :class:`ScheduleLockedError`（FTS-4005）。

        `subject` 只用于错误文案（「2026W02」/「snap_xxx」），不进键。
        """
        token = uuid.uuid4().hex
        payload = json.dumps({"holder": holder, "token": token}, ensure_ascii=False)
        if self._store.set_if_absent(key, payload, self._ttl):
            return LockHandle(key=key, token=token, holder=holder, ttl_s=self._ttl)

        current = self._store.get(key)
        other = "未知"
        if current:
            try:
                other = str(json.loads(current).get("holder", "未知"))
            except (ValueError, AttributeError):
                other = "未知"
        remaining = self._store.ttl(key)
        raise ScheduleLockedError(
            f"{subject} 正在被 {other} 排班，本次提交已拒绝",
            details={
                "lock_key": key,
                "holder": other,
                "ttl_s": max(0, remaining),
                "subject": subject,
            },
            suggestions=[
                f"等 {other} 跑完再提交（剩余约 {max(0, remaining)} 秒）",
                "或换一个排班周提交",
            ],
        )

    def release(self, handle: LockHandle | None) -> bool:
        """释放。**值不匹配就不删**——锁已易主时删掉的会是别人的。"""
        if handle is None:
            return False
        payload = json.dumps({"holder": handle.holder, "token": handle.token}, ensure_ascii=False)
        return self._store.delete_if_value(handle.key, payload)

    def acquire_schedule(self, tenant_id: str, iso_week: str, *, holder: str) -> LockHandle:
        return self.acquire(schedule_lock_key(tenant_id, iso_week), holder=holder, subject=iso_week)

    def acquire_snapshot(self, snapshot_id: str, *, holder: str) -> LockHandle:
        return self.acquire(snapshot_lock_key(snapshot_id), holder=holder, subject=snapshot_id)

    @contextmanager
    def held(self, key: str, *, holder: str, subject: str) -> Iterator[LockHandle]:
        handle = self.acquire(key, holder=holder, subject=subject)
        try:
            yield handle
        finally:
            self.release(handle)

    def holder_of(self, key: str) -> str | None:
        """当前持有者（没锁返回 None）。供 `/jobs` 与前端提示用。"""
        current = self._store.get(key)
        if not current:
            return None
        try:
            return str(json.loads(current).get("holder"))
        except (ValueError, AttributeError):
            return None


__all__ = [
    "DEFAULT_LOCK_TTL_S",
    "SCHEDULE_LOCK_PREFIX",
    "SNAPSHOT_LOCK_PREFIX",
    "LockHandle",
    "LockManager",
    "iso_week_of",
    "schedule_lock_key",
    "snapshot_lock_key",
]
