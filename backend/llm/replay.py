"""ReplayProvider —— 读录制轨迹重放（v6 §11.2 / §7.7.4 / §12.5.2）。

**零 LLM 调用。** 真正承担「开发期速度」职责的就是它：改业务逻辑、改图结构、
改校验器时跑 `replay(trace_id)`，一次 Ollama 请求都不发；只有改提示词、改工具
schema 时才需要真机重跑该组件的 eval 子集。

**查不到就抛，绝不回退到真机调用。** v6 §12.5.2 要求重放一致率 100%、
实际 Ollama 请求数必须为 0——一旦允许「找不到就去问模型」，这两条断言就都
失去意义，而且失配会被悄悄掩盖成「跑通了」。

录制格式：`REPLAY_TRACE_DIR` 下的 `*.jsonl`，每行一条
``{"request_key": "<sha256>", "response": "<text>"}``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.core.config import Settings, get_settings
from backend.core.errors import LLMUnavailableError
from backend.llm.provider import request_key


class ReplayProvider:
    """按请求指纹查表返回录制过的响应。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._cfg = settings or get_settings()
        self._trace_dir = self._cfg.REPLAY_TRACE_DIR
        self._table: dict[str, str] = self._load(self._trace_dir)
        self.call_count = 0

    @staticmethod
    def _load(trace_dir: Path) -> dict[str, str]:
        table: dict[str, str] = {}
        if not trace_dir.is_dir():
            return table
        # 文件名排序，保证同一目录在任何机器上装载顺序一致（铁律 9）
        for path in sorted(trace_dir.glob("*.jsonl")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LLMUnavailableError(
                        f"录制轨迹 {path.name}:{lineno} 不是合法 JSON：{exc}",
                        details={"path": str(path), "line": lineno},
                    ) from exc
                key = record.get("request_key")
                if not isinstance(key, str) or "response" not in record:
                    raise LLMUnavailableError(
                        f"录制轨迹 {path.name}:{lineno} 缺少 request_key 或 response",
                        details={"path": str(path), "line": lineno},
                    )
                table[key] = str(record["response"])
        return table

    @property
    def size(self) -> int:
        """已装载的录制条目数。"""
        return len(self._table)

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> str:
        self.call_count += 1
        key = request_key(messages, schema, temperature)
        if key not in self._table:
            raise LLMUnavailableError(
                "重放轨迹中不存在该请求，拒绝回退到真机调用",
                details={
                    "request_key": key,
                    "trace_dir": str(self._trace_dir),
                    "loaded_entries": len(self._table),
                },
                suggestions=[
                    "先用 LLM_PROVIDER=ollama 录制该请求，再切回 replay",
                    "重放必须零 LLM 调用（v6 §12.5.2），不允许静默回退",
                ],
            )
        return self._table[key]


def record_entry(
    messages: list[dict[str, str]],
    response: str,
    *,
    schema: dict[str, Any] | None = None,
    temperature: float = 0.0,
) -> str:
    """把一次真机调用序列化为一行 JSONL，供录制端写入。"""
    return json.dumps(
        {
            "request_key": request_key(messages, schema, temperature),
            "response": response,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


__all__ = ["ReplayProvider", "record_entry"]
