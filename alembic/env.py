"""Alembic 运行环境。

**数据库 URL 不写在 alembic.ini**，而是从 :class:`backend.core.config.Settings`
读——保证迁移和应用用的是同一份 `.env`，不会出现「应用连 5433、迁移连 5432」
这种典型事故。

M0 只交付可跑通的骨架；建表迁移由 M1 窗口产出，落在 `alembic/versions/`。
`target_metadata` 指向 `backend.models` 的 `Base.metadata`，M1 一旦定义 ORM，
`alembic revision --autogenerate` 立刻可用。
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from backend.core.config import get_settings
from backend.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().DATABASE_URL)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库（离线交付包的 sql/ 目录用它产出）。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
