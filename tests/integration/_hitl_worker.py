"""HITL 跨日恢复的**独立进程** worker（被 `test_graph_live.py` 启动两次）。

```
python tests/integration/_hitl_worker.py pause  <thread_id> <tmp_dir>
python tests/integration/_hitl_worker.py resume <thread_id> <tmp_dir>
```

`pause` 跑到 `interrupt()` 就退出 —— 进程结束等同于「被杀」，内存里的一切
都没了；`resume` 是一个**全新进程**，只拿 `thread_id` 从 `PostgresSaver` 恢复。

**为什么必须是两个进程**：同进程里再 `invoke` 一次，看起来也像「恢复」，
但它证明不了状态真的落在 PG 上——LangGraph 的内存缓存足以让那种测试通过。
v6 §9.2 承诺的是「进程重启、人工确认可以隔天再来」，那就得真的重启一次。

输出：最后一行是一个 JSON 对象（父进程按行取最后一行解析）。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from sqlalchemy.orm import Session

from backend.core.db import get_session_factory
from backend.graph.checkpointer import checkpoint_dsn
from backend.graph.graph import GraphDeps, build_graph
from backend.graph.state import initial_state
from backend.ingestion.loader import active_snapshot_id
from backend.routing.entities import directory_from_session
from backend.skills_loader import load_library

BASELINE_WEEK = date(2026, 1, 5)
TODAY = date(2026, 1, 2)


@contextmanager
def _shared(session: Session) -> Iterator[Session]:
    yield session


def _deps(session: Session, snapshot_id: str, tmp_dir: Path) -> GraphDeps:
    return GraphDeps(
        session_factory=lambda: _shared(session),
        directory=directory_from_session(session, snapshot_id),
        library=load_library(),
        today=TODAY,
        plans_root=tmp_dir / "plans",
        prompt_versions={},
    )


def main() -> int:
    mode, thread_id, tmp_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    session = get_session_factory()()
    try:
        snapshot = active_snapshot_id(session)
        if not snapshot:
            print(json.dumps({"error": "库里没有 ACTIVE 快照"}))
            return 1

        with PostgresSaver.from_conn_string(checkpoint_dsn()) as saver:
            saver.setup()
            app = build_graph(_deps(session, snapshot, tmp_dir), checkpointer=saver)
            config = cast(Any, {"configurable": {"thread_id": thread_id}})

            if mode == "pause":
                state = initial_state(
                    trace_id=thread_id,
                    user_id="tester",
                    user_role="director",
                    snapshot_id=snapshot,
                    week_start=BASELINE_WEEK.isoformat(),
                    messages=[{"role": "user", "content": "给所有人排班"}],
                )
                result = app.invoke(state, config=config)
                print(
                    json.dumps(
                        {
                            "interrupted": "__interrupt__" in result,
                            "sorties": len(result["solution"].sorties),
                            "content_sha256": result["solution"].content_sha256,
                            "status": result["solver_stats"].status,
                            "solve_events": sum(
                                1 for e in result["trace_events"] if e.agent == "solve"
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
                # ★ 刻意**不提交**：本进程写进 PG 的只有 checkpoint（那是 saver
                #   自己的连接）。业务表的改动随本进程退出一并回滚，库不被污染。
                return 0

            result = app.invoke(
                Command(
                    resume={
                        "decision": "APPROVE",
                        "user_id": "tester",
                        "role": "director",
                        "comment": "隔天回来批的",
                    }
                ),
                config=config,
            )
            print(
                json.dumps(
                    {
                        "decision": result["human_decision"].decision,
                        "content_sha256": result["solution"].content_sha256,
                        "committed_plan_id": result.get("committed_plan_id"),
                        # 恢复过程里不该再有求解事件 —— 有就是重跑了
                        "solve_events": sum(
                            1 for e in result["trace_events"] if e.agent == "solve"
                        ),
                        "route_events": sum(
                            1 for e in result["trace_events"] if e.agent == "route"
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
    finally:
        session.rollback()
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
