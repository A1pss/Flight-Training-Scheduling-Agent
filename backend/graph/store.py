"""`FTSStore`：图的跨线程长期记忆（v6 §7.5 的 `store=FTSStore(...)`、§6.2）。

Checkpointer 与 Store 管的**不是同一件事**，混起来会踩坑：

| | Checkpointer | Store |
|---|---|---|
| 作用域 | 单个 `thread_id`（一次对话） | 跨线程、跨周 |
| 存什么 | 图的**状态快照**，供中断恢复 | **长期记忆**：进度、情节、程序性 |
| 生命周期 | 随线程走完即可归档 | 长期保留，跨排班周引用 |

## 三类记忆的命名空间（v6 §6.2）

```
(tenant, "progress")   训练进度类：欠账、锚点变化的摘要
(tenant, "episodic")   情节类：某次排班为什么这么排、用户说过什么
(tenant, "procedural") 程序性：反复出现的偏好（「周五尽量别排刘斌」）
```

**`memory.advance_progress` 不属于任何 LLM 组件**（v6 §7.7.2 注）：训练进度的
推进发生在人工确认之后，是 `commit_plan_node` 在事务中做的事，走的是 PG 的
`training_progress` 表而不是这里。Store 里的 `progress` 命名空间放的是**摘要**，
供检索与解释引用，**不是真源**。

## 为什么 Postgres 后端要能退回内存后端

CI 上有 PG，但单元测试不该为了一个命名空间常量去连库。`build_store()` 因此
分两态：给了 DSN 就用 `PostgresStore`，没给就用 `InMemoryStore`。
**两者的命名空间约定完全一致**——测试里验的是同一套键。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final, Literal

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from langgraph.store.postgres import PostgresStore

from backend.core.config import Settings, get_settings

MemoryKind = Literal["progress", "episodic", "procedural"]

#: 三类长期记忆（v6 §6.2）。**顺序即文档顺序**，便于逐条核对。
MEMORY_KINDS: Final[tuple[MemoryKind, ...]] = ("progress", "episodic", "procedural")


def namespace(tenant_id: str, kind: MemoryKind) -> tuple[str, str]:
    """记忆命名空间。**租户在前**——多租户下先按租户隔离，再谈类别。"""
    if kind not in MEMORY_KINDS:
        raise ValueError(f"未知记忆类别 {kind!r}，合法取值：{MEMORY_KINDS}")
    return (tenant_id, kind)


def store_dsn(settings: Settings | None = None) -> str:
    """`PostgresStore` 要的是 psycopg 原生 DSN，不是 SQLAlchemy URL。"""
    s = settings or get_settings()
    return f"postgresql://{s.PG_USER}:{s.PG_PASSWORD}@{s.PG_HOST}:{s.PG_PORT}/{s.PG_DATABASE}"


@contextmanager
def postgres_store(dsn: str | None = None) -> Iterator[BaseStore]:
    """带建表的 `PostgresStore` 上下文。

    `setup()` 是幂等的，且与 Alembic 无关——LangGraph 自己管这几张表的迁移
    （和 Checkpointer 一样，见 `backend/graph/checkpointer.py`）。
    """
    with PostgresStore.from_conn_string(dsn or store_dsn()) as store:
        store.setup()
        yield store


def build_store(*, dsn: str | None = None, in_memory: bool = False) -> BaseStore:
    """建一个 Store。`in_memory=True` 给单测用。

    **注意**：`PostgresStore` 需要显式关闭连接，所以生产路径请用
    :func:`postgres_store` 上下文管理器；本函数返回的 Postgres 实例由调用方负责
    生命周期。做成两个入口而不是一个，是因为 LangGraph 的 `compile(store=...)`
    要的是实例而不是上下文。
    """
    if in_memory:
        return InMemoryStore()
    store = PostgresStore.from_conn_string(dsn or store_dsn()).__enter__()
    store.setup()
    return store


def remember(
    store: BaseStore,
    *,
    tenant_id: str,
    kind: MemoryKind,
    key: str,
    value: dict[str, object],
) -> None:
    """写一条长期记忆。"""
    store.put(namespace(tenant_id, kind), key, value)


def recall(
    store: BaseStore,
    *,
    tenant_id: str,
    kind: MemoryKind,
    key: str,
) -> dict[str, object] | None:
    """读一条长期记忆。不存在返回 None。"""
    item = store.get(namespace(tenant_id, kind), key)
    return dict(item.value) if item is not None else None


__all__ = [
    "MEMORY_KINDS",
    "MemoryKind",
    "build_store",
    "namespace",
    "postgres_store",
    "recall",
    "remember",
    "store_dsn",
]
