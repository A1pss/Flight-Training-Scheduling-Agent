"""三态 Provider 与零代码切换的落点（v6 §11.2）。

**三态，不是两态**：

- ``ollama``  开发期 = 上线期，同一模型、同一 tag、同一 digest
- ``mock``    CI / 单测：固定桩，**零 LLM 调用，不依赖 Ollama、不依赖 GPU**
- ``replay``  批量回归：读 trace 录制，**零 LLM 调用**

为什么开发期不用小模型：提示词与 few-shot、置信度校准器的 ECE、Harness 的
`mode_selector` 阈值三者全是**模型相关产物**，换模型上线意味着整套重新标定，
§12 的实验数字要重跑一遍才算数。统一模型把这块风险直接删掉（v6 §11.2）。

契约有两层：`chat(LLMRequest) -> LLMResponse` 是全量形态（工具、token 计数、
logprobs），`complete()` 是它的薄封装、保留 M0 冻结的签名。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol, runtime_checkable

from backend.core.config import Settings, get_settings
from backend.llm.types import LLMRequest, LLMResponse, ToolSchema


@runtime_checkable
class LLMProvider(Protocol):
    """v6 §11.2 的 Provider 契约。三个实现都必须真能用。"""

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> str:
        """返回模型的文本输出。`schema` 非空时要求受约束解码为该 JSON Schema。"""
        ...

    def chat(self, request: LLMRequest) -> LLMResponse:
        """全量调用：工具、token 计数、logprobs 一并返回。"""
        ...


def request_key(
    messages: list[dict[str, str]],
    schema: dict[str, Any] | None = None,
    temperature: float = 0.0,
    tools: tuple[ToolSchema, ...] = (),
) -> str:
    """请求的规范化指纹，供 mock 与 replay 做确定性查表。

    **字典按键排序、不含时间戳**——铁律 9 的可复现性要求，任何未固定的
    字典序或时间戳进哈希都会让重放失配。
    """
    return request_fingerprint(
        LLMRequest(
            messages=messages,
            format_schema=schema,
            temperature=temperature,
            tools=tools,
        )
    )


def request_fingerprint(request: LLMRequest) -> str:
    """`LLMRequest` 的规范化指纹。"""
    payload = json.dumps(
        request.canonical(),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_provider(cfg: Settings | None = None) -> LLMProvider:
    """三态工厂（v6 §11.2）。

    延迟 import 三个实现，避免 `LLM_PROVIDER=mock` 的 CI 环境仍需加载
    Ollama 相关代码路径。
    """
    settings = cfg or get_settings()

    if settings.LLM_PROVIDER == "ollama":
        from backend.llm.ollama import OllamaProvider

        return OllamaProvider(settings)
    if settings.LLM_PROVIDER == "mock":
        from backend.llm.mock import MockProvider

        return MockProvider(settings)
    if settings.LLM_PROVIDER == "replay":
        from backend.llm.replay import ReplayProvider

        return ReplayProvider(settings)

    raise ValueError(f"未知的 LLM_PROVIDER: {settings.LLM_PROVIDER!r}")


__all__ = [
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "build_provider",
    "request_fingerprint",
    "request_key",
]
