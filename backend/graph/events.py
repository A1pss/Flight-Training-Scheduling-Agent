"""轨迹事件与错误项的构造助手（v6 §7.4 / §8.2）。

`trace_events` 用 `add` reducer 累积，`seq` 保证回放顺序。序号由**当前状态里
已有的事件数**推出来——不是全局计数器，因为图会被 checkpoint 之后跨进程恢复，
进程内的计数器活不过一次重启。

```python
events = emit(state, "solve", "solver_stats", {"status": "OPTIMAL"})
return Command(goto="validate", update={"trace_events": events})
```
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from backend.core.errors import ErrorCode, Severity, Stage
from backend.graph.state import FTSState
from backend.schemas.common import ErrorItem, TraceEvent, TraceKind


def next_seq(state: FTSState) -> int:
    """下一个事件序号。"""
    return len(cast(dict[str, Any], state).get("trace_events") or [])


def emit(
    state: FTSState,
    agent: str,
    kind: TraceKind,
    payload: dict[str, Any] | None = None,
    *,
    duration_ms: float | None = None,
    token_usage: dict[str, int] | None = None,
) -> list[TraceEvent]:
    """造一条事件，返回**列表**——直接塞进 `update={"trace_events": ...}`。

    返回列表而不是单个对象，是因为 `add` reducer 要的就是列表；返回单个对象
    然后让每个调用点自己包一层，迟早有人漏包，而漏包的表现是 reducer 把字符串
    按字符拼起来——排查起来毫无线索。
    """
    return [
        TraceEvent(
            seq=next_seq(state),
            ts=datetime.now(),
            agent=agent,
            kind=kind,
            payload=payload or {},
            duration_ms=duration_ms,
            token_usage=token_usage,
        )
    ]


def emit_all(
    state: FTSState,
    items: Sequence[tuple[str, TraceKind, dict[str, Any]]],
) -> list[TraceEvent]:
    """一个节点一次写**多条**事件时用它，序号连续。

    **不要连着调两次 `emit` 再拼起来**：两次都从同一份 `state` 读 `next_seq`，
    于是两条事件拿到同一个序号，回放顺序当场退化成「看运气」。这个坑在
    `tests/unit/test_graph.py::test_trace_events_accumulate_across_nodes` 上钉着。
    """
    base = next_seq(state)
    now = datetime.now()
    return [
        TraceEvent(seq=base + offset, ts=now, agent=agent, kind=kind, payload=payload)
        for offset, (agent, kind, payload) in enumerate(items)
    ]


def error(
    code: ErrorCode,
    message: str,
    *,
    severity: Severity,
    stage: Stage,
    details: dict[str, Any] | None = None,
    suggestions: list[str] | None = None,
    retryable: bool = False,
) -> list[ErrorItem]:
    """造一条错误项，同样返回列表。"""
    return [
        ErrorItem(
            code=code,
            message=message,
            severity=severity,
            stage=stage,
            details=details or {},
            suggestions=suggestions or [],
            retryable=retryable,
        )
    ]


__all__ = ["emit", "emit_all", "error", "next_seq"]
