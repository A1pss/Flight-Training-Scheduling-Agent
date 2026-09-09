"""录制 Provider：把真机往返写成 `ReplayProvider` 能吃的轨迹。

v6 §7.7 的录制重放底座**只交付了重放一侧** —— `backend/llm/replay.py` 有
`ReplayProvider` 与 `record_entry()`，但没有「包住真 Provider、边跑边录」的那一层。
没有它，`traces/` 永远是空的，于是 §12.5.2 的重放一致性与 §12.6 的轨迹评估
都没有输入。本模块补上这一层。

**为什么放在 experiments 而不是 backend/llm**：录制只服务于评测与验收，
生产链路不需要它。放进 `llm/` 会让每个部署都带着一个能往磁盘写全部提示词的
开关，那是不必要的暴露面。

写出来的每一行与 `record_entry()` 的形状一致，但**存的是完整
`LLMResponse`** 而不是纯文本 —— 工具调用、token 计数都要能原样重放，
只存 text 会让带工具的那几个组件重放不出来。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.core.errors import LLMUnavailableError
from backend.harness.recorder import ToolEvent, ToolReplayer
from backend.harness.types import ToolResult
from backend.llm.provider import request_fingerprint
from backend.llm.types import LLMRequest, LLMResponse


class RecordingProvider:
    """包住一个真 Provider，逐次把往返落盘。

    实现 `LLMProvider` 协议（`complete` + `chat`），所以任何接受 Provider 的
    地方都能直接换上它。
    """

    def __init__(self, inner: Any, trace_path: Path) -> None:
        self._inner = inner
        self._path = trace_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.call_count = 0

    def _write(self, request: LLMRequest, response: LLMResponse) -> None:
        line = json.dumps(
            {
                "kind": "llm",
                "request_key": request_fingerprint(request),
                "request": request.model_dump(mode="json"),
                "response": response.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # ── LLMProvider 契约 ─────────────────────────────────────────────
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> str:
        request = LLMRequest(messages=messages, format_schema=schema, temperature=temperature)
        response = self.chat(request)
        return response.text

    def chat(self, request: LLMRequest) -> LLMResponse:
        response = cast(LLMResponse, self._inner.chat(request))
        self.call_count += 1
        self._write(request, response)
        return response


class RecordingToolStream:
    """把 Harness 工具返回写进与 LLM 往返相同的 JSONL。

    `ReplayProvider` 会跳过 ``kind=tool``，而 :class:`ToolReplayer` 只读取这些行；
    同一文件因此能冻结一次轨迹的模型响应与外部世界，不再让 replay 重新碰真库或
    真实求解探针。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._seq = 0

    def write(
        self,
        component: str,
        tool: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> None:
        event = ToolEvent(
            seq=self._seq,
            component=component,
            tool=tool,
            arguments=dict(arguments),
            ok=result.ok,
            value=result.value,
            error=result.error,
            cached=result.cached,
        )
        self._seq += 1
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
            )


def load_tool_replayer(path: Path) -> ToolReplayer:
    """从实验轨迹文件装载按序工具返回；旧轨迹没有工具行时得到空重放器。"""
    events: list[ToolEvent] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            if isinstance(raw, dict) and raw.get("kind") == "tool":
                events.append(ToolEvent.model_validate(raw))
    return ToolReplayer(events)


class CheckpointEvent(BaseModel):
    """实验轨迹中的一个可重放内部边界。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["checkpoint"] = "checkpoint"
    seq: int = Field(ge=0)
    name: str = Field(min_length=1)
    payload: Any


class RecordingCheckpointStream:
    """把 JSON 可序列化的内部边界值追加到混合轨迹 JSONL。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def write(self, name: str, payload: Any) -> CheckpointEvent:
        event = CheckpointEvent(seq=self._seq, name=name, payload=payload)
        try:
            line = json.dumps(
                event.model_dump(mode="python"),
                ensure_ascii=False,
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise TypeError(f"checkpoint {name!r} 的 payload 不能 JSON 序列化") from exc
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._seq += 1
        return event


class CheckpointReplayer:
    """严格按录制序号与名称消费 checkpoint。"""

    def __init__(self, events: Sequence[CheckpointEvent]) -> None:
        self._events = tuple(events)
        self._cursor = 0
        for expected_seq, event in enumerate(self._events):
            if event.seq != expected_seq:
                raise LLMUnavailableError(
                    "重放：checkpoint 录制序号不连续",
                    details={
                        "position": expected_seq,
                        "expected_seq": expected_seq,
                        "actual_seq": event.seq,
                        "name": event.name,
                    },
                )

    @property
    def remaining(self) -> int:
        return len(self._events) - self._cursor

    def next_checkpoint(self, name: str) -> CheckpointEvent:
        if self._cursor >= len(self._events):
            raise LLMUnavailableError(
                f"重放：第 {self._cursor + 1} 个 checkpoint（{name}）没有对应录制",
                details={
                    "name": name,
                    "recorded_checkpoints": len(self._events),
                },
            )
        event = self._events[self._cursor]
        if event.seq != self._cursor or event.name != name:
            raise LLMUnavailableError(
                f"重放：第 {self._cursor + 1} 个 checkpoint 与录制不符",
                details={
                    "position": self._cursor,
                    "expected_seq": event.seq,
                    "actual_seq": self._cursor,
                    "expected_name": event.name,
                    "actual_name": name,
                },
            )
        self._cursor += 1
        return event

    def next_payload(self, name: str) -> Any:
        return self.next_checkpoint(name).payload


def load_checkpoint_replayer(path: Path) -> CheckpointReplayer:
    """从混合实验轨迹中读取 checkpoint，并拒绝畸形 checkpoint 行。"""
    events: list[CheckpointEvent] = []
    if path.is_file():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict) or raw.get("kind") != "checkpoint":
                continue
            try:
                events.append(CheckpointEvent.model_validate(raw))
            except ValidationError as exc:
                raise LLMUnavailableError(
                    f"重放：第 {lineno} 行 checkpoint 格式无效",
                    details={"path": str(path), "line": lineno, "errors": exc.errors()},
                ) from exc
    return CheckpointReplayer(events)


__all__ = [
    "CheckpointEvent",
    "CheckpointReplayer",
    "RecordingCheckpointStream",
    "RecordingProvider",
    "RecordingToolStream",
    "load_checkpoint_replayer",
    "load_tool_replayer",
]
