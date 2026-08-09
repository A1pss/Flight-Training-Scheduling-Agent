"""LangGraph Checkpointer 的建表入口。

LangGraph 的 `PostgresSaver` 自带一套内部迁移（`checkpoint_migrations` 表记录
版本号），**不能**用 Alembic autogenerate 去描述它 —— 版本推进由 LangGraph 自己
负责。所以这里只提供一个幂等的 `setup` 包装，由独立的 Alembic 迁移调用；
回滚时按 :data:`CHECKPOINT_TABLES` 反序 drop。

M1 只建表。真正把 Checkpointer 挂进图是 M4b（`feat/m4b-orchestration`）的事。
"""

from __future__ import annotations

from typing import Final

from langgraph.checkpoint.postgres import PostgresSaver

from backend.core.config import get_settings

#: `PostgresSaver.setup()` 创建的全部表，**按依赖顺序**排列（drop 时反序）。
CHECKPOINT_TABLES: Final[tuple[str, ...]] = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)


def checkpoint_dsn() -> str:
    """`PostgresSaver` 要的是 psycopg 原生 DSN，不是 SQLAlchemy URL。"""
    s = get_settings()
    return f"postgresql://{s.PG_USER}:{s.PG_PASSWORD}@{s.PG_HOST}:{s.PG_PORT}/{s.PG_DATABASE}"


def setup_checkpoint_tables(dsn: str | None = None) -> tuple[str, ...]:
    """幂等地建好 LangGraph checkpoint 表，返回涉及的表名。"""
    with PostgresSaver.from_conn_string(dsn or checkpoint_dsn()) as saver:
        saver.setup()
    return CHECKPOINT_TABLES


__all__ = ["CHECKPOINT_TABLES", "checkpoint_dsn", "setup_checkpoint_tables"]
