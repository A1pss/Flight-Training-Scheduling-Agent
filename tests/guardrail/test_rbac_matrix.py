"""RBAC 四角色 × 全部端点的权限矩阵（v6 §11.5「认证鉴权」）。

> RBAC 四角色：`查看者`（只读）/ `排班员`（发起排班）/ `训练主任`（批准计划、
> 授权 R1 松弛）/ `管理员`（数据与规则变更）

矩阵的声明在 `backend/api/rbac.py`，本文件做三件事：

| # | 断言 | 防的是 |
|---|---|---|
| 表 vs 路由 | OpenAPI 的每条 operation 都在表里，不多不少 | **新端点忘了写 `require_role`** —— 那样它对全员开放，而且测试照样绿 |
| 表 vs 实现 | 四角色 × 12 端点逐格发真请求，403 当且仅当角色不够 | 表和代码各说各话 |
| 档位授权 | Tier 1~3 各自需要的角色 | 「进得了 `/approve`」≠「批得了 Tier 3」 |

## 判据只看 403，不看 200

够格的那一格，请求往下走可能因为「找不到那个 trace_id」而 4xx —— 那是业务
结果，不是权限结果。**权限矩阵只回答「拦没拦」**，把它和业务成败绑在一起，
就得为每一格造一份完整的前置数据，而那时候测的已经不是权限了。

## 401 与 403 是两件事

没带 token / token 错 → **401**（你是谁我不知道）；带了合法 token 但角色不够
→ **403**（我知道你是谁，但你不能干这个）。混成一个码会让排障时分不清
「配错了 token」和「角色发少了」。这里两者各有断言。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.rbac import ENDPOINT_POLICIES, EndpointPolicy
from backend.api.security import Principal
from backend.planner.authority import RELAX_TIER_AUTHORITY, ROLE_RANK
from tests.fixtures.api_fixtures import (
    ROLE_HEADERS,
    RecordingRunner,
    RecordingSessionFactory,
    build_test_app,
    make_settings,
)

pytestmark = pytest.mark.guardrail

#: 逐个端点的最小可发请求。**只求「能走到 require_role」**，不求业务成功。
SAMPLE_REQUESTS: dict[tuple[str, str], dict[str, Any]] = {
    ("GET", "/health"): {"url": "/health"},
    ("POST", "/api/v1/ingest"): {
        "url": "/api/v1/ingest",
        "files": {"files": ("a.pdf", b"%PDF-1.4 x", "application/pdf")},
    },
    ("GET", "/api/v1/ingest/{job_id}/changeset"): {"url": "/api/v1/ingest/ing_x/changeset"},
    ("POST", "/api/v1/ingest/{job_id}/confirm"): {
        "url": "/api/v1/ingest/ing_x/confirm",
        "json": {"approver": "P01"},
    },
    ("POST", "/api/v1/chat"): {
        "url": "/api/v1/chat",
        "json": {"message": "给下周排班", "client_request_id": "rbac-probe"},
    },
    ("POST", "/api/v1/schedule"): {
        "url": "/api/v1/schedule",
        "json": {"week_start": "2026-01-05", "client_request_id": "rbac-probe"},
    },
    ("GET", "/api/v1/jobs/{job_id}"): {"url": "/api/v1/jobs/job_x"},
    ("GET", "/api/v1/runs/{trace_id}"): {"url": "/api/v1/runs/trace_x"},
    ("GET", "/api/v1/plans"): {"url": "/api/v1/plans?week=2026-W02"},
    ("GET", "/api/v1/schedule/{trace_id}/export"): {"url": "/api/v1/schedule/trace_x/export"},
    ("POST", "/api/v1/schedule/{trace_id}/reject"): {
        "url": "/api/v1/schedule/trace_x/reject",
        "json": {"comment": "不行"},
    },
    ("POST", "/api/v1/schedule/{trace_id}/approve"): {
        "url": "/api/v1/schedule/trace_x/approve",
        "json": {"comment": "同意"},
    },
}


@pytest.fixture
def client() -> TestClient:
    """一个不入队、不碰库的 app —— 权限矩阵不需要真跑任何业务。"""
    app, _ = build_test_app(
        settings=make_settings(),
        runner=RecordingRunner(),
        session_factory=RecordingSessionFactory(),
    )
    return TestClient(app, raise_server_exceptions=False)


def _send(client: TestClient, policy: EndpointPolicy, headers: dict[str, str]) -> int:
    sample = SAMPLE_REQUESTS[(policy.method, policy.path)]
    kwargs = {key: value for key, value in sample.items() if key != "url"}
    response = client.request(policy.method, sample["url"], headers=headers, **kwargs)
    return response.status_code


# ═════════════════════════════════════════════════════════════════════
# ① 表 vs 路由
# ═════════════════════════════════════════════════════════════════════
def test_every_route_is_declared_in_the_policy_table(client: TestClient) -> None:
    """OpenAPI 里的每条 operation 都必须在权限表里有一行。

    **这条是「新端点忘了鉴权」的唯一自动防线**：`require_role` 漏写不会有任何
    症状，而这里会红。
    """
    spec = client.app.openapi()  # type: ignore[attr-defined]
    routed = {
        (method.upper(), path)
        for path, ops in spec["paths"].items()
        for method in ops
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }
    declared = {(item.method, item.path) for item in ENDPOINT_POLICIES}
    assert routed == declared, (
        f"权限表与路由对不上。\n只在路由里：{sorted(routed - declared)}\n"
        f"只在表里：{sorted(declared - routed)}"
    )


def test_every_declared_endpoint_has_a_sample_request() -> None:
    """表里每一行都要有一个可发的样例请求，否则那一行等于没测。"""
    missing = [
        f"{item.method} {item.path}"
        for item in ENDPOINT_POLICIES
        if (item.method, item.path) not in SAMPLE_REQUESTS
    ]
    assert missing == [], f"以下端点没有样例请求：{missing}"


def test_only_health_is_anonymous() -> None:
    """匿名可达的必须**只有** `/health`。多一个就是信息泄露面。"""
    anonymous = [item.path for item in ENDPOINT_POLICIES if item.minimum is None]
    assert anonymous == ["/health"]


# ═════════════════════════════════════════════════════════════════════
# ② 表 vs 实现：四角色 × 12 端点逐格
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("policy", ENDPOINT_POLICIES, ids=lambda p: f"{p.method}:{p.path}")
@pytest.mark.parametrize("role", sorted(ROLE_HEADERS))
def test_rbac_matrix_cell(client: TestClient, policy: EndpointPolicy, role: str) -> None:
    """一格 = 一个 (角色, 端点)。403 当且仅当角色不够。"""
    status = _send(client, policy, ROLE_HEADERS[role])
    if policy.minimum is None:
        expected_forbidden = False
    else:
        expected_forbidden = ROLE_RANK[role] < ROLE_RANK[policy.minimum]
    if expected_forbidden:
        assert status == 403, (
            f"{role} 不该能{policy.action}（{policy.method} {policy.path}），实得 {status}"
        )
    else:
        assert status != 403, (
            f"{role} 应当能{policy.action}（{policy.method} {policy.path}），却被 403 拦下"
        )


def test_forbidden_response_says_who_and_what_is_missing(client: TestClient) -> None:
    """403 要说清「谁、什么角色、需要什么角色」，不能只回一句 Forbidden。"""
    response = client.post(
        "/api/v1/schedule/trace_x/approve",
        headers=ROLE_HEADERS["scheduler"],
        json={"comment": "同意"},
    )
    assert response.status_code == 403
    message = response.json()["message"]
    assert "P02" in message and "scheduler" in message and "director" in message


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, 401),
        ({"Authorization": "Bearer nope"}, 401),
        ({"Authorization": "tok-dir"}, 401),
        ({"Authorization": "Basic tok-dir"}, 401),
    ],
)
def test_unauthenticated_is_401_not_403(
    client: TestClient, headers: dict[str, str], expected: int
) -> None:
    """没认证 → 401。**不是 403** —— 两者的排障动作完全不同。"""
    assert client.get("/api/v1/plans?week=2026-W02", headers=headers).status_code == expected


def test_empty_token_table_rejects_everyone() -> None:
    """`API_TOKENS` 为空 = 全部拒绝，**不是全部放行**（v6 §11.5 的默认拒绝）。"""
    app, _ = build_test_app(
        settings=make_settings(API_TOKENS=""),
        runner=RecordingRunner(),
        session_factory=RecordingSessionFactory(),
    )
    probe = TestClient(app, raise_server_exceptions=False)
    response = probe.get("/api/v1/plans?week=2026-W02", headers=ROLE_HEADERS["admin"])
    assert response.status_code == 401
    assert "未配置 API_TOKENS" in response.json()["message"]
    # 但存活探针照样要能答 —— 否则运维连「进程死没死」都问不出来
    assert probe.get("/health").status_code == 200


# ═════════════════════════════════════════════════════════════════════
# ③ 松弛档位授权：进得了端点 ≠ 批得了那一档
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("tier", sorted(RELAX_TIER_AUTHORITY))
@pytest.mark.parametrize("role", sorted(ROLE_HEADERS))
def test_relax_tier_authority_matrix(role: str, tier: int) -> None:
    """档位授权矩阵：`can_authorize_tier` 与 `RELAX_TIER_AUTHORITY` 逐格一致。"""
    principal = Principal(user_id="PX", role=role)  # type: ignore[arg-type]
    required = RELAX_TIER_AUTHORITY[tier]
    assert principal.can_authorize_tier(tier) == (ROLE_RANK[role] >= ROLE_RANK[required])


def test_approve_and_the_highest_tier_require_the_same_role() -> None:
    """第二道门当前**不产生额外拒绝**，这件事要被钉住而不是被跳过。

    实测：`RELAX_TIER_AUTHORITY` 的最高档 Tier 3 需要 `director`，而 `/approve`
    的最低角色也正是 `director` —— 所以不存在「进得了这个端点、却批不了某一档」
    的角色。用 `pytest.skip` 表达这件事是错的（跳过的用例没人看），改成一条
    显式断言：**哪天有人把某一档抬到 admin，或把 approve 降到 scheduler，
    这里就会红**，提醒去补那一格的用例。
    """
    approve = next(item for item in ENDPOINT_POLICIES if item.path.endswith("/approve"))
    highest_requirement = max(ROLE_RANK[role] for role in RELAX_TIER_AUTHORITY.values())
    assert approve.minimum is not None
    assert ROLE_RANK[approve.minimum] == highest_requirement, (
        "approve 的最低角色与最高松弛档位的要求分叉了 —— "
        "现在存在「进得来但批不了」的角色，必须为那一格补一条用例"
    )


def test_schedule_submit_checks_tier_authority(client: TestClient) -> None:
    """提交排班时带一个自己批不了的档位 → 403（不是等到 approve 才发现）。"""
    highest = max(RELAX_TIER_AUTHORITY)
    if ROLE_RANK[RELAX_TIER_AUTHORITY[highest]] <= ROLE_RANK["scheduler"]:
        pytest.skip("最高档排班员就能批，构造不出越权")
    response = client.post(
        "/api/v1/schedule",
        headers=ROLE_HEADERS["scheduler"],
        json={"week_start": "2026-01-05", "relaxation_tier": highest},
    )
    assert response.status_code == 403
