"""审计留痕（v6 §11.5「审计」）：所有写操作与批准操作入 `audit_log`。

> 操作人、IP、前后值 diff、trace_id

四要素**一个都不能缺**——缺操作人的审计行是废纸，缺 IP 的查不出是哪台机器，
缺 diff 的看不出改了什么，缺 trace_id 的对不上运行轨迹。本文件逐条钉。

## 覆盖口径：全部 POST 端点

`backend/api/rbac.py` 的权限表里每一条 POST 都要留痕；GET 一律不留（读操作
写审计只会把表撑到没人愿意翻）。**这条口径由测试遍历权限表得出**，所以新增
一个 POST 端点而忘了写审计，这里会红。
"""

from __future__ import annotations

from typing import Any, get_args, get_type_hints

import pytest
from fastapi.testclient import TestClient

from backend.api.rbac import ENDPOINT_POLICIES
from backend.core.audit import value_diff
from backend.models.audit import AuditLog
from tests.fixtures.api_fixtures import (
    ADMIN,
    DIRECTOR,
    SCHEDULER,
    VIEWER,
    RecordingRunner,
    RecordingSessionFactory,
    build_test_app,
    make_settings,
)

pytestmark = pytest.mark.guardrail


@pytest.fixture
def rig() -> tuple[TestClient, RecordingSessionFactory]:
    factory = RecordingSessionFactory()
    app, _ = build_test_app(
        settings=make_settings(),
        runner=RecordingRunner(),
        session_factory=factory,
    )
    return TestClient(app, raise_server_exceptions=False), factory


# ═════════════════════════════════════════════════════════════════════
# value_diff 的语义
# ═════════════════════════════════════════════════════════════════════
def test_diff_reports_changed_added_removed() -> None:
    diff = value_diff({"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 9, "d": 4})
    assert diff == {
        "changed": {"b": {"before": 2, "after": 9}},
        "added": {"d": 4},
        "removed": {"c": 3},
    }


def test_diff_of_identical_values_is_empty() -> None:
    """**空 diff 照样写一行审计**：「批准了但什么都没改」本身是有意义的记录。"""
    assert value_diff({"a": 1}, {"a": 1}) == {}


def test_diff_treats_none_as_absent_not_as_empty() -> None:
    """`before=None`（提交类操作本来就没有前值）不该造出一堆假的 `added`……

    ……不对，它**应当**全部算 `added`：从「什么都没有」到「有了这些」，这正是
    新增。这里钉的是「None 与 {} 等价」这一条，免得日后有人为了让 diff 好看
    把 None 悄悄改成别的语义。
    """
    assert value_diff(None, {"a": 1}) == {"added": {"a": 1}}
    assert value_diff({}, {"a": 1}) == {"added": {"a": 1}}
    assert value_diff(None, None) == {}


def test_diff_compares_nested_values_as_a_whole() -> None:
    """嵌套结构整体当一个值比 —— 审计回答「哪个字段变了」，不是「第几层变了」。"""
    diff = value_diff({"x": {"p": 1}}, {"x": {"p": 2}})
    assert diff == {"changed": {"x": {"before": {"p": 1}, "after": {"p": 2}}}}


# ═════════════════════════════════════════════════════════════════════
# 四要素
# ═════════════════════════════════════════════════════════════════════
def test_submit_writes_an_audit_row_with_all_four_elements(
    rig: tuple[TestClient, RecordingSessionFactory],
) -> None:
    client, factory = rig
    response = client.post(
        "/api/v1/schedule",
        headers=SCHEDULER,
        json={"week_start": "2026-01-05", "snapshot_id": "snap_x", "client_request_id": "audit-1"},
    )
    assert response.status_code == 202, response.text
    rows = factory.audit_rows
    assert len(rows) == 1
    row = rows[0]
    assert row.actor == "P02"  # 操作人 = user_id，**不是 token**
    assert row.actor_ip == "testclient"  # IP 来自 request.client.host
    assert row.trace_id and row.trace_id != "unknown"
    assert row.action == "api.schedule.submit"
    assert row.resource_type == "run"
    assert row.after is not None and row.after["week_start"] == "2026-01-05"


def test_audit_never_records_the_token(rig: tuple[TestClient, RecordingSessionFactory]) -> None:
    """审计行里**不许出现 token** —— 口令进日志是最经典的一类泄露。"""
    client, factory = rig
    client.post(
        "/api/v1/schedule",
        headers=SCHEDULER,
        json={"week_start": "2026-01-05", "snapshot_id": "snap_x"},
    )
    blob = str([(r.actor, r.actor_ip, r.before, r.after, r.diff) for r in factory.audit_rows])
    assert "tok-sch" not in blob and "Bearer" not in blob


def test_trace_id_matches_the_response_header(
    rig: tuple[TestClient, RecordingSessionFactory],
) -> None:
    """审计的 trace_id 必须与这次请求返回的那个相同，否则对不上运行轨迹。"""
    client, factory = rig
    response = client.post(
        "/api/v1/schedule",
        headers=SCHEDULER,
        json={"week_start": "2026-01-05", "snapshot_id": "snap_x"},
    )
    assert factory.audit_rows[0].trace_id == response.headers["X-Trace-Id"]


def test_client_supplied_trace_id_is_honoured(
    rig: tuple[TestClient, RecordingSessionFactory],
) -> None:
    client, factory = rig
    client.post(
        "/api/v1/schedule",
        headers={**SCHEDULER, "X-Trace-Id": "trace-from-client"},
        json={"week_start": "2026-01-05", "snapshot_id": "snap_x"},
    )
    assert factory.audit_rows[0].trace_id == "trace-from-client"


# ═════════════════════════════════════════════════════════════════════
# 覆盖面：POST 写、GET 不写
# ═════════════════════════════════════════════════════════════════════
def test_read_endpoints_write_no_audit_rows(
    rig: tuple[TestClient, RecordingSessionFactory],
) -> None:
    client, factory = rig
    for url in (
        "/health",
        "/api/v1/jobs/job_x",
        "/api/v1/runs/trace_x",
        "/api/v1/plans?week=2026-W02",
        "/api/v1/schedule/trace_x/export",
    ):
        client.get(url, headers=VIEWER)
    assert factory.audit_rows == [], "读操作写审计只会把表撑到没人愿意翻"


def test_every_post_endpoint_is_wired_to_the_recorder() -> None:
    """权限表里的每个 POST 端点，其处理器都必须依赖 `CurrentAudit`。

    这是「新 POST 端点忘了留痕」的静态防线：忘了写 `audit.record(...)` 时
    这条会红，不必为每个端点各造一份能跑通的前置数据。
    """
    from backend.api import main as api_main
    from backend.api.audit import AuditRecorder

    app = api_main.create_app(
        settings=make_settings(),
        runner=RecordingRunner(),
        session_factory=RecordingSessionFactory(),
    )
    handlers: dict[tuple[str, str], Any] = {}

    def walk(routes: Any, prefix: str = "") -> None:
        """递归收集 `(方法, 完整路径) → 处理器`。

        ⚠️ 这个 FastAPI 版本把 `include_router(...)` 的结果包成 `_IncludedRouter`：
        它**没有** `.routes`，真正的 `APIRouter` 在 `.original_router`，而
        `/api/v1` 前缀在 `.include_context.prefix` 上。只遍历 `app.routes` 一层
        会看到 11 条里 6 条是包装器（第一版就红在「路由里找不到
        POST /api/v1/ingest」）。
        """
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                context = getattr(route, "include_context", None)
                walk(included.routes, prefix + getattr(context, "prefix", ""))
                continue
            nested = getattr(route, "routes", None)
            if nested:
                walk(nested, prefix)
                continue
            endpoint = getattr(route, "endpoint", None)
            for method in getattr(route, "methods", set()):
                handlers[(method, prefix + getattr(route, "path", ""))] = endpoint

    walk(app.routes)

    missing: list[str] = []
    for policy in ENDPOINT_POLICIES:
        if policy.method != "POST":
            continue
        handler = handlers.get((policy.method, policy.path))
        assert handler is not None, f"路由里找不到 {policy.method} {policy.path}"
        # ⚠️ 不能用 `inspect.signature(...).parameters` 直接看注解：全仓库都写了
        # `from __future__ import annotations`，那里拿到的是**字符串**
        # （`"CurrentAudit"`），与 `AuditRecorder` 永远不相等。必须让
        # `get_type_hints` 把它求值出来，`include_extras=True` 才留得住 Annotated。
        hints = get_type_hints(handler, include_extras=True)
        # `CurrentAudit = Annotated[AuditRecorder, Depends(get_audit)]`：
        # 类型在 `get_args(...)[0]`，`__metadata__` 里放的是 `Depends` 对象。
        wired = any(
            annotation is AuditRecorder or (get_args(annotation) or (None,))[0] is AuditRecorder
            for annotation in hints.values()
        )
        if not wired:
            missing.append(f"{policy.method} {policy.path}")
    assert missing == [], f"以下 POST 端点没有接审计：{missing}"


# ═════════════════════════════════════════════════════════════════════
# 批准操作的前后值
# ═════════════════════════════════════════════════════════════════════
def test_audit_row_is_appended_not_updated() -> None:
    """审计是只追加的：同一个资源被操作两次 → 两行，不是改一行。"""
    factory = RecordingSessionFactory()
    app, _ = build_test_app(
        settings=make_settings(), runner=RecordingRunner(), session_factory=factory
    )
    client = TestClient(app, raise_server_exceptions=False)
    # ⚠️ **两次要用不同的排班周**：同一周的第二次提交会被 `(tenant, week)` 锁
    # 拒掉（FTS-4005），那时候没有写操作发生、自然也不该有审计行 —— 第一版
    # 就是这么写的，于是「两次提交只有一行审计」看起来像审计漏了，其实是锁生效了。
    for week in ("2026-01-05", "2026-01-12"):
        response = client.post(
            "/api/v1/schedule",
            headers=SCHEDULER,
            json={
                "week_start": week,
                "snapshot_id": "snap_x",
                "client_request_id": f"append-{week}",
            },
        )
        assert response.status_code == 202, response.text
    assert len(factory.audit_rows) == 2
    assert len({row.resource_id for row in factory.audit_rows}) == 2


def test_audit_failure_does_not_break_the_request(capsys: pytest.CaptureFixture[str]) -> None:
    """审计写不进去时：请求照常成功，但**日志里必须留下 ERROR**。

    为一次已经生效的业务操作回滚，只会制造「操作做了一半」这种更糟的状态；
    但静默吞掉就等于「这段时间的审计不可信」而没人知道。所以是「不阻断 + 必留痕」。
    """

    class ExplodingSession:
        """能开、能关，但一写就炸。

        ⚠️ **不能让工厂本身炸**：`get_session` 这个依赖也调同一个工厂，那样
        请求会在进业务代码之前就 500，测到的是「库挂了」而不是「审计挂了」。
        第一版正是这么写的，红在 `assert 500 == 202`。
        """

        def add(self, obj: Any) -> None:
            raise RuntimeError("audit_log 写入失败")

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    app, _ = build_test_app(
        settings=make_settings(), runner=RecordingRunner(), session_factory=ExplodingSession
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/schedule",
        headers=SCHEDULER,
        json={"week_start": "2026-01-05", "snapshot_id": "snap_x"},
    )
    assert response.status_code == 202, "审计失败不该拖垮业务请求"
    # ⚠️ 用 `capsys` 而不是 `caplog`：`configure_logging` 里的
    # `basicConfig(force=True)` 会把 pytest 装在 root 上的捕获 handler 一起换掉，
    # 于是 `caplog.records` 恒为空 —— 一条永远拿不到日志的断言会**永远失败**
    # （这次）或**永远通过**（如果写成 `not any(...)`），两种都不能要。
    captured = capsys.readouterr()
    assert "审计写入失败" in captured.out + captured.err, (
        "审计写失败必须留下 ERROR —— 静默吞掉等于「这段时间的审计不可信」而没人知道"
    )


def test_admin_actions_are_attributed_to_the_admin(
    rig: tuple[TestClient, RecordingSessionFactory],
) -> None:
    client, factory = rig
    client.post(
        "/api/v1/schedule",
        headers=ADMIN,
        json={"week_start": "2026-01-05", "snapshot_id": "snap_x"},
    )
    assert factory.audit_rows[0].actor == "P00"


def test_audit_model_carries_the_four_columns() -> None:
    """表结构层面钉一次：四要素各有其列。"""
    columns = set(AuditLog.__table__.columns.keys())
    assert {"actor", "actor_ip", "before", "after", "diff", "trace_id"} <= columns


def test_director_decision_is_attributed(rig: tuple[TestClient, RecordingSessionFactory]) -> None:
    """决策类操作即便因为「找不到这次运行」而失败，也不该留下一行假的成功审计。"""
    client, factory = rig
    response = client.post(
        "/api/v1/schedule/trace_missing/approve", headers=DIRECTOR, json={"comment": "同意"}
    )
    assert response.status_code >= 400
    assert factory.audit_rows == [], "操作没成功却写了审计 = 审计说了假话"
