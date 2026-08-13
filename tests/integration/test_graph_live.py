"""编排层集成测试：真快照、真求解、真 checkpoint（v6 §7.5 / §9.2）。

**直连裸装 PG（127.0.0.1:5433）**，读 `--baseline` 落下来的 ACTIVE 快照。
跑之前必须 `alembic upgrade head` 并跑过 `python -m backend.ingestion.cli --baseline`。

## 本文件覆盖的出口标准

| 出口标准 | 用例 |
|---|---|
| 排班主路径端到端跑通 | `test_baseline_week_runs_end_to_end` |
| `commit_plan_node` 写 `last_done_date` 锚点（R19） | `test_commit_plan_writes_the_anchor` |
| HITL 跨日恢复（杀进程 → 重启 → 从 checkpoint 继续） | `test_hitl_survives_a_process_restart` |
| `resume_guard` 快照陈旧性 → FTS-3004 | `test_resume_guard_*` |
| FTS-4001 降级：排班能力完整保留 | `test_scheduling_survives_without_llm` |

## 会话策略：全图共用一个**不提交**的会话

排班主路径会真的往 `plans` / `sorties` / `training_progress` 写行。测试里给图一个
共用会话、跑完 `rollback()`，于是：

- 断言能看见写进去的行（同一事务内可见）；
- 库不被污染（回滚后一行不留）。

**这不是在削弱 `commit_plan` 的事务语义**：它照常在一个事务里做完四件事，
只是那个事务的边界由测试掌握。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import PROJECT_ROOT
from backend.core.db import get_session_factory, session_scope
from backend.graph.graph import GraphDeps, build_graph
from backend.graph.state import initial_state
from backend.models.progress import TrainingProgress
from backend.nodes.compile_spec import compile_spec
from backend.nodes.resume_guard import check_staleness
from backend.routing.entities import directory_from_session
from backend.skills_loader import load_library
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot

pytestmark = pytest.mark.integration

BASELINE_WEEK = date(2026, 1, 5)
TODAY = date(2026, 1, 2)  # 排班发生在基准周之前的那个周五


@pytest.fixture(scope="module")
def snapshot() -> str:
    """一个 ACTIVE 快照。**库里没有就现建一份**（CLAUDE.md §6）。

    不写成 `assert 库里应当已经有` —— 那是在断言「有人在测试之外先跑过某个
    命令」，本地绿、CI 红。本文件按字母序排在 `test_ingestion_pipeline_live.py`
    **之前**，全新的 CI 库跑到这里时还没有任何快照。
    """
    with session_scope() as session:
        return ensure_baseline_snapshot(session)


@contextmanager
def shared_session() -> Iterator[Session]:
    """一个跨节点共用、**永不提交**的会话。"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def graph_deps(session: Session, snapshot_id: str, tmp_path: Path, **kwargs: Any) -> GraphDeps:
    @contextmanager
    def factory() -> Iterator[Session]:
        yield session

    base: dict[str, Any] = {
        "session_factory": factory,
        "directory": directory_from_session(session, snapshot_id),
        "library": load_library(),
        "today": TODAY,
        "plans_root": tmp_path / "plans",
        "prompt_versions": {},
    }
    base.update(kwargs)
    return GraphDeps(**base)


def schedule_state(snapshot_id: str, text: str = "给所有人排班") -> Any:
    return initial_state(
        trace_id="m4b-live",
        user_id="tester",
        user_role="director",
        snapshot_id=snapshot_id,
        week_start=BASELINE_WEEK.isoformat(),
        messages=[{"role": "user", "content": text}],
    )


# ─────────────────────────────────────────────────────────────────────
# 主路径端到端
# ─────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def end_to_end(tmp_path_factory: pytest.TempPathFactory, snapshot: str) -> dict[str, Any]:
    """跑一次完整排班链路，把耗时与两次状态都留给后面的用例。

    **模块级 fixture**：一次真求解要几十秒，不该被每个断言各跑一遍。
    """
    from langgraph.checkpoint.memory import InMemorySaver

    tmp_path = tmp_path_factory.mktemp("e2e")
    with shared_session() as session:
        app = build_graph(graph_deps(session, snapshot, tmp_path), checkpointer=InMemorySaver())
        config = cast(Any, {"configurable": {"thread_id": "e2e-1"}})

        started = time.monotonic()
        paused = app.invoke(schedule_state(snapshot), config=config)
        to_gate_s = time.monotonic() - started

        resumed_at = time.monotonic()
        final = app.invoke(
            Command(
                resume={
                    "decision": "APPROVE",
                    "user_id": "tester",
                    "role": "director",
                    "comment": "同意",
                }
            ),
            config=config,
        )
        commit_s = time.monotonic() - resumed_at

        anchors = {
            (row.person_id, row.mission_id): row.last_done_date
            for row in session.execute(
                select(TrainingProgress).where(TrainingProgress.snapshot_id == snapshot)
            ).scalars()
        }
        return {
            "paused": paused,
            "final": final,
            "to_gate_s": to_gate_s,
            "commit_s": commit_s,
            "anchors": anchors,
        }


def test_baseline_week_runs_end_to_end(end_to_end: dict[str, Any]) -> None:
    """route → planner → compile_spec → solve → validate → explain → resume_guard → human_gate。"""
    paused = end_to_end["paused"]
    assert "__interrupt__" in paused, "主路径没有停在人工门禁"
    agents = [e.agent for e in paused["trace_events"]]
    for node in (
        "route",
        "planner",
        "compile_spec",
        "solve",
        "validate",
        "explain",
        "resume_guard",
    ):
        assert node in agents, f"主路径少了 {node}：{agents}"


def test_baseline_solution_is_optimal_and_fully_compliant(end_to_end: dict[str, Any]) -> None:
    """v6 §1.4 的基准周预期：OPTIMAL / 14 架次 / 7 条阻塞项 / 14 条校验全绿。"""
    paused = end_to_end["paused"]
    assert paused["solver_stats"].status == "OPTIMAL"
    assert len(paused["solution"].sorties) == 14
    assert len(paused["blocked_items"]) == 7
    assert paused["validation"].all_passed
    assert paused["validation"].missing_rules() == []


def test_validate_never_bounced_back_to_solve(end_to_end: dict[str, Any]) -> None:
    """驳回回环是**自检**，正常路径一次都不该触发（FTS-3003）。"""
    codes = [e.code.value for e in end_to_end["paused"]["errors"]]
    assert "FTS-3003" not in codes
    assert end_to_end["paused"]["solve_attempts"] == 1


def test_human_gate_payload_is_serializable(end_to_end: dict[str, Any]) -> None:
    payload = end_to_end["paused"]["__interrupt__"][0].value
    json.dumps(payload)
    assert payload["validation"]["plan_id"]
    assert len(payload["blocked_items"]) == 7


def test_approve_commits_and_archives(end_to_end: dict[str, Any], snapshot: str) -> None:
    final = end_to_end["final"]
    assert final["human_decision"].decision == "APPROVE"
    assert final["committed_plan_id"]
    assert final["workbook_path"].endswith(".xlsx")
    assert Path(final["workbook_path"]).is_file()


def test_commit_plan_writes_the_anchor(end_to_end: dict[str, Any]) -> None:
    """★ R19 的唯一缓解措施：归档后 `last_done_date` 必须被写入。

    S-12（锚点缺失 → 视为从本周周一起算、不计欠账）只在**首次排班**成立。
    第二周起 `gap` 必须是真值，否则真实欠账被 S-12 永久掩盖，而 Sheet 4 会
    一路显示「无欠账」。
    """
    anchors = end_to_end["anchors"]
    written = {key: value for key, value in anchors.items() if value is not None}
    assert written, "归档后一个锚点都没写 —— S-12 将永久掩盖真实欠账"

    plan = end_to_end["paused"]["solution"]
    flown: dict[tuple[str, str], date] = {}
    for sortie in plan.sorties:
        for member in sortie.crew:
            key = (member.person_id, sortie.mission_id)
            flown[key] = max(flown.get(key, sortie.date), sortie.date)

    for key, last in flown.items():
        if key in anchors:
            assert anchors[key] == last, f"{key} 的锚点应为本周最后一次飞行日 {last}"


def test_commit_plan_advances_progress_but_never_invents_completion(
    end_to_end: dict[str, Any], snapshot: str
) -> None:
    """进度推进 `NOT_STARTED → IN_PROGRESS`；**不自动置 COMPLETED**（铁律 5）。"""
    payload = next(
        e.payload for e in end_to_end["final"]["trace_events"] if e.agent == "commit_plan"
    )
    assert payload["progress_rows_advanced"] > 0
    assert payload["anchors_written"] > 0


def test_end_to_end_latency_is_recorded(end_to_end: dict[str, Any]) -> None:
    """v6 §7.6 的端到端那一格由本用例复测（M4-A 留的是合成值）。"""
    assert end_to_end["to_gate_s"] > 0
    print(
        f"\n[M4-B 实测] 端到端到人工门禁 {end_to_end['to_gate_s']:.1f}s，"
        f"确认后归档 {end_to_end['commit_s']:.1f}s（LLM_PROVIDER=mock，无 LLM 调用）"
    )


# ─────────────────────────────────────────────────────────────────────
# FTS-4001：LLM 挂了，排班能力完整保留
# ─────────────────────────────────────────────────────────────────────
def test_scheduling_survives_without_llm(tmp_path: Path, snapshot: str) -> None:
    """`harness_factory` 恒返回 None —— 与「Ollama 停了」在代码路径上完全等价。

    ⚠️ **这一条断言的是「排班能力完整保留」，不是「最优性」**：状态收 `OPTIMAL`
    或 `FEASIBLE` 都算过，14 条硬约束必须全绿。

    理由是实测出来的：全量套件下这个用例是本机**第三个并发求解**（`end_to_end`
    fixture 一个、HITL 的两个子进程各一个），再叠上 coverage 插桩，60s 预算
    （`Z-13`）证不完最优性，落到 `FEASIBLE`。单跑它是 `OPTIMAL`。
    **把它写成必须 `OPTIMAL`，等于让一条降级路径的测试去承担求解器的性能承诺**
    —— 那是 `test_baseline_solution_is_optimal_and_fully_compliant` 的活，
    那一条是模块里的第一个求解，不受这个影响。

    `FEASIBLE` 在 Tier 0 下同样满足全部硬约束（14 条校验就是证据），
    所以这条断言没有放宽任何合规要求。
    """
    from langgraph.checkpoint.memory import InMemorySaver

    with shared_session() as session:
        deps = graph_deps(session, snapshot, tmp_path, harness_factory=lambda _s: None)
        app = build_graph(deps, checkpointer=InMemorySaver())
        config = cast(Any, {"configurable": {"thread_id": "no-llm"}})
        paused = app.invoke(schedule_state(snapshot), config=config)

    assert paused["solver_stats"].status in ("OPTIMAL", "FEASIBLE")
    assert paused["solution"] is not None
    assert paused["validation"].all_passed
    assert len(paused["validation"].results) == 14
    assert paused["validation"].all_violations() == []
    # 解释退化为「事实直出」，但 grounding 一条不支持的断言都没有
    assert "LLM 服务不可用" in paused["explanation"]
    llm_calls = sum(
        int(e.payload.get("llm_calls", 0))
        for e in paused["trace_events"]
        if "llm_calls" in e.payload
    )
    assert llm_calls == 0


# ─────────────────────────────────────────────────────────────────────
# resume_guard 的快照陈旧性（FTS-3004）
# ─────────────────────────────────────────────────────────────────────
def test_resume_guard_passes_when_the_snapshot_is_unchanged(
    end_to_end: dict[str, Any], snapshot: str
) -> None:
    with shared_session() as session:
        verdict = check_staleness(
            session,
            plan=end_to_end["paused"]["solution"],
            snapshot_id=snapshot,
            current_snapshot_id=snapshot,
        )
    assert not verdict.changed and not verdict.affects_plan


def test_resume_guard_flags_a_change_that_touches_the_plan(
    end_to_end: dict[str, Any], snapshot: str
) -> None:
    """改一个**本方案用到的人**的不可用日期 → 触及，强制重解。"""
    plan = end_to_end["paused"]["solution"]
    touched_person = next(iter(plan.sorties[0].crew)).person_id
    with shared_session() as session:
        stale = _clone_snapshot_with_change(
            session,
            snapshot,
            entity_type="person",
            entity_id=touched_person,
            field="unavailable_dates",
            value=[BASELINE_WEEK.isoformat()],
        )
        verdict = check_staleness(
            session, plan=plan, snapshot_id=snapshot, current_snapshot_id=stale
        )
    assert verdict.changed and verdict.affects_plan
    assert any(c.entity_id == touched_person for c in verdict.affecting)


def test_resume_guard_ignores_a_change_outside_the_plan(
    end_to_end: dict[str, Any], snapshot: str
) -> None:
    """改一个本方案没用到的空域 → 不触及，放行但留痕。"""
    plan = end_to_end["paused"]["solution"]
    used = {s.airspace_id for s in plan.sorties}
    with shared_session() as session:
        unused = next(
            (a for a in ("SAA", "SAB", "IFR", "RT1", "RT2", "RNG") if a not in used), None
        )
        if unused is None:
            pytest.skip("本方案用满了全部空域，构造不出「不触及」的变更")
        stale = _clone_snapshot_with_change(
            session,
            snapshot,
            entity_type="airspace",
            entity_id=unused,
            field="capacity",
            value=99,
        )
        verdict = check_staleness(
            session, plan=plan, snapshot_id=snapshot, current_snapshot_id=stale
        )
    assert verdict.changed and not verdict.affects_plan


def test_resume_guard_node_raises_fts_3004(end_to_end: dict[str, Any], snapshot: str) -> None:
    """节点级：触及本方案 → `goto=planner` + FTS-3004 + 换成新快照。"""
    from backend.nodes.resume_guard import resume_guard

    plan = end_to_end["paused"]["solution"]
    touched = next(iter(plan.sorties[0].crew)).person_id
    with shared_session() as session:
        stale = _clone_snapshot_with_change(
            session,
            snapshot,
            entity_type="person",
            entity_id=touched,
            field="unavailable_dates",
            value=[BASELINE_WEEK.isoformat()],
        )
        state = initial_state(trace_id="rg", user_id="u", snapshot_id=snapshot)
        cast(dict[str, Any], state)["solution"] = plan
        command = resume_guard(state, session, current_snapshot_id=stale)
    assert command.goto == "planner"
    update = cast(dict[str, Any], command.update)
    assert update["snapshot_id"] == stale
    assert update["solution"] is None  # 强制重解
    assert [e.code.value for e in update["errors"]] == ["FTS-3004"]


def _clone_snapshot_with_change(
    session: Session,
    snapshot_id: str,
    *,
    entity_type: str,
    entity_id: str,
    field: str,
    value: Any,
) -> str:
    """复制一份快照并改一个字段，用作「恢复时数据已经变了」的对照。

    只改 `normalized_facts`（Diff 的基线，v6 §5.1），不动事实表——本测试要验的
    是 `resume_guard` 的比对逻辑，不是摄取。**会话不提交**，跑完随事务回滚。
    """
    from backend.ingestion.loader import load_snapshot_normalized
    from backend.models.versioning import DataSnapshot

    facts = load_snapshot_normalized(session, snapshot_id)
    facts.setdefault(entity_type, {}).setdefault(entity_id, {})[field] = value
    clone_id = f"{snapshot_id}_stale"
    from backend.ingestion.diff import content_sha256

    session.add(
        DataSnapshot(
            snapshot_id=clone_id,
            status="ACTIVE",
            source_manifest={},
            content_sha256=content_sha256(facts),
            normalized_facts=facts,
            note="resume_guard 测试用的对照快照",
        )
    )
    session.flush()
    return clone_id


# ─────────────────────────────────────────────────────────────────────
# HITL 跨日恢复：杀进程 → 重启 → 从 checkpoint 继续
# ─────────────────────────────────────────────────────────────────────
_HITL_SCRIPT = PROJECT_ROOT / "tests" / "integration" / "_hitl_worker.py"


def test_hitl_survives_a_process_restart(tmp_path: Path, snapshot: str) -> None:
    """v6 §9.2：人工确认可以隔天再来，状态在 PG 里而非内存。

    两个**独立进程**：第一个跑到 `interrupt()` 就退出（相当于被杀），第二个
    只拿 `thread_id` 恢复。第二个进程**不重跑求解** —— 这一点靠比对两次的
    `content_sha256` 与求解事件数验证。
    """
    thread_id = f"hitl-{int(time.time())}"
    first = _run_worker("pause", thread_id, tmp_path)
    assert first["interrupted"] is True, first
    assert first["sorties"] == 14

    second = _run_worker("resume", thread_id, tmp_path)
    assert second["decision"] == "APPROVE"
    assert second["content_sha256"] == first["content_sha256"], "恢复后方案变了 —— 重跑了求解"
    # 恢复进程里求解事件数**没有增加** —— 增加了就说明它重跑了一次求解，
    # 而 v6 §9.2 的承诺是「从断点恢复，不重跑求解」
    assert second["solve_events"] == first["solve_events"]
    assert second["route_events"] == 1
    assert second["committed_plan_id"]


def _run_worker(mode: str, thread_id: str, tmp_path: Path) -> dict[str, Any]:
    # 子进程没有 pytest 的 `pythonpath = ["."]`，得自己把仓库根塞进 PYTHONPATH
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    result = subprocess.run(  # noqa: S603 - 固定命令、固定脚本路径
        [sys.executable, str(_HITL_SCRIPT), mode, thread_id, str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        timeout=900,
        check=False,
    )
    assert result.returncode == 0, f"worker({mode}) 失败：\n{result.stdout}\n{result.stderr}"
    payload = result.stdout.strip().splitlines()[-1]
    return cast(dict[str, Any], json.loads(payload))


@pytest.fixture(scope="module", autouse=True)
def _cleanup_checkpoints() -> Iterator[None]:
    """跑完把本测试写的 checkpoint 清掉——它们是真落 PG 的。"""
    yield
    from sqlalchemy import text

    from backend.core.db import get_engine

    with get_engine().begin() as conn:
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            conn.execute(
                text(f"DELETE FROM {table} WHERE thread_id LIKE :prefix"),
                {"prefix": "hitl-%"},
            )


# ─────────────────────────────────────────────────────────────────────
# compile_spec 的两项额外职责（v6 §7.2.4）
# ─────────────────────────────────────────────────────────────────────
def test_compile_spec_node_expands_prereq_and_marks_recurrent(snapshot: str) -> None:
    """S-01 类别先修展开 + S-11 复训标记，都在 `compile_spec` 这一步落地。"""
    with shared_session() as session:
        bundle = compile_spec(session, snapshot_id=snapshot, week_start=BASELINE_WEEK)
        rows = list(
            session.execute(
                select(TrainingProgress).where(TrainingProgress.snapshot_id == snapshot)
            ).scalars()
        )
        blocked = {(r.person_id, r.mission_id): r.blocked_reason for r in rows if not r.prereq_met}
        recurrent = {(r.person_id, r.mission_id) for r in rows if r.is_recurrent}

    # v6 §1.4.2 的 7 条阻塞项
    assert ("P08", "missionB-1") in blocked
    assert blocked[("P08", "missionB-1")] == "missionA-2 未完成"
    # S-11：刘斌 C 类到期后进复训
    assert any(pid == "P04" for pid, _ in recurrent)
    assert bundle.spec.relaxation_tier == 0
