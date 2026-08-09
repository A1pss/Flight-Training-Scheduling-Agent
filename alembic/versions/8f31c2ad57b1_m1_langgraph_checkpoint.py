"""M1: LangGraph Checkpointer 建表（PostgresSaver.setup）

`PostgresSaver` 自带一套内部迁移，版本号记在它自己的 `checkpoint_migrations`
表里。**不能**用 Alembic autogenerate 去描述这几张表 —— 那等于把 LangGraph 的
schema 演进权抢过来，升级 langgraph 版本时必然打架。所以这里只调用它的
`setup()`（幂等），回滚时按依赖反序 drop。

Revision ID: 8f31c2ad57b1
Revises: 4650109da8d3
Create Date: 2026-08-07 23:20:01.029246+08:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from backend.graph.checkpointer import CHECKPOINT_TABLES, setup_checkpoint_tables

revision: str = "8f31c2ad57b1"
down_revision: str | None = "4650109da8d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    setup_checkpoint_tables()


def downgrade() -> None:
    for table in reversed(CHECKPOINT_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
