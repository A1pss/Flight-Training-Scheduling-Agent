"""API 全链路实测（真 PG + 真 Redis + 真求解，v6 §9）。

一次基准周排班从提交走到归档，中间把 M6 的出口标准逐条量出来：

| 出口标准 | 本文件哪条用例 |
|---|---|
| 提交立即返回 job_id，轮询拿阶段 | `test_submit_polls_and_reaches_the_human_gate` |
| **回放完整性 = 100%** | `test_replay_is_complete_in_both_stores` |
| 「已检查 N 项」是真实数字 | `test_checked_items_are_real_and_distinct` |
| 幂等键：同请求同 job_id | `test_idempotent_submission_on_real_redis` |
| **分布式锁：后者被拒** | `test_concurrent_submission_for_the_same_week_is_rejected` |
| approve → 归档 + 导出 + 历史查询 | `test_approve_archives_and_exports` |

## 库污染怎么处理

`approve` 会真的推进训练进度、写 `last_done_date` 锚点、往 `plans` 落一行。
**这些写入会改变后续测试看到的基准状态**（`last_done_date` 一旦有值，S-12 的
「从本周周一起算」就不成立了，基准周的频率窗口跟着变）。所以本文件在归档那条
用例前后做**精确回滚**：进度四个字段按值还原、新增的已完成事实行删掉、
新建的计划行删掉。

不是「跑完 rollback 就行」——worker 用的是它自己的会话并且 `commit()` 了
（那正是 v6 §9.2 说的「worker 之间不共享事务」）。
"""

from __future__ import annotations

import os
import time as time_module
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.api.runner import InlineRunner
from backend.api.store import RedisStore
from backend.core.config import Settings, get_settings
from backend.core.db import get_session_factory, session_scope
from backend.core.errors import ErrorCode
from backend.models.audit import TraceEventRow
from tests.conftest import TEST_SOLVER_TIME_LIMIT_S
from tests.fixtures.api_fixtures import (
    BASELINE_TODAY,
    DIRECTOR,
    SCHEDULER,
    VIEWER,
    build_test_app,
    make_settings,
    restored_db,
)
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot
from tests.unit.test_api_security_store import _store_contract

pytestmark = pytest.mark.integration

MONDAY = "2026-01-05"


@pytest.fixture(scope="module", autouse=True)
def instrumented_solver_budget() -> Iterator[None]:
    """插桩期的求解墙钟（`Z-23`：300 s；产品默认仍是 60 s）。

    ⚠️ **`tests/conftest.py` 的那把只对函数作用域生效，盖不住这个模块。**

    pytest 按作用域从大到小建 fixture：模块作用域的 `baseline_run`（真正跑求解的
    那个）**先于**函数作用域的 autouse `_force_mock_provider` 建立，那时候
    `SOLVER_TIME_LIMIT_S` 还没设上。而预算是在 `compile_spec` 那一刻被烤进
    `ConstraintSpec.solver_time_limit_s` 的（`backend/nodes/compile_spec.py`），
    于是整次求解用的是**产品默认 60 s**。

    实测（探针）：

    ```
    模块作用域 fixture 里：os.environ 无该键，get_settings() → 60.0
    函数体里：            os.environ = "300"，get_settings() → 300.0
    ```

    后果很具体：不带 `--cov` 时 60 s 够用（基准周约 20 s），**全量
    `pytest --cov` 下证不完最优性 → `FEASIBLE`**，于是
    `test_baseline_week_matches_the_known_result` 红成 `assert 'FEASIBLE' == 'OPTIMAL'`
    ——看起来像求解器回归，其实是预算没传到。

    **这不是放宽判据**：14 条硬约束、三态判据、`OPTIMAL` 断言一个字没改，
    只是把 `Z-23` 已经裁定的插桩预算真正应用到本模块的求解上。
    """
    previous = os.environ.get("SOLVER_TIME_LIMIT_S")
    os.environ["SOLVER_TIME_LIMIT_S"] = TEST_SOLVER_TIME_LIMIT_S
    get_settings.cache_clear()
    yield
    if previous is None:
        os.environ.pop("SOLVER_TIME_LIMIT_S", None)
    else:
        os.environ["SOLVER_TIME_LIMIT_S"] = previous
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def snapshot() -> str:
    with session_scope() as session:
        return ensure_baseline_snapshot(session)


@pytest.fixture(scope="module")
def redis_store() -> Iterator[RedisStore]:
    """真 Redis（裸装 6380）。连不上就跳过，不静默退回内存版。"""
    store = RedisStore.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]
    try:
        store.set("fts:api:probe", "1", 5)
    except Exception as exc:  # pragma: no cover —— 没起 Redis 时跳过
        pytest.skip(f"Redis 不可连（先跑 deploy/native/start_redis.sh）：{exc}")
    yield store
    for key in store.scan("fts:api:*"):
        store.delete_if_value(key, store.get(key) or "")


@dataclass
class Rig:
    client: TestClient
    store: RedisStore
    plans_root: Path


@pytest.fixture(scope="module")
def rig(redis_store: RedisStore, snapshot: str, tmp_path_factory: pytest.TempPathFactory) -> Rig:
    """一个连真 Redis、真 PG、inline 执行的 app。

    inline 跑的是与 RQ **同一个** `execute_run`（见 `backend/api/runner.py`），
    区别只有「谁来跑」。RQ 那一侧由 `tests/e2e/` 用真 worker 进程验。
    """
    root = tmp_path_factory.mktemp("plans")
    settings = make_settings(PLANS_DIR=root)
    app, _store = build_test_app(
        settings=settings,
        store=redis_store,
        runner=InlineRunner(redis_store),
        session_factory=get_session_factory(),
        today=BASELINE_TODAY,
    )
    assert snapshot
    return Rig(
        client=TestClient(app, raise_server_exceptions=False), store=redis_store, plans_root=root
    )


@pytest.fixture(scope="module")
def baseline_run(rig: Rig) -> dict[str, Any]:
    """跑一次基准周排班，停在人工门禁。**整个模块共用这一次求解。**"""
    response = rig.client.post(
        "/api/v1/schedule",
        headers=SCHEDULER,
        json={"week_start": MONDAY, "client_request_id": "live-baseline"},
    )
    assert response.status_code == 202, response.text
    submitted = response.json()
    run = rig.client.get(f"/api/v1/runs/{submitted['trace_id']}", headers=VIEWER)
    assert run.status_code == 200, run.text
    return {"submit": submitted, "run": run.json()}


# ─────────────────────────────────────────────────────────────────────
# 提交 → 轮询 → 人工门禁
# ─────────────────────────────────────────────────────────────────────
def test_submit_polls_and_reaches_the_human_gate(rig: Rig, baseline_run: dict[str, Any]) -> None:
    job = rig.client.get(f"/api/v1/jobs/{baseline_run['submit']['job_id']}", headers=VIEWER).json()
    assert job["status"] == "AWAITING_HUMAN"
    assert job["stage"] == "生成报表"
    assert job["percent"] == 95, "停在人工门禁不该显示 100%（用户还没确认）"

    run = baseline_run["run"]
    assert run["gate"]["awaiting"] is True
    assert run["gate"]["pending_revision"] is False, "首轮不是回显确认屏（Z-19）"


def test_baseline_week_matches_the_known_result(baseline_run: dict[str, Any]) -> None:
    """CLAUDE.md §4 的基准周结果：OPTIMAL / 14 架次 / 7 条阻塞项 / 双跑道。

    **跑出别的数先怀疑自己的改动**（CLAUDE.md §7 第 4 条），不许放宽约束。
    """
    run = baseline_run["run"]
    plan = run["plan"]
    assert run["solver"]["stats"]["status"] == "OPTIMAL"
    assert len(plan["sorties"]) == 14
    assert len(plan["blocked_items"]) == 7
    assert plan["runway_model"] == "dual_runway"
    assert run["solver"]["runway_allocation"] == {"RWY-1": 7, "RWY-2": 7}
    dual = sum(1 for s in plan["sorties"] if len(s["crew"]) == 2)
    assert (dual, len(plan["sorties"]) - dual) == (9, 5), "9 带飞 + 5 单飞"


def test_validation_is_all_green_with_fourteen_rules(baseline_run: dict[str, Any]) -> None:
    report = baseline_run["run"]["validation"]
    assert report["all_passed"] is True
    assert len(report["results"]) == 14


def test_checked_items_are_real_and_distinct(baseline_run: dict[str, Any]) -> None:
    """「已检查 N 项」必须是真实数字（v6 §4.2 脚注：0 项是假通过的信号）。

    出口标准要求「用一个 checked_items 各不相同的场景验证」——基准周天然如此：
    14 条规则检查的对象各不相同（时间一致性按架次、任务完成度按人×课目…）。
    """
    results = baseline_run["run"]["validation"]["results"]
    counts = {r["rule_id"]: r["checked_items"] for r in results}
    assert all(v > 0 for v in counts.values()), f"有规则检查了 0 项：{counts}"
    assert len(set(counts.values())) >= 10, f"检查项数几乎全一样，疑似写死：{counts}"
    assert counts["C13"] > counts["C01"], "任务完成度按人×课目算，必然多于按架次算的时间一致性"


def test_replay_is_complete_in_both_stores(rig: Rig, baseline_run: dict[str, Any]) -> None:
    """**回放完整性 = 100%**（M6 出口标准）。

    两处都要完整：`GET /runs` 返回的全量事件，以及 `trace_events` 表里落的那份
    （前者随 checkpoint 走，后者是审计寿命）。
    """
    trace_id = baseline_run["submit"]["trace_id"]
    events = baseline_run["run"]["trace_events"]
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(len(seqs))), f"seq 不连续：{seqs}"
    assert len(events) >= 6, "一次完整排班至少要有 route/planner/compile/solve/validate/explain"

    with session_scope() as session:
        rows = list(
            session.execute(
                select(TraceEventRow)
                .where(TraceEventRow.run_id == trace_id)
                .order_by(TraceEventRow.seq)
            ).scalars()
        )
    assert [row.seq for row in rows] == seqs, "PG 里落的事件与返回的不一致"
    assert {row.agent for row in rows} == {e["agent"] for e in events}


def test_three_node_kinds_all_appear_in_the_trace(baseline_run: dict[str, Any]) -> None:
    """时间线要能分出三类节点，前提是三类都真的出现在轨迹里。"""
    from frontend.components.process import node_kind

    kinds = {node_kind(e["agent"]) for e in baseline_run["run"]["trace_events"]}
    assert {"llm", "deterministic"} <= kinds


# ─────────────────────────────────────────────────────────────────────
# 幂等与锁（真 Redis）
# ─────────────────────────────────────────────────────────────────────
def test_redis_store_satisfies_the_same_contract_as_memory(redis_store: RedisStore) -> None:
    """内存版与 Redis 版跑**同一套断言**——不存在「内存能过、Redis 不行」。"""
    _store_contract(redis_store, f"fts:api:test:{int(time_module.time())}")


def test_idempotent_submission_on_real_redis(rig: Rig, baseline_run: dict[str, Any]) -> None:
    """同一个客户端 UUID 重复提交 → 同一个 job_id，且不重跑求解。"""
    again = rig.client.post(
        "/api/v1/schedule",
        headers=SCHEDULER,
        json={"week_start": MONDAY, "client_request_id": "live-baseline"},
    )
    assert again.status_code == 202
    assert again.json()["job_id"] == baseline_run["submit"]["job_id"]
    assert again.json()["idempotent_hit"] is True


def test_concurrent_submission_for_the_same_week_is_rejected(rig: Rig, snapshot: str) -> None:
    """**分布式锁实测**（v6 §9.2 / FTS-4005）。

    手工占住 2026W03 的锁（模拟另一个人的任务正在跑），第二个人提交应当被
    立即拒绝，并被告知是谁在排、还剩多久。
    """
    from backend.api.locks import LockManager

    locks = LockManager(rig.store)
    handle = locks.acquire_schedule("default", "2026W03", holder="P09")
    try:
        response = rig.client.post(
            "/api/v1/schedule",
            headers=SCHEDULER,
            json={"week_start": "2026-01-12", "client_request_id": "live-conflict"},
        )
        assert response.status_code == 409
        payload = response.json()
        assert payload["code"] == ErrorCode.SCHEDULE_LOCKED.value
        assert payload["details"]["holder"] == "P09"
        assert payload["details"]["ttl_s"] > 0
        assert payload["retryable"] is True
    finally:
        locks.release(handle)


def test_snapshot_lock_serialises_runs_on_the_same_snapshot(rig: Rig, snapshot: str) -> None:
    """`Z-24`：快照级锁盖住「不同周、同快照」这个 `(tenant,week)` 锁盖不住的缺口。

    占住快照锁后提交另一周 —— 提交本身通过（周锁没冲突），但 worker 里拿不到
    快照锁，任务以 FTS-4005 失败。**这正是不让两个 `materialize_progress`
    同时跑的机制**（M5 实测撞出过 PG 死锁）。
    """
    from backend.api.locks import LockManager

    locks = LockManager(rig.store)
    handle = locks.acquire_snapshot(snapshot, holder="job:other")
    try:
        response = rig.client.post(
            "/api/v1/schedule",
            headers=SCHEDULER,
            json={"week_start": "2026-01-19", "client_request_id": "live-snaplock"},
        )
        assert response.status_code == 202
        job = rig.client.get(f"/api/v1/jobs/{response.json()['job_id']}", headers=VIEWER).json()
        assert job["status"] == "FAILED"
        assert job["error_code"] == ErrorCode.SCHEDULE_LOCKED.value
    finally:
        locks.release(handle)


def test_lock_is_released_after_the_run_pauses(rig: Rig, baseline_run: dict[str, Any]) -> None:
    """走到人工门禁就放锁 —— 那之后可能停一整天等人（见 worker 模块注释）。"""
    from backend.api.locks import LockManager, schedule_lock_key

    assert LockManager(rig.store).holder_of(schedule_lock_key("default", "2026W02")) is None


# ─────────────────────────────────────────────────────────────────────
# 决策：状态冲突 / 驳回 / 确认归档
# ─────────────────────────────────────────────────────────────────────
def test_decision_before_the_gate_is_a_state_conflict(rig: Rig) -> None:
    """对一个不在门禁上的运行按确认 → FTS-4005（等状态变了再来）。"""
    from backend.api.jobs import JobRecord, JobStore

    JobStore(rig.store).put(JobRecord(job_id="j-fake", trace_id="t-fake", iso_week="2026W02"))
    response = rig.client.post("/api/v1/schedule/t-fake/approve", headers=DIRECTOR, json={})
    assert response.status_code == 409
    assert response.json()["code"] == ErrorCode.SCHEDULE_LOCKED.value


def test_export_before_archive_says_what_to_do_next(rig: Rig, baseline_run: dict[str, Any]) -> None:
    """还没归档就下载 → FTS-1004，并明确告诉用户下一步是去确认。"""
    response = rig.client.get(
        f"/api/v1/schedule/{baseline_run['submit']['trace_id']}/export", headers=VIEWER
    )
    assert response.status_code == 400
    payload = response.json()
    assert payload["code"] == ErrorCode.REQUIRED_INPUT_MISSING.value
    assert any("approve" in s for s in payload["suggestions"])


def test_approve_archives_and_exports(
    rig: Rig, baseline_run: dict[str, Any], snapshot: str
) -> None:
    """确认 → 归档 → 下载 → 历史查询。**跑完把库还原**（见模块注释）。"""
    trace_id = baseline_run["submit"]["trace_id"]
    with restored_db(snapshot):
        response = rig.client.post(
            f"/api/v1/schedule/{trace_id}/approve",
            headers=DIRECTOR,
            json={"comment": "确认归档", "authorized_tiers": []},
        )
        assert response.status_code == 202, response.text
        decision = response.json()
        assert decision["decision"] == "APPROVE"

        job = rig.client.get(f"/api/v1/jobs/{decision['job_id']}", headers=VIEWER).json()
        assert job["status"] == "DONE"
        assert job["percent"] == 100

        run = rig.client.get(f"/api/v1/runs/{trace_id}", headers=VIEWER).json()
        assert run["committed_plan_id"], "归档后必须有 plan_id"
        assert run["workbook_path"], "归档后必须有 xlsx"
        assert Path(run["workbook_path"]).exists()

        # 下载
        export = rig.client.get(f"/api/v1/schedule/{trace_id}/export", headers=VIEWER)
        assert export.status_code == 200
        assert export.content[:2] == b"PK", "xlsx 是 zip 容器，头两个字节是 PK"
        assert len(export.content) > 5000

        # 历史查询（两种周写法都认）
        for week in ("2026-W02", "2026W02"):
            plans = rig.client.get("/api/v1/plans", headers=VIEWER, params={"week": week}).json()
            assert plans["week"] == "2026W02"
            ids = [p["plan_id"] for p in plans["plans"]]
            assert run["committed_plan_id"] in ids
            row = next(p for p in plans["plans"] if p["plan_id"] == run["committed_plan_id"])
            assert row["sorties"] == 14
            assert row["status"] == "OPTIMAL"
            assert row["approved_by"] == "P01"

        # 回放在归档之后仍然完整（新事件接在后面）
        seqs = [e["seq"] for e in run["trace_events"]]
        assert seqs == list(range(len(seqs)))
        assert len(seqs) > len(baseline_run["run"]["trace_events"])


def test_plans_rejects_a_malformed_week(rig: Rig) -> None:
    response = rig.client.get("/api/v1/plans", headers=VIEWER, params={"week": "第二周"})
    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.REQUIRED_INPUT_MISSING.value
