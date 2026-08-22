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
from pathlib import Path
from typing import Any, cast

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


__all__ = ["RecordingProvider"]
