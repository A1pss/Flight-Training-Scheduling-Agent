"""任务提交：入 RQ 队列，或就地跑完（v6 §9.2）。

## 两种 runner，一个接口

| runner | 用在哪 | 行为 |
|---|---|---|
| `RQRunner` | 生产（默认） | `enqueue` 到 Redis :6380 的 `fts` 队列，立即返回 |
| `InlineRunner` | 集成测试、单机排障 | 在当前线程里把 `execute_run` 跑完再返回 |

**`InlineRunner` 不是「简化版」**：它跑的是同一个 `execute_run`，同一个图，
同一套锁与状态写入。区别只有「谁来跑」。所以集成测试用它验出来的行为
（幂等、锁、阶段推进、回放完整性）在 RQ 下同样成立——这一点由
`tests/integration/test_api_worker_live.py` 用真 RQ worker 再验一次。

## 为什么默认是 RQ 而不是 inline

一次排班几十秒（求解 21 s + 校验 + Excel），inline 会把 HTTP 请求卡在那儿，
而 v6 §9.2 的第一句就是「提交后**立即**返回 job_id」。
"""

from __future__ import annotations

from typing import Any, Protocol

from backend.api.store import KeyValueStore
from backend.api.worker import RunPayload, execute_run
from backend.core.config import Settings, get_settings


class JobRunner(Protocol):
    """提交一个任务。返回队列侧的 id（inline 时就是 job_id）。"""

    def submit(self, payload: RunPayload) -> str: ...


class InlineRunner:
    """就地执行。`last_result` 留着给测试断言。

    **`store` 必须与 app 用同一个**：inline 时 worker 与 API 在同一个进程里，
    各自 `build_store()` 会得到两份状态，于是轮询永远看不到进度推进
    —— 实测踩过，见 `worker.execute_run` 的 docstring。
    """

    def __init__(self, store: KeyValueStore | None = None) -> None:
        self.last_result: dict[str, Any] | None = None
        self._store = store

    def submit(self, payload: RunPayload) -> str:
        self.last_result = execute_run(payload.to_dict(), store=self._store)
        return payload.job_id


class RQRunner:
    """入 RQ 队列（Redis :6380 的 `fts`）。

    **队列是懒建的**：`create_app()` 在模块导入时就会跑一次（`uvicorn
    backend.api.main:app` 的入口），那时候不该去碰 Redis —— 否则任何
    `import backend.api.main` 的地方（比如单测收集阶段）都要求 Redis 在线。
    """

    def __init__(
        self, queue: Any = None, *, timeout_s: int, settings: Settings | None = None
    ) -> None:
        self._queue = queue
        self._timeout = timeout_s
        self._settings = settings

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> RQRunner:
        cfg = settings or get_settings()
        return cls(timeout_s=cfg.RQ_JOB_TIMEOUT_S, settings=cfg)

    def queue(self) -> Any:
        if self._queue is None:
            import redis
            from rq import Queue

            cfg = self._settings or get_settings()
            self._queue = Queue(cfg.RQ_QUEUE, connection=redis.Redis.from_url(cfg.REDIS_URL))
        return self._queue

    def submit(self, payload: RunPayload) -> str:
        job = self.queue().enqueue(
            # 用**字符串路径**而不是函数对象：worker 进程按路径 import，
            # 这样 API 进程与 worker 进程不必共享同一份内存里的函数
            "backend.api.worker.execute_run",
            payload.to_dict(),
            job_id=payload.job_id,
            job_timeout=self._timeout,
            result_ttl=3600,
        )
        return str(job.id)


def build_runner(settings: Settings | None = None, store: KeyValueStore | None = None) -> JobRunner:
    cfg = settings or get_settings()
    if cfg.JOB_RUNNER == "inline":
        return InlineRunner(store)
    return RQRunner.from_settings(cfg)


__all__ = ["InlineRunner", "JobRunner", "RQRunner", "build_runner"]
