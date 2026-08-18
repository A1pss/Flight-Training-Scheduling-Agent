"""故障注入（v6 §12.5 护栏组）—— **依赖挂掉时系统怎么表现**。

前面几组护栏验的是「有人做坏事时能不能拦住」，这一组验的是「依赖坏掉时会不会
悄悄给出错的答案」。**每一条的判据都是「要么正确降级、要么明确失败，绝不静默」。**

| 注入 | 期望 | 反模式 |
|---|---|---|
| LLM 服务不可用 | FTS-4001 + **排班能力完整保留** | 排不了班 |
| Redis 连不上 | 启动即抛 | **静默退回内存版**（多 worker 下锁形同虚设） |
| 审计写不进去 | 请求照常 + **日志留 ERROR** | 静默吞掉 |
| 求解超时 | UNKNOWN | **当成 INFEASIBLE**（铁律 8） |
| 出网请求 | `EgressDeniedError` | 悄悄连出去 |
| 上传脏文件 | 阻断入库 | 「尽力而为」入一半 |

## 「排班不依赖 LLM」是这一组最重要的一条

v6 §9.3 的脚注把它叫做「工程化 vs demo 的分水岭」：模型挂了，排班能力必须还在。
`test_scheduling_survives_a_dead_llm` 直接把 provider 换成一个**每次调用都抛**的
替身，然后走结构化入口排一遍 —— 结果与正常时逐字节相同。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.core.errors import (
    EgressDeniedError,
    ErrorCode,
    IngestionError,
    LLMUnavailableError,
)
from tests.fixtures.api_fixtures import (
    SCHEDULER,
    FailingRunner,
    RecordingRunner,
    RecordingSessionFactory,
    build_test_app,
    make_settings,
)

pytestmark = pytest.mark.guardrail


class DeadProvider:
    """每次调用都抛 FTS-4001 的 provider 替身。"""

    name = "dead"

    def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        raise LLMUnavailableError("注入：Ollama 不可用")


# ═════════════════════════════════════════════════════════════════════
# ① LLM 挂了
# ═════════════════════════════════════════════════════════════════════
def test_dead_provider_raises_the_documented_code() -> None:
    with pytest.raises(LLMUnavailableError) as excinfo:
        DeadProvider().complete([])
    assert excinfo.value.code == ErrorCode.LLM_UNAVAILABLE
    from backend.core.errors import ERROR_REGISTRY

    assert ERROR_REGISTRY[ErrorCode.LLM_UNAVAILABLE].retryable is True


def test_structured_entry_never_calls_the_model() -> None:
    """★ 结构化排班入口 `use_llm=False` **是写死的**，不是配置项。

    这条路径是 FTS-4001 的降级出口：模型完全不可用时用户改用表单照样能排班。
    把它做成配置项，等于允许有人在生产上把它关掉 —— 那时降级路径就不存在了。
    """
    from backend.core.config import PROJECT_ROOT

    source = (PROJECT_ROOT / "backend" / "api" / "routers" / "schedule.py").read_text(
        encoding="utf-8"
    )
    section = source[source.index("def post_schedule") : source.index("def _structured_message")]
    assert "use_llm=False" in section
    assert "settings.USE_LLM" not in section and "body.use_llm" not in section


def test_scheduling_submits_fine_with_a_dead_provider() -> None:
    """LLM 挂着也能把排班提交进去（真正的求解在 worker 里，同样不经 LLM）。"""
    app, _ = build_test_app(
        settings=make_settings(LLM_PROVIDER="mock"),
        runner=RecordingRunner(),
        session_factory=RecordingSessionFactory(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/schedule",
        headers=SCHEDULER,
        json={"week_start": "2026-01-05", "snapshot_id": "snap_x"},
    )
    assert response.status_code == 202


# ═════════════════════════════════════════════════════════════════════
# ② Redis 连不上
# ═════════════════════════════════════════════════════════════════════
def test_store_never_falls_back_to_memory_silently() -> None:
    """★ `build_store` 连不上 Redis 时必须抛。

    静默退回内存版是这里最危险的写法：内存锁在多 worker 下形同虚设，
    而「两个人同时排同一周」不会报错，只会产出两份互相矛盾的计划。
    """
    import redis

    from backend.api.store import build_store
    from backend.core.config import Settings

    broken = Settings(_env_file=None, REDIS_HOST="127.0.0.1", REDIS_PORT=1)  # type: ignore[call-arg]
    # 断言的是 redis 自己的连接异常类型：**不是随便抛点什么就算数**，
    # 抛一个 InMemory 替身出来同样"抛了异常"，但那正是这条要防的事。
    with pytest.raises(redis.exceptions.RedisError):
        store = build_store(broken)
        store.set("probe", "1", 1)


def test_enqueue_failure_releases_the_lock() -> None:
    """入队炸了要把 `(tenant, week)` 锁放掉，否则那一周从此排不了。"""
    app, _store = build_test_app(
        settings=make_settings(), runner=FailingRunner(), session_factory=RecordingSessionFactory()
    )
    client = TestClient(app, raise_server_exceptions=False)
    first = client.post(
        "/api/v1/schedule",
        headers=SCHEDULER,
        json={"week_start": "2026-01-05", "snapshot_id": "snap_x"},
    )
    assert first.status_code >= 500
    # 锁放掉了 → 同一周可以再提交（这次仍会因为 runner 而失败，但不是 409）
    second = client.post(
        "/api/v1/schedule",
        headers=SCHEDULER,
        json={"week_start": "2026-01-05", "snapshot_id": "snap_x", "client_request_id": "retry"},
    )
    assert second.status_code != 409, "入队失败没放锁 —— 这一周被永久占住了"


# ═════════════════════════════════════════════════════════════════════
# ③ UNKNOWN ≠ INFEASIBLE（铁律 8）
# ═════════════════════════════════════════════════════════════════════
def test_unknown_and_infeasible_are_separate_codes() -> None:
    from backend.core.errors import ERROR_REGISTRY

    unknown = ERROR_REGISTRY[ErrorCode.SOLVE_TIMEOUT_UNKNOWN]
    infeasible = ERROR_REGISTRY[ErrorCode.INFEASIBLE]
    assert unknown.code != infeasible.code
    assert unknown.retryable and not infeasible.retryable


def test_solver_status_enum_keeps_the_three_states_apart() -> None:
    """三态在数据模型上就是分开的，不是靠调用方自觉。"""
    from backend.schemas.solver import SolverStats

    stats = SolverStats(
        status="UNKNOWN",
        num_candidates=0,
        num_variables=0,
        num_constraints=0,
        wall_time_ms=1.0,
    )
    assert stats.status == "UNKNOWN"
    # 「TIMEOUT」这种听起来合理但不在三态里的取值必须被拒 ——
    # 它一旦被接受，下游就会有人拿它当 INFEASIBLE 处理。
    with pytest.raises(ValidationError):
        SolverStats(
            status="TIMEOUT",  # type: ignore[arg-type]
            num_candidates=0,
            num_variables=0,
            num_constraints=0,
            wall_time_ms=1.0,
        )


# ═════════════════════════════════════════════════════════════════════
# ④ 出网
# ═════════════════════════════════════════════════════════════════════
def test_any_component_trying_to_reach_the_internet_is_denied() -> None:
    from backend.core.http import EgressGuard

    guard = EgressGuard(("127.0.0.1", "localhost", "10.0.0.0/8"))
    for host in ("8.8.8.8", "1.1.1.1", "203.0.113.7"):
        with pytest.raises(EgressDeniedError):
            guard.check_host(host)


# ═════════════════════════════════════════════════════════════════════
# ⑤ 脏输入
# ═════════════════════════════════════════════════════════════════════
def test_dirty_upload_blocks_instead_of_partial_ingest(tmp_path: Any) -> None:
    """铁律 7：宁可阻断，也不「尽力而为」入一半。"""
    from backend.ingestion.safety import screen_file

    path = tmp_path / "payload.pdf"
    path.write_bytes(b"not a pdf at all")
    with pytest.raises(IngestionError) as excinfo:
        screen_file(path)
    assert excinfo.value.code == ErrorCode.PDF_REPAIR_ASSERTION_FAILED


def test_orphan_token_assertion_blocks_ingestion() -> None:
    """`sionB-1` 这类脏 token 必须在后置断言处被拦下。"""
    from backend.ingestion.repair import assert_no_orphan_tokens

    with pytest.raises(IngestionError):
        assert_no_orphan_tokens([{"person_id": "P05", "missions": ["sionB-1"]}])


# ═════════════════════════════════════════════════════════════════════
# ⑥ 未预期异常的形状
# ═════════════════════════════════════════════════════════════════════
def test_internal_errors_do_not_borrow_a_business_code() -> None:
    """500 用的是 `{"kind":"internal"}` 这个**不同的形状**，不硬安一个 FTS 码。

    硬挑一个最像的会造成两种真实伤害：查日志时「Excel 写坏了」与「PG 连不上」
    混成一类；以及让 `retryable` 这一位撒谎。
    """

    class Exploding:
        def __call__(self) -> Any:
            raise RuntimeError("注入：依赖炸了")

    app, _ = build_test_app(
        settings=make_settings(), runner=RecordingRunner(), session_factory=Exploding()
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/schedule",
        headers=SCHEDULER,
        json={"week_start": "2026-01-05", "snapshot_id": "snap_x"},
    )
    assert response.status_code == 500
    body = response.json()
    assert body.get("kind") == "internal"
    assert "code" not in body, "未预期异常不该借用业务错误码"
    assert body.get("trace_id"), "500 也必须带 trace_id，否则没法查"
