"""LLM 请求 / 响应契约（v6 §11.2 的完整形态）。

M0 给 `LLMProvider` 冻结的 `complete(messages, schema, temperature) -> str`
只够做「一次纯文本补全」。Harness（v6 §7.7）还需要三样东西：

1. **原生 tool calling** —— 双模式调用的「主用」那一模式（§7.7.1 第 2 行）；
2. **真实 token 计数** —— 预算控制的 `token ≤40k` 必须是实测而非估算（铁律 6）；
3. **logprobs** —— M4-B 的置信度校准（§7.3.5）要用序列 logprob 做特征。

所以这里补一层结构化契约，`complete()` 保留为 `chat()` 的薄封装：**旧调用点
一行都不用改**，新调用点拿得到 tool_calls 与 token 计数。

> ⚠️ **logprobs 在本机 Ollama 0.6.8 上不可得**（实测：`/api/chat` 响应里只有
> `prompt_eval_count` / `eval_count`，没有任何 logprob 字段）。请求侧的开关与
> 响应侧的解析都已实现，装上支持该字段的 Ollama 即可生效；在此之前
> `LLMResponse.sequence_logprob` 一律为 `None`，**不许拿别的数去凑**。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 双模式调用的两种模式（v6 §7.7.1 第 2 行）。由 `mode_selector` 的运行时统计
#: 决定用哪个，**不写死在配置里**。
CallMode = Literal["native", "constrained_json"]


class ToolSchema(BaseModel):
    """交给模型的单个工具声明（Pydantic 入参模型导出的 JSON Schema）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)

    def to_ollama(self) -> dict[str, Any]:
        """转成 Ollama `/api/chat` 的 `tools[]` 元素。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class RawToolCall(BaseModel):
    """模型返回的**未经校验**的工具调用。

    `arguments` 刻意允许 `str`：模型把参数写成一段不合法的 JSON 时，这一层
    必须原样留住，才能在 §12.5.1 的失败模式分类里判成 `json_malformed`
    而不是在解析处静默丢掉。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = ""
    arguments: dict[str, Any] | str = Field(default_factory=dict)

    def decoded_arguments(self) -> tuple[dict[str, Any] | None, str | None]:
        """把 `arguments` 归一为 dict。

        返回 ``(参数, 解析错误)``——两者恰有一个非空。
        """
        if isinstance(self.arguments, dict):
            return self.arguments, None
        try:
            parsed = json.loads(self.arguments)
        except json.JSONDecodeError as exc:
            return None, f"arguments 不是合法 JSON：{exc}"
        if not isinstance(parsed, dict):
            return None, f"arguments 应为 JSON 对象，实际是 {type(parsed).__name__}"
        return parsed, None


class LLMRequest(BaseModel):
    """一次 LLM 调用的完整输入。

    **不含任何时间戳、随机数或调用序号**——它要参与 `request_key` 的指纹计算，
    掺进不确定量重放就永远对不上（铁律 9）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: list[dict[str, str]]
    #: 原生 tool calling 的工具表；`constrained_json` 模式下为空
    tools: tuple[ToolSchema, ...] = ()
    #: 受约束 JSON 解码的目标 schema（Ollama 的 `format` 字段）
    format_schema: dict[str, Any] | None = None
    temperature: float = 0.0
    #: 请求 logprobs（本机 Ollama 0.6.8 不支持，见模块 docstring）
    logprobs: bool = False
    top_logprobs: int | None = None

    def canonical(self) -> dict[str, Any]:
        """指纹用的规范形态：键全排序、无时间戳。"""
        return {
            "messages": self.messages,
            "tools": [t.model_dump(mode="json") for t in self.tools],
            "format_schema": self.format_schema,
            "temperature": self.temperature,
            "logprobs": self.logprobs,
            "top_logprobs": self.top_logprobs,
        }


class LLMResponse(BaseModel):
    """一次 LLM 调用的完整输出。

    **不含墙钟耗时**：耗时由 `TraceRecorder` 单独记在事件上。响应体本身必须
    是「同输入 → 同输出」的确定量，否则重放一致性（§12.5.2）没法逐字段比。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = ""
    tool_calls: tuple[RawToolCall, ...] = ()
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    #: 序列 logprob（M4-B §7.3.5 的校准特征）。取不到时为 None，**不做估算**
    sequence_logprob: float | None = None
    token_logprobs: tuple[float, ...] = ()
    model: str = ""
    finish_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


__all__ = [
    "CallMode",
    "LLMRequest",
    "LLMResponse",
    "RawToolCall",
    "ToolSchema",
]
