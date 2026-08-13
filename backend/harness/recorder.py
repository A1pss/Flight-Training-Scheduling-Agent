"""录制与重放（v6 §7.7.1 第 7 行 / §7.7.4 / §12.5.2）。

> 记录每次 LLM 请求/响应、每次工具调用与返回。`replay(trace_id)` 用录制的响应
> 重跑图，**完全不调 LLM**。

录制落在 `traces/<trace_id>/`：

```
traces/<trace_id>/
├── events.jsonl   # 逐条事件，顺序即发生顺序
└── meta.json      # provider/model/snapshot/prompt 版本/预算用量/最终状态
```

**为什么工具调用也要重放**：只重放 LLM、工具照常执行，那重放结果就取决于库里
此刻的数据——今天重放昨天的 trace，`sql_query` 返回的行数变了，最终状态自然对
不上，而这与「图的逻辑有没有被改坏」毫无关系。§12.5.2 要的重放一致率 100% 只有
在「外部世界也一并冻结」时才是个有意义的断言。

**为什么 `latency_ms` 不参与比对**：它是唯一进 trace 的非确定量（同一台机器上
两次跑都不会一样）。它有用——§7.6 的端到端延迟就靠它实测——但它不进一致性判定，
也不进任何指纹（铁律 9）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from backend.core.config import Settings, get_settings
from backend.core.errors import LLMUnavailableError
from backend.harness.types import ComponentName
from backend.llm.provider import request_fingerprint
from backend.llm.types import LLMRequest, LLMResponse

if TYPE_CHECKING:  # 仅类型注解需要；运行时由 `replay()` 内部局部 import
    from backend.harness.harness import Harness
    from backend.harness.registry import ToolRegistry
    from backend.harness.validation import EntityIndex

EVENTS_FILENAME = "events.jsonl"
META_FILENAME = "meta.json"


class LLMEvent(BaseModel):
    """一次 LLM 往返。`request_key` 是重放时逐次核对的指纹。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["llm"] = "llm"
    seq: int = Field(ge=0)
    component: str
    request_key: str
    request: LLMRequest
    response: LLMResponse
    prompt_version: str = ""
    mode: str = "native"
    latency_ms: float = 0.0


class ToolEvent(BaseModel):
    """一次工具调用与返回。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["tool"] = "tool"
    seq: int = Field(ge=0)
    component: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    value: Any = None
    error: str = ""
    cached: bool = False
    latency_ms: float = 0.0


class NoteEvent(BaseModel):
    """备注事件：契约失败、模式切换、上下文裁剪、预算熔断。

    它们不参与重放，但**必须录**——§12.5.1 的失败模式分布表就是从这里离线统计
    出来的，没有它就只剩一个「通过率 97%」的孤零零的数。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["note"] = "note"
    seq: int = Field(ge=0)
    component: str
    topic: Literal["contract_failure", "mode_switch", "context_trim", "budget", "degrade", "acl"]
    level: Literal["INFO", "WARN", "ERROR", "CRITICAL"] = "WARN"
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


TraceEvent: TypeAlias = Annotated[LLMEvent | ToolEvent | NoteEvent, Field(discriminator="kind")]
_EVENT_ADAPTER: TypeAdapter[TraceEvent] = TypeAdapter(TraceEvent)
_E = TypeVar("_E", LLMEvent, ToolEvent, NoteEvent)


class TraceMeta(BaseModel):
    """一条轨迹的元信息。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: str
    created_at: str = ""
    provider: str = ""
    model: str = ""
    snapshot_id: str = ""
    #: 本次运行用到的全部提示词版本（§7.7.1 第 8 行 / §10.6 manifest）
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    llm_calls: int = 0
    tool_calls: int = 0
    #: 运行结束时的最终状态，重放一致性逐字段比的就是它
    final_state: dict[str, Any] = Field(default_factory=dict)
    finished: bool = False


class TraceRecorder:
    """把一次运行录到 `traces/<trace_id>/`。

    `root=None` 时只在内存里记（单测默认形态：不落盘、不留垃圾）。
    """

    def __init__(
        self,
        trace_id: str,
        *,
        root: Path | None = None,
        provider: str = "",
        model: str = "",
        snapshot_id: str = "",
        prompt_versions: dict[str, str] | None = None,
    ) -> None:
        self.trace_id = trace_id
        self._root = root
        self._events: list[TraceEvent] = []
        self._seq = 0
        self.meta = TraceMeta(
            trace_id=trace_id,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            provider=provider,
            model=model,
            snapshot_id=snapshot_id,
            prompt_versions=dict(prompt_versions or {}),
        )
        if self._root is not None:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._events_path.write_text("", encoding="utf-8")

    # ── 路径 ─────────────────────────────────────────────────────────
    @property
    def dir(self) -> Path:
        if self._root is None:
            raise LLMUnavailableError("本次录制未指定 traces 根目录（内存模式）")
        return self._root / self.trace_id

    @property
    def _events_path(self) -> Path:
        return self.dir / EVENTS_FILENAME

    # ── 录 ───────────────────────────────────────────────────────────
    def log_llm(
        self,
        component: ComponentName,
        request: LLMRequest,
        response: LLMResponse,
        *,
        prompt_version: str = "",
        mode: str = "native",
        latency_ms: float = 0.0,
    ) -> LLMEvent:
        event = LLMEvent(
            seq=self._next_seq(),
            component=component,
            request_key=request_fingerprint(request),
            request=request,
            response=response,
            prompt_version=prompt_version,
            mode=mode,
            latency_ms=round(latency_ms, 3),
        )
        self.meta.llm_calls += 1
        return self._append(event)

    def log_tool(
        self,
        component: ComponentName,
        tool: str,
        arguments: dict[str, Any],
        *,
        ok: bool = True,
        value: Any = None,
        error: str = "",
        cached: bool = False,
        latency_ms: float = 0.0,
    ) -> ToolEvent:
        event = ToolEvent(
            seq=self._next_seq(),
            component=component,
            tool=tool,
            arguments=arguments,
            ok=ok,
            value=value,
            error=error,
            cached=cached,
            latency_ms=round(latency_ms, 3),
        )
        self.meta.tool_calls += 1
        return self._append(event)

    def note(
        self,
        component: ComponentName,
        topic: Literal[
            "contract_failure", "mode_switch", "context_trim", "budget", "degrade", "acl"
        ],
        message: str,
        *,
        level: Literal["INFO", "WARN", "ERROR", "CRITICAL"] = "WARN",
        detail: dict[str, Any] | None = None,
    ) -> NoteEvent:
        return self._append(
            NoteEvent(
                seq=self._next_seq(),
                component=component,
                topic=topic,
                level=level,
                message=message,
                detail=detail or {},
            )
        )

    def finish(self, final_state: dict[str, Any] | None = None) -> TraceMeta:
        """收尾：写 meta.json。**最终状态在这里定型**，重放拿它做比对基准。"""
        self.meta.final_state = dict(final_state or {})
        self.meta.finished = True
        if self._root is not None:
            (self.dir / META_FILENAME).write_text(
                json.dumps(self.meta.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return self.meta

    # ── 读 ───────────────────────────────────────────────────────────
    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def _append(self, event: _E) -> _E:
        self._events.append(event)
        if self._root is not None:
            line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            with self._events_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return event


class Trace(BaseModel):
    """读回来的一条轨迹。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    meta: TraceMeta
    events: tuple[TraceEvent, ...] = ()

    @property
    def llm_events(self) -> tuple[LLMEvent, ...]:
        return tuple(e for e in self.events if isinstance(e, LLMEvent))

    @property
    def tool_events(self) -> tuple[ToolEvent, ...]:
        return tuple(e for e in self.events if isinstance(e, ToolEvent))

    @property
    def note_events(self) -> tuple[NoteEvent, ...]:
        return tuple(e for e in self.events if isinstance(e, NoteEvent))


def load_trace(trace_id: str, root: Path | None = None, settings: Settings | None = None) -> Trace:
    """从磁盘读回一条轨迹。"""
    cfg = settings or get_settings()
    base = (root or cfg.TRACES_DIR) / trace_id
    meta_path = base / META_FILENAME
    events_path = base / EVENTS_FILENAME
    if not meta_path.is_file() or not events_path.is_file():
        raise LLMUnavailableError(
            f"轨迹 {trace_id} 不完整或不存在",
            details={
                "dir": str(base),
                "meta": meta_path.is_file(),
                "events": events_path.is_file(),
            },
        )
    meta = TraceMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
    events = tuple(
        _EVENT_ADAPTER.validate_json(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    return Trace(meta=meta, events=events)


class ToolReplayer:
    """按录制次序回放工具返回值。

    与 `ReplayProvider` 同一套严格口径：**次序 + 内容双重核对**。工具名或参数
    对不上就抛，不去执行真的 handler——重放期间碰真库，重放就不叫重放了。
    """

    def __init__(self, events: Sequence[ToolEvent]) -> None:
        self._events = tuple(events)
        self._cursor = 0

    @property
    def remaining(self) -> int:
        return len(self._events) - self._cursor

    def next_result(self, tool: str, arguments: dict[str, Any]) -> ToolEvent:
        if self._cursor >= len(self._events):
            raise LLMUnavailableError(
                f"重放：第 {self._cursor + 1} 次工具调用（{tool}）没有对应录制",
                details={"tool": tool, "recorded_tool_calls": len(self._events)},
            )
        event = self._events[self._cursor]
        if event.tool != tool or event.arguments != arguments:
            raise LLMUnavailableError(
                f"重放：第 {self._cursor + 1} 次工具调用与录制不符",
                details={
                    "position": self._cursor,
                    "expected_tool": event.tool,
                    "actual_tool": tool,
                    "expected_arguments": event.arguments,
                    "actual_arguments": arguments,
                },
                suggestions=["图的逻辑或工具参数改了 —— 这正是重放要抓的东西，不是重放本身的 bug"],
            )
        self._cursor += 1
        return event


class ReplayResult(BaseModel):
    """一次重放的结论。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trace_id: str
    consistent: bool
    diff: tuple[str, ...] = ()
    final_state: dict[str, Any] = Field(default_factory=dict)
    expected_final_state: dict[str, Any] = Field(default_factory=dict)
    llm_calls: int = 0
    tool_calls: int = 0


def diff_states(expected: dict[str, Any], actual: dict[str, Any]) -> tuple[str, ...]:
    """逐字段比较最终状态（§12.5.2 的「逐字段相等」）。"""
    problems: list[str] = []
    for key in sorted(set(expected) | set(actual)):
        if key not in expected:
            problems.append(f"{key}：原始运行没有这个字段，重放多出 {actual[key]!r}")
        elif key not in actual:
            problems.append(f"{key}：重放缺失，原始运行为 {expected[key]!r}")
        elif expected[key] != actual[key]:
            problems.append(f"{key}：原始 {expected[key]!r} ≠ 重放 {actual[key]!r}")
    return tuple(problems)


def replay(
    trace_id: str,
    run: Callable[[Harness], dict[str, Any]],
    *,
    root: Path | None = None,
    settings: Settings | None = None,
    registry: ToolRegistry | None = None,
    entity_index: EntityIndex | None = None,
    replay_tools: bool = True,
) -> ReplayResult:
    """用录制的响应重跑一次，**对 Ollama 的实际请求数为 0**。

    `run` 拿到一个装好 `ReplayProvider` 的 Harness，跑与录制时相同的流程，
    返回最终状态；这里负责比对。

    `replay_tools=True`（默认）时工具也从轨迹回放，`registry` 只用于导出工具
    schema；置 `False` 时工具会真跑，那就必须传一个接好线的 `registry`。
    """
    from backend.harness.harness import Harness as HarnessImpl  # 局部 import：避免循环

    cfg = settings or get_settings()
    base = root or cfg.TRACES_DIR
    trace = load_trace(trace_id, root=base, settings=cfg)

    replay_cfg = cfg.model_copy(
        update={"LLM_PROVIDER": "replay", "REPLAY_TRACE_DIR": base / trace_id}
    )
    harness = HarnessImpl.for_replay(
        trace=trace,
        settings=replay_cfg,
        registry=registry,
        entity_index=entity_index,
        replay_tools=replay_tools,
    )
    final_state = run(harness)
    diff = diff_states(trace.meta.final_state, final_state)
    return ReplayResult(
        trace_id=trace_id,
        consistent=not diff,
        diff=diff,
        final_state=final_state,
        expected_final_state=trace.meta.final_state,
        llm_calls=harness.usage().llm_calls,
        tool_calls=harness.usage().tool_calls,
    )


__all__ = [
    "EVENTS_FILENAME",
    "META_FILENAME",
    "LLMEvent",
    "NoteEvent",
    "ReplayResult",
    "ToolEvent",
    "ToolReplayer",
    "Trace",
    "TraceEvent",
    "TraceMeta",
    "TraceRecorder",
    "diff_states",
    "load_trace",
    "replay",
]
