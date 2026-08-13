"""MockProvider —— CI / 单测的确定性桩（v6 §11.2）。

**零 LLM 调用，不依赖 Ollama、不依赖 GPU。** 与主模型选型正交——无论主模型
是什么，单测都不该依赖一个会飘的外部服务。

两种桩，各管一段：

1. **内容匹配桩**（`MOCK_FIXTURE_DIR/stubs.json`）——「谁问这句话都回这个」，
   适合意图路由这类与调用次序无关的组件：

   ```json
   {
     "rules": [
       {"when_contains": ["排班", "本周"], "response": "{\\"intent\\": \\"schedule\\"}"}
     ],
     "default": "MOCK_DEFAULT_RESPONSE"
   }
   ```

   匹配顺序**自上而下，首个命中者胜**——确定性优先于灵活性：同一份输入在任何
   机器上必须给出同一个输出，否则 CI 就不是回归而是抽签。

2. **场景桩**（`register_scenario` / `activate`）——「这次会话第 1 次调用回 A、
   第 2 次回 B」，这是护栏测试（畸形 tool call → 回灌 → 纠正）唯一能用的形态：
   契约重试要的正是「同一段消息在两次调用间给出不同输出」。**场景耗尽即抛**，
   不静默循环最后一条——静默循环会把「少调了一次」伪装成通过。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.core.config import Settings, get_settings
from backend.core.errors import LLMUnavailableError
from backend.llm.types import LLMRequest, LLMResponse, RawToolCall

#: 桩文件缺失时的兜底响应。刻意做成可识别的字面量，让「忘了配桩」在断言里显形。
DEFAULT_RESPONSE = "MOCK_DEFAULT_RESPONSE"


class StubRule(BaseModel):
    """一条桩规则。`when_contains` 全部命中才算匹配（AND 语义）。"""

    model_config = ConfigDict(extra="forbid")

    when_contains: list[str] = Field(default_factory=list)
    response: str


class StubFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[StubRule] = Field(default_factory=list)
    default: str = DEFAULT_RESPONSE


def text_response(text: str) -> LLMResponse:
    """把一段文本包成 `LLMResponse`（token 数由估算器补，见 `harness.tokens`）。"""
    return LLMResponse(text=text, model="mock")


def tool_response(name: str, arguments: dict[str, Any] | str) -> LLMResponse:
    """构造一次工具调用输出。`arguments` 传 `str` 即可造出 `json_malformed` 场景。"""
    return LLMResponse(
        tool_calls=(RawToolCall(name=name, arguments=arguments),),
        model="mock",
    )


class MockProvider:
    """按场景或内容匹配返回固定响应。全程不发生任何网络调用。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._cfg = settings or get_settings()
        self._stubs = self._load(self._cfg.MOCK_FIXTURE_DIR)
        self._scenarios: dict[str, tuple[LLMResponse, ...]] = {}
        self._active: str | None = None
        self._cursor = 0
        #: 记录本实例被调用过的次数，供护栏测试断言「零 LLM 调用」时区分
        #: 「没调用 provider」与「调用了但没出网」。
        self.call_count = 0

    @staticmethod
    def _load(fixture_dir: Path) -> StubFile:
        path = fixture_dir / "stubs.json"
        if not path.is_file():
            return StubFile()
        return StubFile.model_validate(json.loads(path.read_text(encoding="utf-8")))

    # ── 场景桩 ───────────────────────────────────────────────────────
    def register_scenario(self, name: str, steps: Sequence[LLMResponse | str]) -> None:
        """登记一个按次序回放的场景。重名即覆盖（测试里刻意允许）。"""
        self._scenarios[name] = tuple(
            step if isinstance(step, LLMResponse) else text_response(step) for step in steps
        )

    def activate(self, name: str) -> None:
        """激活场景并把游标归零。"""
        if name not in self._scenarios:
            raise LLMUnavailableError(
                f"未登记的 mock 场景 {name!r}",
                details={"registered": sorted(self._scenarios)},
            )
        self._active = name
        self._cursor = 0

    def deactivate(self) -> None:
        """回到内容匹配桩。"""
        self._active = None
        self._cursor = 0

    @property
    def active_scenario(self) -> str | None:
        return self._active

    @property
    def remaining(self) -> int:
        """当前场景剩余的未消费步数；未激活场景时为 0。"""
        if self._active is None:
            return 0
        return len(self._scenarios[self._active]) - self._cursor

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
        if self._active is not None:
            return self._next_scenario_step()
        haystack = "\n".join(m.get("content", "") for m in request.messages)
        for rule in self._stubs.rules:
            if all(needle in haystack for needle in rule.when_contains):
                return text_response(rule.response)
        return text_response(self._stubs.default)

    def _next_scenario_step(self) -> LLMResponse:
        assert self._active is not None  # 由调用点保证
        steps = self._scenarios[self._active]
        if self._cursor >= len(steps):
            raise LLMUnavailableError(
                f"mock 场景 {self._active!r} 已耗尽：第 {self._cursor + 1} 次调用没有对应桩",
                details={"scenario": self._active, "steps": len(steps)},
                suggestions=["场景步数要与被测流程的调用次数一致；不要靠循环最后一条掩盖差异"],
            )
        step = steps[self._cursor]
        self._cursor += 1
        return step


__all__ = [
    "DEFAULT_RESPONSE",
    "MockProvider",
    "StubFile",
    "StubRule",
    "text_response",
    "tool_response",
]
