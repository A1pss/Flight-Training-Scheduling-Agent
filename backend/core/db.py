"""数据库引擎与会话工厂。

URL 一律来自 :class:`backend.core.config.Settings`，不在别处硬编码 —— 应用、
迁移、测试连的必须是同一个实例（M0 已经踩过「应用连 5433、迁移连 5432」这类
事故的坑，这里从源头堵掉）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """进程内单例引擎。测试中换库请用 ``get_engine.cache_clear()``。"""
    settings = get_settings()
    return create_engine(
        settings.DATABASE_URL,
        future=True,
        pool_pre_ping=True,
        # 摄取是批量写入，关掉自动 flush 避免中途部分可见
        echo=False,
    )


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务边界。异常时回滚 —— 摄取宁可整批失败也不半批入库（铁律 7）。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_cache() -> None:
    """清掉引擎与会话工厂缓存（改 `.env` 或换库后调用）。"""
    get_engine.cache_clear()
    get_session_factory.cache_clear()


__all__ = ["get_engine", "get_session_factory", "reset_engine_cache", "session_scope"]
