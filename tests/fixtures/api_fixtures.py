"""M6 的测试替身：假 runner、假会话工厂、一份可注入的 app。

**替身只替「谁来跑」与「连哪个库」，不替业务逻辑**——集成测试跑的是同一个
`execute_run`、同一张图（见 `backend/api/runner.py` 的模块注释）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

from fastapi import FastAPI

from backend.api.main import create_app
from backend.api.runner import InlineRunner
from backend.api.store import InMemoryStore, KeyValueStore
from backend.api.worker import RunPayload
from backend.core.config import Settings

#: 三个角色各一个 token，覆盖 §9.1 的最低角色矩阵。
TEST_TOKENS = "tok-dir:P01:director,tok-sch:P02:scheduler,tok-view:P03:viewer"
DIRECTOR = {"Authorization": "Bearer tok-dir"}
SCHEDULER = {"Authorization": "Bearer tok-sch"}
VIEWER = {"Authorization": "Bearer tok-view"}

BASELINE_WEEK = date(2026, 1, 5)
BASELINE_TODAY = date(2026, 1, 2)


class RecordingRunner:
    """只记不跑。用于验提交侧的行为（幂等、锁、鉴权），不牵扯求解。"""

    def __init__(self) -> None:
        self.payloads: list[RunPayload] = []

    def submit(self, payload: RunPayload) -> str:
        self.payloads.append(payload)
        return payload.job_id


class FailingRunner:
    """入队就炸。用于验「入队失败要把锁放掉」。"""

    def submit(self, payload: RunPayload) -> str:
        raise RuntimeError("队列不可用")


class NullSession:
    """什么都不做的会话替身（单测里不碰库）。"""

    def close(self) -> None:
        return None


def null_session_factory() -> Any:
    return NullSession()


@contextmanager
def shared_session(session: Any) -> Iterator[Any]:
    yield session


def make_settings(**overrides: Any) -> Settings:
    """一份不读 `.env` 的配置，带测试 token 与 inline runner。"""
    base: dict[str, Any] = {
        "API_TOKENS": TEST_TOKENS,
        "JOB_RUNNER": "inline",
        "LLM_PROVIDER": "mock",
        "APP_ENV": "ci",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def build_test_app(
    *,
    settings: Settings | None = None,
    store: KeyValueStore | None = None,
    runner: Any = None,
    session_factory: Any = None,
    today: date = BASELINE_TODAY,
) -> tuple[FastAPI, KeyValueStore]:
    """建一个可注入的 app，返回 `(app, store)`。

    **store 要回传**：inline runner 必须与 app 共用同一个 store，否则轮询看到的
    永远是 `QUEUED`（`worker.execute_run` 的 docstring 记着这次实测）。
    """
    cfg = settings or make_settings()
    backend_store = store or InMemoryStore()
    app = create_app(
        settings=cfg,
        store=backend_store,
        runner=runner if runner is not None else InlineRunner(backend_store),
        session_factory=session_factory or null_session_factory,
        today=today,
    )
    return app, backend_store


class ProgressRestore:
    """归档前的库状态快照，用于**精确还原**。

    `approve` 会真的推进训练进度、写 `last_done_date` 锚点、往 `plans` 落一行。
    这些写入会改变后续测试看到的基准状态 —— `last_done_date` 一旦有值，
    S-12 的「从本周周一起算」就不成立了，基准周的频率窗口跟着变。

    **不能靠 `rollback()`**：worker 用的是它自己的会话并且 `commit()` 了
    （那正是 v6 §9.2 说的「worker 之间不共享事务」）。
    """

    def __init__(self, snapshot_id: str) -> None:
        from sqlalchemy import select

        from backend.core.db import session_scope
        from backend.models.entities import PersonCompletedMission
        from backend.models.planning import Plan
        from backend.models.progress import TrainingProgress

        self.snapshot_id = snapshot_id
        with session_scope() as session:
            self.progress = {
                (r.person_id, r.mission_id, r.cycle_start): (
                    r.status,
                    r.completed_count,
                    r.last_done_date,
                    r.debt_count,
                )
                for r in session.execute(
                    select(TrainingProgress).where(TrainingProgress.snapshot_id == snapshot_id)
                ).scalars()
            }
            self.completed = {
                (r.person_id, r.mission_id)
                for r in session.execute(
                    select(PersonCompletedMission).where(
                        PersonCompletedMission.snapshot_id == snapshot_id
                    )
                ).scalars()
            }
            self.plans = {row.plan_id for row in session.execute(select(Plan)).scalars()}

    def restore(self) -> None:
        """进度四个字段按值还原、新增的已完成事实与计划行删掉。"""
        from sqlalchemy import delete, select

        from backend.core.db import session_scope
        from backend.models.entities import PersonCompletedMission
        from backend.models.planning import Plan
        from backend.models.progress import TrainingProgress

        with session_scope() as session:
            for row in session.execute(
                select(TrainingProgress).where(TrainingProgress.snapshot_id == self.snapshot_id)
            ).scalars():
                key = (row.person_id, row.mission_id, row.cycle_start)
                if key in self.progress:
                    (
                        row.status,
                        row.completed_count,
                        row.last_done_date,
                        row.debt_count,
                    ) = self.progress[key]
            for fact in session.execute(
                select(PersonCompletedMission).where(
                    PersonCompletedMission.snapshot_id == self.snapshot_id
                )
            ).scalars():
                if (fact.person_id, fact.mission_id) not in self.completed:
                    session.delete(fact)
            new_ids = {row.plan_id for row in session.execute(select(Plan)).scalars()} - self.plans
            if new_ids:
                session.execute(delete(Plan).where(Plan.plan_id.in_(new_ids)))


@contextmanager
def restored_db(snapshot_id: str) -> Iterator[ProgressRestore]:
    """归档类测试的护栏：跑完把库还原到测试之前。"""
    state = ProgressRestore(snapshot_id)
    try:
        yield state
    finally:
        state.restore()


__all__ = [
    "BASELINE_TODAY",
    "BASELINE_WEEK",
    "DIRECTOR",
    "SCHEDULER",
    "TEST_TOKENS",
    "VIEWER",
    "FailingRunner",
    "NullSession",
    "ProgressRestore",
    "RecordingRunner",
    "build_test_app",
    "make_settings",
    "null_session_factory",
    "restored_db",
    "shared_session",
]
