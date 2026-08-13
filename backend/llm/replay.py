"""ReplayProvider —— 读录制轨迹重放（v6 §11.2 / §7.7.4 / §12.5.2）。

**零 LLM 调用。** 真正承担「开发期速度」职责的就是它：改业务逻辑、改图结构、
改校验器时跑 `replay(trace_id)`，一次 Ollama 请求都不发；只有改提示词、改工具
schema 时才需要真机重跑该组件的 eval 子集。

**严格按录制次序回放，且逐次核对请求指纹。** 两条一起才有意义：

- 只按指纹查表 → 少调一次、多调一次、把两次调用换个顺序，全都查得到、全都「通过」，
  而这三件事恰恰是重构最容易引入的 bug；
- 只按次序 → 请求内容变了（提示词改了、上下文装配改了）也照样返回老响应，
  重放就成了自欺。

所以 `complete()` 的语义是「取下一条录制，并断言它就是你要问的那个问题」。
指纹对不上即抛 `LLMUnavailableError`，把期望与实际的 `request_key` 一并给出。

**查不到就抛，绝不回退到真机调用。** v6 §12.5.2 要求重放一致率 100%、
实际 Ollama 请求数必须为 0——一旦允许「找不到就去问模型」，这两条断言就都
失去意义，而且失配会被悄悄掩盖成「跑通了」。

录制格式：`REPLAY_TRACE_DIR` 下的 `*.jsonl`，每行一条。两种形态都认：

- Harness 录制的事件行 ``{"kind": "llm", "request_key": ..., "response": {...}}``
  （`response` 是 `LLMResponse` 的 dump，非 llm 事件行自动跳过）
- 精简行 ``{"request_key": "<sha256>", "response": "<text>"}``（`record_entry` 产出）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.core.config import Settings, get_settings
from backend.core.errors import LLMUnavailableError
from backend.llm.provider import request_fingerprint
from backend.llm.types import LLMRequest, LLMResponse


class ReplayEntry(BaseModel):
    """一条录制的 LLM 往返。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_key: str
    response: LLMResponse
    source: str = ""


class ReplayProvider:
    """按录制次序回放，并逐次核对请求指纹。"""

    def __init__(self, settings: Settings | None = None, *, strict_order: bool = True) -> None:
        self._cfg = settings or get_settings()
        self._trace_dir = self._cfg.REPLAY_TRACE_DIR
        self._entries: tuple[ReplayEntry, ...] = self._load(self._trace_dir)
        self._strict_order = strict_order
        self._cursor = 0
        self.call_count = 0

    # ── 装载 ─────────────────────────────────────────────────────────
    @staticmethod
    def _load(trace_dir: Path) -> tuple[ReplayEntry, ...]:
        entries: list[ReplayEntry] = []
        if not trace_dir.is_dir():
            return ()
        # 文件名排序，保证同一目录在任何机器上装载顺序一致（铁律 9）
        for path in sorted(trace_dir.glob("*.jsonl")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                entry = _parse_line(line, path, lineno)
                if entry is not None:
                    entries.append(entry)
        return tuple(entries)

    @property
    def size(self) -> int:
        """已装载的录制条目数。"""
        return len(self._entries)

    @property
    def remaining(self) -> int:
        return len(self._entries) - self._cursor

    def rewind(self) -> None:
        """游标归零，供同一份轨迹连跑两遍（重放一致性要比两次结果）。"""
        self._cursor = 0

    # ── LLMProvider 契约 ─────────────────────────────────────────────
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> str:
        return self.chat(
            LLMRequest(messages=messages, format_schema=schema, temperature=temperature)
        ).text

    def chat(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        key = request_fingerprint(request)
        if self._strict_order:
            return self._next_in_order(key)
        return self._lookup(key)

    def _next_in_order(self, key: str) -> LLMResponse:
        if self._cursor >= len(self._entries):
            raise LLMUnavailableError(
                f"重放轨迹已耗尽：第 {self._cursor + 1} 次调用没有对应录制，拒绝回退到真机调用",
                details={
                    "request_key": key,
                    "trace_dir": str(self._trace_dir),
                    "recorded_calls": len(self._entries),
                },
                suggestions=[
                    "调用次数比录制时多了：先确认是不是多发了一次请求",
                    "确需新增请求就用 LLM_PROVIDER=ollama 重新录制整条轨迹",
                ],
            )
        entry = self._entries[self._cursor]
        if entry.request_key != key:
            raise LLMUnavailableError(
                f"重放轨迹第 {self._cursor + 1} 条的请求指纹不匹配，拒绝回退到真机调用",
                details={
                    "position": self._cursor,
                    "expected_request_key": entry.request_key,
                    "actual_request_key": key,
                    "source": entry.source,
                },
                suggestions=[
                    "提示词、工具 schema 或上下文装配改过了 —— 按 §7.7.1 第 8 条重跑该组件的 eval 子集并重新录制",
                    "调用次序变了：重放要求逐次对齐，不是按指纹查表",
                ],
            )
        self._cursor += 1
        return entry.response

    def _lookup(self, key: str) -> LLMResponse:
        """非严格模式：按指纹查表（最后一条同指纹录制胜出）。"""
        for entry in reversed(self._entries):
            if entry.request_key == key:
                return entry.response
        raise LLMUnavailableError(
            "重放轨迹中不存在该请求，拒绝回退到真机调用",
            details={
                "request_key": key,
                "trace_dir": str(self._trace_dir),
                "loaded_entries": len(self._entries),
            },
            suggestions=[
                "先用 LLM_PROVIDER=ollama 录制该请求，再切回 replay",
                "重放必须零 LLM 调用（v6 §12.5.2），不允许静默回退",
            ],
        )


def _parse_line(line: str, path: Path, lineno: int) -> ReplayEntry | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise LLMUnavailableError(
            f"录制轨迹 {path.name}:{lineno} 不是合法 JSON：{exc}",
            details={"path": str(path), "line": lineno},
        ) from exc

    if not isinstance(record, dict):
        raise LLMUnavailableError(
            f"录制轨迹 {path.name}:{lineno} 不是 JSON 对象",
            details={"path": str(path), "line": lineno},
        )

    kind = record.get("kind")
    if kind is not None and kind != "llm":
        return None  # Harness 轨迹里的工具/备注事件，不归 Provider 管

    key = record.get("request_key")
    if not isinstance(key, str) or "response" not in record:
        raise LLMUnavailableError(
            f"录制轨迹 {path.name}:{lineno} 缺少 request_key 或 response",
            details={"path": str(path), "line": lineno},
        )

    raw = record["response"]
    response = (
        LLMResponse.model_validate(raw) if isinstance(raw, dict) else LLMResponse(text=str(raw))
    )
    return ReplayEntry(request_key=key, response=response, source=f"{path.name}:{lineno}")


def record_entry(
    messages: list[dict[str, str]],
    response: str,
    *,
    schema: dict[str, Any] | None = None,
    temperature: float = 0.0,
) -> str:
    """把一次真机调用序列化为一行 JSONL，供录制端写入。"""
    request = LLMRequest(messages=messages, format_schema=schema, temperature=temperature)
    return json.dumps(
        {
            "request_key": request_fingerprint(request),
            "response": response,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


__all__ = ["ReplayEntry", "ReplayProvider", "record_entry"]
