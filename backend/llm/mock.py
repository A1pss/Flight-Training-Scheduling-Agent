"""MockProvider —— CI / 单测的确定性桩（v6 §11.2）。

**零 LLM 调用，不依赖 Ollama、不依赖 GPU。** 与主模型选型正交——无论主模型
是什么，单测都不该依赖一个会飘的外部服务。

桩规则从 `MOCK_FIXTURE_DIR/stubs.json` 读取：

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
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.core.config import Settings, get_settings

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


class MockProvider:
    """按内容匹配返回固定响应。全程不发生任何网络调用。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._cfg = settings or get_settings()
        self._stubs = self._load(self._cfg.MOCK_FIXTURE_DIR)
        #: 记录本实例被调用过的次数，供护栏测试断言「零 LLM 调用」时区分
        #: 「没调用 provider」与「调用了但没出网」。
        self.call_count = 0

    @staticmethod
    def _load(fixture_dir: Path) -> StubFile:
        path = fixture_dir / "stubs.json"
        if not path.is_file():
            return StubFile()
        return StubFile.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,  # noqa: ARG002 —— LLMProvider 契约要求
        temperature: float = 0.0,  # noqa: ARG002 —— 桩响应与温度无关，刻意忽略
    ) -> str:
        self.call_count += 1
        haystack = "\n".join(m.get("content", "") for m in messages)
        for rule in self._stubs.rules:
            if all(needle in haystack for needle in rule.when_contains):
                return rule.response
        return self._stubs.default


__all__ = ["DEFAULT_RESPONSE", "MockProvider", "StubFile", "StubRule"]
