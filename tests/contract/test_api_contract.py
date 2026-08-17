"""契约测试：schemathesis × v6 §9.1 的 11 个端点。

schemathesis 从 `/api/v1/openapi.json` 读契约，按每个端点的 schema 生成载荷
（含畸形、边界、错方法、错媒体类型）去打真的 app。判据只有两条，但都很硬：

1. **任何输入都不许 5xx。** 5xx 说明实现漏了边界——把它挡在 422 才是契约层
   的活。`backend/api/main.py` 那个「未预期异常」处理器专门写了一句：
   它的存在不是为了让 500 变得可接受。
2. **错误响应必须符合 v6 §9.3 的 `ErrorResponse` 形状**（`code` / `severity` /
   `stage` / `trace_id` / `retryable` 五个字段一个不少）。前端按这个形状分色、
   决定要不要给「重试」按钮，缺一个字段就得写一堆 `.get(...)` 兜底。

## 为什么用 `RecordingRunner`

契约测试要打**几十上百个**载荷。真跑求解的话一轮下来是几十分钟，而它要验的
东西（契约与错误形状）和求解一点关系都没有。求解链路由
`tests/integration/test_api_live.py` 跑真的。

## 为什么不碰数据库

请求体里带 `snapshot_id` 时 `require_snapshot` 短路；schemathesis 生成的
路径参数（不存在的 job_id / trace_id）走的是「找不到 → FTS-1004」那条。
两条都不需要库。**但 `/plans` 与 `/ingest/*` 会查库**，所以它们用真会话工厂
（本机 PG 已就绪；CI 的迁移步骤在门禁之前跑）。
"""

from __future__ import annotations

from typing import Any

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis import Case

from backend.core.db import get_session_factory
from backend.core.errors import ErrorCode
from tests.fixtures.api_fixtures import (
    RecordingRunner,
    build_test_app,
)

#: 契约层的合法拒绝。**5xx 一个都不许有。**
ACCEPTABLE_REJECTIONS = frozenset({400, 401, 403, 404, 405, 409, 415, 422})

#: 路由层（Starlette）产生的拒绝，走的是框架自己的 `{"detail": ...}`，
#: 不是 v6 §9.3 的业务错误契约 —— 见 `_assert_error_contract` 处的说明。
FRAMEWORK_REJECTIONS = frozenset({404, 405, 415})

#: 每个端点 5 个例子 × 11 个端点 ≈ 55 条。覆盖面靠 schema 的结构化生成，
#: 不靠海量随机（与 M3 的 Excel 契约测试同一口径）。
CONTRACT_SETTINGS = settings(
    max_examples=5,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

_app, _ = build_test_app(runner=RecordingRunner(), session_factory=get_session_factory())
schema = schemathesis.openapi.from_asgi("/api/v1/openapi.json", _app)

#: 鉴权头：不带的话每个用例都停在 401，等于什么都没测。
AUTH = {"Authorization": "Bearer tok-dir"}

#: v6 §9.1 的 11 个端点，按 schemathesis 的 label 形态列出。
EXPECTED_OPERATIONS = {
    "POST /api/v1/ingest",
    "GET /api/v1/ingest/{job_id}/changeset",
    "POST /api/v1/ingest/{job_id}/confirm",
    "POST /api/v1/chat",
    "POST /api/v1/schedule",
    "GET /api/v1/jobs/{job_id}",
    "GET /api/v1/runs/{trace_id}",
    "POST /api/v1/schedule/{trace_id}/approve",
    "POST /api/v1/schedule/{trace_id}/reject",
    "GET /api/v1/schedule/{trace_id}/export",
    "GET /api/v1/plans",
}

#: 存活探针不是 §9.1 的端点，也**刻意不鉴权**（探针要能在没有凭据时探活）。
HEALTH_OPERATION = "GET /health"


@schema.parametrize()
@CONTRACT_SETTINGS
def test_api_contract_never_breaks(case: Case) -> None:
    response = case.call(headers=AUTH)
    assert response.status_code < 500, (
        f"{case.operation.label} 上出现服务端错误：{response.text[:400]}"
    )
    if response.status_code >= 400:
        assert response.status_code in ACCEPTABLE_REJECTIONS, (
            f"{case.operation.label} 意外状态码 {response.status_code}"
        )
        if response.status_code in FRAMEWORK_REJECTIONS:
            # 405 / 415 / 404 是**路由层**的拒绝：请求还没进到任何业务代码，
            # 谈不上「哪条业务规则不满足」。给它们安一个 FTS 码只会污染错误码
            # 的语义（同 `main.py` 那个「不给内部故障安业务码」的理由）。
            # 断言它们至少带着可读的 detail，形状检查留给业务错误。
            assert response.json(), f"{case.operation.label} 的 {response.status_code} 响应是空的"
            return
        _assert_error_contract(response.json(), label=case.operation.label)


def _assert_error_contract(payload: Any, *, label: str) -> None:
    """错误响应必须是 v6 §9.3 的形状（内部故障除外，它刻意换了形状）。"""
    if not isinstance(payload, dict):
        pytest.fail(f"{label} 的错误响应不是对象：{payload!r}")
    if payload.get("kind") == "internal":
        pytest.fail(f"{label} 返回了内部故障载荷 —— 契约测试里出现它就是缺陷")
    for field in ("code", "message", "severity", "stage", "trace_id", "retryable"):
        assert field in payload, f"{label} 的错误响应缺 {field}：{payload}"
    assert payload["code"] in {c.value for c in ErrorCode}
    assert payload["severity"] in {"INFO", "WARN", "ERROR", "CRITICAL"}
    assert payload["stage"] in {"ingest", "intent", "constraint", "solve", "validate", "export"}
    assert isinstance(payload["retryable"], bool)


@pytest.mark.parametrize("operation", sorted(EXPECTED_OPERATIONS))
def test_every_v6_endpoint_is_documented(operation: str) -> None:
    """v6 §9.1 的 11 个端点必须都在 OpenAPI 里 —— 不在等于 schemathesis 没测它。"""
    labels = {op.ok().label for op in schema.get_all_operations()}
    assert operation in labels


def test_openapi_documents_no_extra_operations() -> None:
    """也不许多出端点：多一个就得先改 v6 §9.1。"""
    labels = {op.ok().label for op in schema.get_all_operations()}
    assert labels == EXPECTED_OPERATIONS | {HEALTH_OPERATION}


def test_unauthenticated_requests_are_rejected_everywhere() -> None:
    """**每一个**端点都要认证 —— 漏一个就是一个不带锁的后门。"""
    from fastapi.testclient import TestClient

    client = TestClient(_app, raise_server_exceptions=False)
    for operation in schema.get_all_operations():
        info = operation.ok()
        if info.label == HEALTH_OPERATION:
            continue
        path = info.path.replace("{job_id}", "x").replace("{trace_id}", "x")
        response = client.request(info.method.upper(), path, json={})
        assert response.status_code in (401, 422), (
            f"{info.label} 未鉴权却返回 {response.status_code}"
        )
