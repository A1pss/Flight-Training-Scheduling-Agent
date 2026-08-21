"""真链路端到端延迟复测（v6 §7.6 的「端到端」那一格）。

M4-A 把 §7.6 的端到端一格标成**合成值**并写明「M4-B 装完图后要用真链路复测并
替换这一格」——图当时还没装起来。本文件就是那次复测。

**跑的是真 Ollama**（`LLM_PROVIDER=ollama`，`CUDA_VISIBLE_DEVICES=0`），
所以带 `@pytest.mark.ollama`：CI 上没有 GPU、也不该有，那里一律跳过。

## 量的是什么

一次完整排班请求：`route → planner → compile_spec → solve → validate → explain
→ resume_guard → human_gate`，停在人工门禁。分三段计：

| 段 | 含什么 |
|---|---|
| LLM | Planner 规划 + explain 生成与核验重写（route 走规则命中，0 次调用） |
| 求解 | `solve_node` 的墙钟（CP-SAT 优化阶段 + 规范化阶段） |
| 其余 | 编译、校验、装配、序列化 |

**三段分开报，不合成一个数**：合成值正是 M4-A 那一格被标注为「不是实测」的
原因，M4-B 不该用另一个合成值去替换它。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.orm import Session

from backend.core.config import Settings
from backend.core.db import get_session_factory, session_scope
from backend.graph.graph import GraphDeps, build_graph
from backend.graph.state import initial_state
from backend.harness import Harness
from backend.ingestion.loader import active_snapshot_id
from backend.llm.ollama import OllamaProvider
from backend.routing.entities import directory_from_session
from backend.skills_loader import load_library

pytestmark = [pytest.mark.integration, pytest.mark.ollama]

BASELINE_WEEK = date(2026, 1, 5)
TODAY = date(2026, 1, 2)
CFG = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]


def _require_ollama() -> None:
    provider = OllamaProvider(CFG)
    try:
        models = provider.list_models()
    except Exception as exc:  # pragma: no cover —— 没起 Ollama 时跳过
        pytest.skip(f"Ollama 不可连：{exc}")
    if CFG.LLM_MODEL not in models:
        pytest.skip(f"模型 {CFG.LLM_MODEL} 未拉取")


@contextmanager
def _shared(session: Session) -> Iterator[Session]:
    yield session


def test_end_to_end_latency_on_the_real_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """跑一次真链路，把三段耗时打出来 —— §7.6 那一格按它回填。"""
    _require_ollama()
    session = get_session_factory()()
    try:
        with session_scope() as probe:
            snapshot = active_snapshot_id(probe)
        if not snapshot:
            pytest.skip("库里没有 ACTIVE 快照")

        harness = Harness(snapshot_id=snapshot, settings=CFG)
        deps = GraphDeps(
            session_factory=lambda: _shared(session),
            directory=directory_from_session(session, snapshot),
            library=load_library(),
            today=TODAY,
            plans_root=tmp_path / "plans",
            harness_factory=lambda _state: harness,
            settings=CFG,
            prompt_versions={},
        )
        app = build_graph(deps, checkpointer=InMemorySaver())
        state = initial_state(
            trace_id="m4b-latency",
            user_id="tester",
            user_role="director",
            snapshot_id=snapshot,
            week_start=BASELINE_WEEK.isoformat(),
            messages=[{"role": "user", "content": "给所有人排班"}],
        )

        started = time.monotonic()
        result = app.invoke(state, config=cast(Any, {"configurable": {"thread_id": "latency-1"}}))
        wall_s = time.monotonic() - started
    finally:
        session.rollback()
        session.close()

    assert "__interrupt__" in result, "真链路没停在人工门禁"
    solve_s = result["solver_stats"].wall_time_ms / 1000
    llm_calls = sum(
        int(e.payload.get("llm_calls", 0))
        for e in result["trace_events"]
        if "llm_calls" in e.payload
    )
    per_agent = {
        e.agent: e.payload.get("llm_calls", 0)
        for e in result["trace_events"]
        if "llm_calls" in e.payload
    }

    with capsys.disabled():
        print("\n── v6 §7.6 端到端实测（M4-B，真链路）──")
        print(
            f"   Provider {CFG.LLM_PROVIDER} · 模型 {CFG.LLM_MODEL} · GPU {CFG.CUDA_VISIBLE_DEVICES}"
        )
        print(f"   端到端墙钟（到人工门禁）：{wall_s:.1f} s")
        print(f"   其中求解：{solve_s:.1f} s（状态 {result['solver_stats'].status}）")
        print(f"   其中 LLM + 其余：{wall_s - solve_s:.1f} s，LLM 调用 {llm_calls} 次")
        print(f"   逐组件调用数：{per_agent}")
        print(
            f"   架次 {len(result['solution'].sorties)} · 校验全绿 {result['validation'].all_passed}"
        )

    # 不对延迟设阈值 —— 这是**测量**不是性能门禁（铁律 6：报实测，不报目标）
    assert wall_s > 0
    assert result["validation"].all_passed
    # v6 §7.6：规则命中即 0 次 —— 「给所有人排班」命中 `排班`，route 不该调模型
    assert per_agent.get("route", 0) == 0
