"""端点层单测：鉴权、幂等、锁、错误契约（**不碰数据库、不跑求解**）。

用 `RecordingRunner` 把「提交」和「执行」切开：这一层要验的是 v6 §9.1 那张表
——谁能调、幂等键是什么、拿不到锁怎么办。求解链路由
`tests/integration/test_api_live.py` 用真库跑。

会话替身是 `RecordingSession`：请求里显式给 `snapshot_id` 时 `require_snapshot`
短路，压根不查库。**这不是绕过检查**——「没给快照且库里也没有」那条分支
由集成测试覆盖。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.locks import LockManager
from backend.api.store import InMemoryStore
from backend.core.errors import ErrorCode
from tests.fixtures.api_fixtures import (
    DIRECTOR,
    SCHEDULER,
    VIEWER,
    FailingRunner,
    RecordingRunner,
    build_test_app,
    make_settings,
)

SNAP = "snap_test"
MONDAY = "2026-01-05"


@pytest.fixture
def rig() -> tuple[TestClient, RecordingRunner, InMemoryStore]:
    store = InMemoryStore()
    runner = RecordingRunner()
    app, _ = build_test_app(store=store, runner=runner)
    return TestClient(app, raise_server_exceptions=False), runner, store


def _schedule_body(**overrides: Any) -> dict[str, Any]:
    body = {"week_start": MONDAY, "snapshot_id": SNAP}
    body.update(overrides)
    return body


# ─────────────────────────────────────────────────────────────────────
# 端点清单与文档
# ─────────────────────────────────────────────────────────────────────
def test_openapi_lists_exactly_the_eleven_endpoints_plus_health(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    """v6 §9.1 是 11 个端点。多一个少一个都要先改设计文档。"""
    client, _, _ = rig
    spec = client.get("/api/v1/openapi.json").json()
    paths = {(path, method) for path, ops in spec["paths"].items() for method in ops}
    assert paths == {
        ("/api/v1/ingest", "post"),
        ("/api/v1/ingest/{job_id}/changeset", "get"),
        ("/api/v1/ingest/{job_id}/confirm", "post"),
        ("/api/v1/chat", "post"),
        ("/api/v1/schedule", "post"),
        ("/api/v1/jobs/{job_id}", "get"),
        ("/api/v1/runs/{trace_id}", "get"),
        ("/api/v1/schedule/{trace_id}/approve", "post"),
        ("/api/v1/schedule/{trace_id}/reject", "post"),
        ("/api/v1/schedule/{trace_id}/export", "get"),
        ("/api/v1/plans", "get"),
        ("/health", "get"),
    }


def test_health_reports_versions_and_offline_flag(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    client, _, _ = rig
    payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["offline"] is True
    assert payload["ruleset_version"] and payload["semantics_version"]


def test_trace_id_is_echoed_in_the_response_header(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    client, _, _ = rig
    response = client.get("/health", headers={"X-Trace-Id": "trace-abc"})
    assert response.headers["X-Trace-Id"] == "trace-abc"


# ─────────────────────────────────────────────────────────────────────
# 认证与鉴权
# ─────────────────────────────────────────────────────────────────────
def test_missing_token_is_401_with_the_error_contract(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    client, _, _ = rig
    response = client.post("/api/v1/schedule", json=_schedule_body())
    assert response.status_code == 401
    payload = response.json()
    assert payload["code"] == ErrorCode.TOOL_PERMISSION_DENIED.value
    assert payload["trace_id"]
    assert payload["retryable"] is False


def test_viewer_cannot_submit_a_schedule(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    client, _, _ = rig
    response = client.post("/api/v1/schedule", headers=VIEWER, json=_schedule_body())
    assert response.status_code == 403
    assert "director" not in response.json()["message"]


def test_scheduler_cannot_approve(rig: tuple[TestClient, RecordingRunner, InMemoryStore]) -> None:
    """归档是本系统唯一不可撤销的写 —— 需训练主任。"""
    client, _, _ = rig
    response = client.post("/api/v1/schedule/t1/approve", headers=SCHEDULER, json={})
    assert response.status_code == 403
    assert "director" in response.json()["message"]


def test_director_cannot_authorize_a_tier_beyond_the_ladder(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    client, _, _ = rig
    response = client.post(
        "/api/v1/schedule/t1/approve", headers=DIRECTOR, json={"authorized_tiers": [7]}
    )
    assert response.status_code == 403


def test_scheduler_cannot_submit_tier_three(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    client, _, _ = rig
    response = client.post(
        "/api/v1/schedule", headers=SCHEDULER, json=_schedule_body(relaxation_tier=3)
    )
    assert response.status_code == 403


def test_viewer_can_read_jobs(rig: tuple[TestClient, RecordingRunner, InMemoryStore]) -> None:
    client, _, _ = rig
    response = client.get("/api/v1/jobs/nope", headers=VIEWER)
    assert response.status_code == 400  # 找不到任务，不是没权限
    assert response.json()["code"] == ErrorCode.REQUIRED_INPUT_MISSING.value


# ─────────────────────────────────────────────────────────────────────
# 提交与幂等
# ─────────────────────────────────────────────────────────────────────
def test_schedule_returns_job_id_immediately(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    client, runner, _ = rig
    response = client.post("/api/v1/schedule", headers=SCHEDULER, json=_schedule_body())
    assert response.status_code == 202
    payload = response.json()
    assert payload["job_id"] and payload["trace_id"]
    assert payload["poll_url"] == f"/api/v1/jobs/{payload['job_id']}"
    assert len(runner.payloads) == 1
    assert runner.payloads[0].use_llm is False, "结构化入口是 FTS-4001 降级路径，不该调 LLM"


def test_same_body_twice_returns_the_same_job_id(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    """`/schedule` 没给客户端 UUID 时按请求体指纹幂等。"""
    client, runner, _ = rig
    first = client.post("/api/v1/schedule", headers=SCHEDULER, json=_schedule_body()).json()
    second = client.post("/api/v1/schedule", headers=SCHEDULER, json=_schedule_body()).json()
    assert first["job_id"] == second["job_id"]
    assert second["idempotent_hit"] is True
    assert len(runner.payloads) == 1, "第二次不该再入队"


def test_chat_idempotency_keyed_on_client_uuid(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    client, runner, _ = rig
    # 用问答类表述：它不带周、不加锁，于是这条用例量的**只有**幂等键这一件事
    # （带周的话第三次会撞上 FTS-4005，那是另一条用例的事）
    body = {
        "message": "刘斌的仪表等级何时到期？",
        "client_request_id": "uuid-1",
        "snapshot_id": SNAP,
    }
    first = client.post("/api/v1/chat", headers=SCHEDULER, json=body).json()
    second = client.post("/api/v1/chat", headers=SCHEDULER, json=body).json()
    assert first["job_id"] == second["job_id"]
    assert second["idempotent_hit"] is True

    other = client.post(
        "/api/v1/chat", headers=SCHEDULER, json={**body, "client_request_id": "uuid-2"}
    ).json()
    assert other["job_id"] != first["job_id"], "同一句话换个 UUID 就是新的一次请求"
    assert len(runner.payloads) == 2


def test_chat_resolves_the_week_from_the_utterance(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    """周表述是确定性解析出来的（`routing/entities.py`），不走 LLM。"""
    client, runner, _ = rig
    client.post(
        "/api/v1/chat",
        headers=SCHEDULER,
        json={"message": "给 2026W02 排班", "client_request_id": "u1", "snapshot_id": SNAP},
    )
    assert runner.payloads[0].iso_week == "2026W02"
    assert runner.payloads[0].week_start == "2026-01-05"


def test_chat_without_a_week_takes_no_lock(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    """问答类请求不碰排班资源，锁不该拦它。"""
    client, runner, store = rig
    client.post(
        "/api/v1/chat",
        headers=SCHEDULER,
        json={
            "message": "刘斌的仪表等级何时到期？",
            "client_request_id": "u1",
            "snapshot_id": SNAP,
        },
    )
    assert runner.payloads[0].iso_week == ""
    assert store.scan("fts:api:lock:*") == []


def test_week_start_must_be_a_monday(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    client, _, _ = rig
    response = client.post(
        "/api/v1/schedule", headers=SCHEDULER, json=_schedule_body(week_start="2026-01-06")
    )
    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.REQUIRED_INPUT_MISSING.value


def test_bad_request_body_is_422_with_the_error_contract(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    client, _, _ = rig
    response = client.post("/api/v1/schedule", headers=SCHEDULER, json={"week_start": "不是日期"})
    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == ErrorCode.REQUIRED_INPUT_MISSING.value
    assert payload["retryable"] is True


def test_unknown_field_is_rejected(rig: tuple[TestClient, RecordingRunner, InMemoryStore]) -> None:
    """契约 `extra="forbid"`：多写一个字段就报错，不静默忽略。"""
    client, _, _ = rig
    response = client.post(
        "/api/v1/schedule", headers=SCHEDULER, json=_schedule_body(bogus_field=1)
    )
    assert response.status_code == 422


# ─────────────────────────────────────────────────────────────────────
# 分布式锁（v6 §9.2 / FTS-4005）
# ─────────────────────────────────────────────────────────────────────
def test_second_submission_for_the_same_week_is_rejected(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    client, runner, _ = rig
    client.post("/api/v1/schedule", headers=SCHEDULER, json=_schedule_body())
    response = client.post(
        "/api/v1/schedule",
        headers=DIRECTOR,  # 换个人、换个幂等键，锁照样拦
        json=_schedule_body(client_request_id="another"),
    )
    assert response.status_code == 409
    payload = response.json()
    assert payload["code"] == ErrorCode.SCHEDULE_LOCKED.value
    assert payload["retryable"] is True
    assert payload["details"]["holder"] == "P02"
    assert "2026W02" in payload["details"]["subject"]
    assert len(runner.payloads) == 1


def test_another_week_is_not_blocked(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    client, runner, _ = rig
    client.post("/api/v1/schedule", headers=SCHEDULER, json=_schedule_body())
    response = client.post(
        "/api/v1/schedule", headers=SCHEDULER, json=_schedule_body(week_start="2026-01-12")
    )
    assert response.status_code == 202
    assert len(runner.payloads) == 2


def test_enqueue_failure_releases_the_lock() -> None:
    """入队失败不放锁的话，那一周会被一把没有主人的锁锁死到 TTL 过期。"""
    store = InMemoryStore()
    app, _ = build_test_app(store=store, runner=FailingRunner())
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/v1/schedule", headers=SCHEDULER, json=_schedule_body())
    assert response.status_code == 500
    assert LockManager(store).holder_of("fts:api:lock:schedule:default:2026W02") is None


def test_decision_on_an_unknown_run_is_fts_1004(
    rig: tuple[TestClient, RecordingRunner, InMemoryStore],
) -> None:
    """没有这个运行 → FTS-1004。

    「有这个运行、但它还没走到人工门禁」是另一码事（FTS-4005，状态冲突），
    那条要真跑一次图才构造得出来，放在 `tests/integration/test_api_live.py`。
    """
    client, _, _ = rig
    response = client.post("/api/v1/schedule/never-existed/approve", headers=DIRECTOR, json={})
    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.REQUIRED_INPUT_MISSING.value


# ─────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────
def test_no_tokens_configured_means_everything_is_denied() -> None:
    app, _ = build_test_app(settings=make_settings(API_TOKENS=""), runner=RecordingRunner())
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/v1/jobs/x", headers=VIEWER)
    assert response.status_code == 401
    assert "API_TOKENS" in response.json()["message"]
