"""HITL 跨日恢复：杀进程 → 重启 → 从 checkpoint 继续（v6 §9.2）。

## 为什么单独一个模块

它要起**两个真的子进程**，而子进程走的是**独立的数据库事务**。放在
`test_graph_live.py` 里时，那个模块的 `end_to_end` fixture 持有一个
**模块级、未提交的写事务**（`materialize_progress` 的 delete+insert、
`advance_progress` 的 update 都在里面），于是子进程的 `commit_plan` 撞上

```
sqlalchemy.orm.exc.StaleDataError:
  UPDATE statement on table 'training_progress' expected to update 7 row(s); 6 were matched.
```

—— 父进程把行锁住了、还改过它们，子进程看到的是另一个快照。
**这不是 `commit_plan` 的 bug，是测试隔离没做够**：真实部署里 RQ worker 之间
本来就不共享事务（v6 §9.2 的 `(tenant, week)` 分布式锁管的是另一件事）。

单独成模块之后，跑到这里时 `test_graph_live` 的模块级会话已经关掉，
子进程独占地跑自己的事务。

## 它证明什么

`interrupt()` + `PostgresSaver` 让状态活过进程重启：第一个进程跑到人工门禁就
退出（等同被杀），第二个进程只拿 `thread_id` 恢复，**不重跑求解**。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from backend.core.config import PROJECT_ROOT
from backend.core.db import session_scope
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def snapshot() -> str:
    """一个 ACTIVE 快照。库里没有就现建一份（CLAUDE.md §6）。"""
    with session_scope() as session:
        return ensure_baseline_snapshot(session)


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
