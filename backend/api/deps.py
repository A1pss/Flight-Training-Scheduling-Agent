"""FastAPI 依赖注入。

## 单例挂在 `app.state`，不挂模块级全局

模块级全局的问题是**测试没法换**：一个进程里跑两个配置不同的 app（单测用内存
store、集成测试用真 Redis）时，模块级全局只有一份。挂 `app.state` 之后，
`create_app(store=..., runner=...)` 就是全部的注入点，没有 monkeypatch。

## 会话是「一个请求一个」

`get_session` 每次开一个新会话，随请求结束关闭（M4-B §8 第 5 条的同一条口径）。
读端点里不 commit；写端点（`/confirm`）自己 commit。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Annotated, Any, cast

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from backend.api.idempotency import IdempotencyStore
from backend.api.jobs import JobStore
from backend.api.locks import LockManager
from backend.api.runner import JobRunner
from backend.api.security import Principal, TokenTable, parse_bearer
from backend.api.store import KeyValueStore
from backend.core.config import Settings
from backend.core.db import get_session_factory


def get_settings_dep(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_store(request: Request) -> KeyValueStore:
    return cast(KeyValueStore, request.app.state.store)


def get_job_store(request: Request) -> JobStore:
    return cast(JobStore, request.app.state.jobs)


def get_locks(request: Request) -> LockManager:
    return cast(LockManager, request.app.state.locks)


def get_idempotency(request: Request) -> IdempotencyStore:
    return cast(IdempotencyStore, request.app.state.idempotency)


def get_runner(request: Request) -> JobRunner:
    return cast(JobRunner, request.app.state.runner)


def get_token_table(request: Request) -> TokenTable:
    return cast(TokenTable, request.app.state.tokens)


def get_today(request: Request) -> date:
    """「今天」由 app 决定，不在链路深处调 `date.today()`（M4-B §8 第 3 条）。

    默认是真的今天；测试把 `app.state.today` 换成基准周附近的日期即可。
    """
    return cast(date, request.app.state.today or date.today())


def get_trace_id(request: Request) -> str:
    """本次请求的 trace_id（中间件写进 `request.state`）。"""
    return cast(str, getattr(request.state, "trace_id", "") or "unknown")


def get_principal(request: Request) -> Principal:
    """认证：`Authorization: Bearer <token>` → `Principal`。"""
    table = get_token_table(request)
    principal = table.resolve(parse_bearer(request.headers.get("authorization")))
    request.state.principal = principal
    return principal


def get_session(request: Request) -> Iterator[Session]:
    """一个请求一个会话。"""
    factory = cast(Any, request.app.state.session_factory or get_session_factory())
    session: Session = factory()
    try:
        yield session
    finally:
        session.close()


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
CurrentSession = Annotated[Session, Depends(get_session)]
CurrentSettings = Annotated[Settings, Depends(get_settings_dep)]
CurrentJobs = Annotated[JobStore, Depends(get_job_store)]
CurrentLocks = Annotated[LockManager, Depends(get_locks)]
CurrentIdempotency = Annotated[IdempotencyStore, Depends(get_idempotency)]
CurrentRunner = Annotated[JobRunner, Depends(get_runner)]
CurrentTraceId = Annotated[str, Depends(get_trace_id)]
CurrentToday = Annotated[date, Depends(get_today)]

__all__ = [
    "CurrentIdempotency",
    "CurrentJobs",
    "CurrentLocks",
    "CurrentPrincipal",
    "CurrentRunner",
    "CurrentSession",
    "CurrentSettings",
    "CurrentToday",
    "CurrentTraceId",
    "get_idempotency",
    "get_job_store",
    "get_locks",
    "get_principal",
    "get_runner",
    "get_session",
    "get_settings_dep",
    "get_store",
    "get_today",
    "get_token_table",
]
