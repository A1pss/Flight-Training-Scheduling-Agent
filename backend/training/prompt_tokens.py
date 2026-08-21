"""提示词 token 的**实测**（§15.4 门禁「较基线下降 ≥30%」的量具）。

## 为什么不能用 `harness.tokens.estimate_tokens`

那个模块自己的 docstring 写着：「**估算用于事前拦截，实测用于事后记账**……
报告里出现的 token 数一律取实测（铁律 6）」。§15.4 是准入门禁，只能用实测。

## 怎么实测「系统提示词 + 工具 schema」这一段

Ollama 只在响应里给一个 `prompt_eval_count`（整条请求的提示词 token 数），
没有分项。所以用**三次差分**把两段拆出来，每次只动一个变量：

```
A  system=正文   tools=有   user=探针   → pe_full
B  system=" "    tools=有   user=探针   → pe_nosys
C  system=" "    tools=无   user=探针   → pe_notools

system 提示词 tokens = pe_full   − pe_nosys
工具 schema  tokens = pe_nosys  − pe_notools
门禁口径的合计       = pe_full   − pe_notools
```

**基线用一个空格而不是「不给 system」**：Qwen2.5 的聊天模板在没有 system 消息时
会自己塞一句默认的「You are Qwen, created by Alibaba Cloud…」，那一句会混进差值里，
把 system 提示词的实测值系统性压低几十个 token。给一个空格，模板走的是同一条
分支，差出来的才是正文本身。

**前缀缓存不影响这个数**（M7 实测：同一条请求连发两次，`prompt_eval_count`
两次都是 998，不因命中缓存而变小）。差分法因此成立；换推理端之后要重验这一条。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict

from backend.harness.acl import DEFAULT_ACL
from backend.harness.registry import ToolRegistry
from backend.harness.types import ALL_COMPONENTS, ComponentName
from backend.llm.provider import LLMProvider
from backend.llm.types import LLMRequest
from backend.training.prompt_configs import PromptConfigName, registry_for

#: 三次差分共用的探针用户消息。内容无所谓，**三次必须逐字一致**。
PROBE_USER: Final[str] = "【任务】测量提示词长度\n【已知条件】（无）"

#: 「空 system」的基线取值 —— 用一个空格走同一条模板分支，见模块 docstring。
BLANK_SYSTEM: Final[str] = " "


class PromptTokenMeasurement(BaseModel):
    """一个 (配置, 组件) 的实测提示词 token。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: str
    component: str
    #: 三次探针的原始 `prompt_eval_count`，**原样留着** —— 差值算错时要能回溯
    pe_full: int
    pe_nosys: int
    pe_notools: int

    @property
    def system_tokens(self) -> int:
        return self.pe_full - self.pe_nosys

    @property
    def schema_tokens(self) -> int:
        return self.pe_nosys - self.pe_notools

    @property
    def gate_tokens(self) -> int:
        """§15.4 门禁口径：system 提示词 + 工具 schema 合计。"""
        return self.pe_full - self.pe_notools


def measure_component(
    provider: LLMProvider,
    config: PromptConfigName,
    component: ComponentName,
    *,
    registry: ToolRegistry | None = None,
) -> PromptTokenMeasurement:
    """对一个组件做三次差分探针。"""
    tools = tuple(sorted(DEFAULT_ACL.allowed_tools(component)))
    schemas = (registry or ToolRegistry()).schemas_for(tools)
    body = registry_for(config).get(component).body

    def probe(system: str, with_tools: bool) -> int:
        request = LLMRequest(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": PROBE_USER},
            ],
            tools=schemas if with_tools else (),
            temperature=0.0,
        )
        return provider.chat(request).prompt_tokens

    return PromptTokenMeasurement(
        config=config,
        component=component,
        pe_full=probe(body, True),
        pe_nosys=probe(BLANK_SYSTEM, True),
        pe_notools=probe(BLANK_SYSTEM, False),
    )


def measure_config(
    provider: LLMProvider,
    config: PromptConfigName,
    *,
    components: Sequence[ComponentName] = ALL_COMPONENTS,
) -> tuple[PromptTokenMeasurement, ...]:
    """一种配置下六个组件的实测值。"""
    return tuple(measure_component(provider, config, c) for c in components)


def weighted_gate_tokens(
    measurements: Sequence[PromptTokenMeasurement],
    weights: dict[str, int],
) -> float:
    """按组件权重加权的门禁 token 数。

    权重取 `tool_calls_200` 的 200 条 valid 在各组件上的**实际条数**——
    §15.4 的门禁是给「工具调用这件事」立的，用均权会让只有 9 条的 knowledge
    与 59 条的 diagnosis 一样重，加权后的基线跟着失真。
    """
    total = sum(weights.get(m.component, 0) for m in measurements)
    if total == 0:
        return 0.0
    return round(sum(m.gate_tokens * weights.get(m.component, 0) for m in measurements) / total, 1)


def component_weights(items: Sequence[dict[str, object]]) -> dict[str, int]:
    """从数据集条目里数出各组件的 valid 条数。"""
    weights: dict[str, int] = {}
    for item in items:
        if item.get("stratum") != "valid":
            continue
        component = str(item.get("component", ""))
        weights[component] = weights.get(component, 0) + 1
    return weights


__all__ = [
    "BLANK_SYSTEM",
    "PROBE_USER",
    "PromptTokenMeasurement",
    "component_weights",
    "measure_component",
    "measure_config",
    "weighted_gate_tokens",
]
