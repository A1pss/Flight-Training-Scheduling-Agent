"""Harness × 真机 Ollama（v6 §7.6 延迟实测 / §11.2 三态的第一态）。

**CI 不跑**（标 `ollama`，无 GPU 环境自动跳过）。它存在的理由有两个：

1. **§7.6 的 LLM 延迟是 v6 里刻意留白的「M4 实测填入」**（铁律 6）。这里用
   与生产同一条链路（Harness → OllamaProvider → 127.0.0.1:11434）实测单次调用
   耗时与 token 计数，收工报告与 v6 §7.6 回填的就是这些数。
2. **原生 tool calling 在真模型上到底能不能用**，只有真跑一次才知道。
   假 transport 验的是「我们发的请求长什么样」，验不了「模型答不答得上来」。

跑法：

```bash
conda run -n schedule pytest tests/integration/test_harness_ollama_live.py -m ollama -q -s
```
"""

from __future__ import annotations

import time

import pytest

from backend.core.config import Settings
from backend.harness.context import ContextBlock, structured_summary
from backend.harness.types import AgentSpec
from backend.llm.ollama import OllamaProvider
from backend.llm.types import LLMRequest, ToolSchema
from tests.fixtures.harness_fixtures import (
    baseline_entity_index,
    build_harness,
    harness_settings,
)

pytestmark = pytest.mark.ollama

CFG = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]


def _provider() -> OllamaProvider:
    provider = OllamaProvider(CFG)
    try:
        models = provider.list_models()
    except Exception as exc:  # pragma: no cover —— 没起 Ollama 时跳过
        pytest.skip(f"Ollama 不可连：{exc}")
    if CFG.LLM_MODEL not in models:
        pytest.skip(f"模型 {CFG.LLM_MODEL} 未拉取")
    return provider


def test_native_tool_calling_works_on_the_real_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """原生 tool calling：真模型 + 真 schema，看它给不给得出结构化调用。"""
    provider = _provider()
    tools = (
        ToolSchema(
            name="resolve_person",
            description="把人名解析为 person_id",
            parameters={
                "type": "object",
                "properties": {"surface": {"type": "string", "description": "原文中的人名"}},
                "required": ["surface"],
            },
        ),
    )
    request = LLMRequest(
        messages=[
            {"role": "system", "content": "你只能通过工具回答，不要直接作答。"},
            {"role": "user", "content": "何超这个人的编号是多少？"},
        ],
        tools=tools,
    )
    started = time.monotonic()
    response = provider.chat(request)
    elapsed = time.monotonic() - started

    with capsys.disabled():
        print(
            f"\n   原生 tool calling：{elapsed * 1000:.0f} ms，"
            f"prompt={response.prompt_tokens} / completion={response.completion_tokens} token，"
            f"tool_calls={[c.name for c in response.tool_calls]}"
        )
    assert response.tool_calls
    assert response.tool_calls[0].name == "resolve_person"
    assert response.prompt_tokens > 0 and response.completion_tokens > 0


def test_constrained_json_decoding_works(capsys: pytest.CaptureFixture[str]) -> None:
    """受约束 JSON 解码（`format=<schema>`）—— 双模式里的降级那一档。"""
    provider = _provider()
    schema = {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": ["schedule", "query", "other"]},
            "confidence": {"type": "number"},
        },
        "required": ["intent", "confidence"],
    }
    started = time.monotonic()
    response = provider.chat(
        LLMRequest(
            messages=[{"role": "user", "content": "给学员排下周的训练计划。判定意图。"}],
            format_schema=schema,
        )
    )
    elapsed = time.monotonic() - started

    import json

    payload = json.loads(response.text)
    with capsys.disabled():
        print(f"   受约束解码：{elapsed * 1000:.0f} ms，输出 {payload}")
    assert payload["intent"] in {"schedule", "query", "other"}


def test_logprobs_are_absent_on_this_ollama_build(capsys: pytest.CaptureFixture[str]) -> None:
    """**如实记录环境能力**：0.6.8 不返回 logprobs（M4-B 的置信度校准要据此改口径）。

    这条不是「期望它没有」，而是把现状钉住：哪天换了支持 logprobs 的 Ollama，
    这条会红，届时正好回去把 §7.3.5 的校准特征打开。
    """
    provider = _provider()
    response = provider.chat(
        LLMRequest(
            messages=[{"role": "user", "content": "用一个词回答：飞行训练排班的核心是什么？"}],
            logprobs=True,
            top_logprobs=3,
        )
    )
    with capsys.disabled():
        print(
            f"   logprobs：sequence={response.sequence_logprob}，"
            f"token 数={len(response.token_logprobs)}（Ollama {CFG.LLM_MODEL} 不返回该字段）"
        )
    assert response.sequence_logprob is None


def test_end_to_end_harness_call_latency(capsys: pytest.CaptureFixture[str]) -> None:
    """§7.6「M4 实测填入」的取数用例：整条 Harness 链路的单次调用延迟。

    量的是**生产链路**：上下文装配 → 工具 schema 导出 → 真机调用 → 契约校验 →
    工具执行。不是裸 `POST /api/chat`——那个数好看但没用。
    """
    _provider()
    cfg = harness_settings(LLM_PROVIDER="ollama")
    harness, _, _ = build_harness(
        settings=cfg,
        entity_index=baseline_entity_index(),
    )
    harness._provider = OllamaProvider(cfg)

    agent = AgentSpec(name="route", tools=("resolve_person", "resolve_week"))
    blocks = [
        ContextBlock(
            kind="summary",
            content=structured_summary(
                "基准周快照",
                {
                    "人员": ["P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08"],
                    "飞机": ["AC10", "AC27", "AC34", "AC49", "AC61", "AC73", "AC84", "AC95"],
                    "周次": "2026W02",
                },
            ),
        ),
        ContextBlock(kind="history", content="用户：何超这个人的编号是多少？"),
    ]

    started = time.monotonic()
    out = harness.call(agent, blocks)
    elapsed = time.monotonic() - started
    usage = harness.usage()

    with capsys.disabled():
        print("\n── Harness × 真机 Ollama 端到端（v6 §7.6 实测取数）──")
        print(f"   模型：{cfg.LLM_MODEL}（num_ctx={cfg.LLM_NUM_CTX}）")
        print(f"   墙钟：{elapsed:.2f} s，LLM 请求 {out.llm_calls} 次（含契约重试）")
        print(f"   token：{usage.tokens}（实测计数，tokens_estimated={usage.tokens_estimated}）")
        print(f"   结果：degraded={out.degraded}，calls={[c.name for c in out.calls]}")
        for attempt in out.attempts:
            print(
                f"     尝试 {attempt.attempt + 1}（{attempt.mode}）："
                f"{'通过' if attempt.passed else [f.mode.value for f in attempt.failures]}"
            )

    assert out.llm_calls >= 1
    assert usage.tokens_estimated is False  # 真机有实测 token 计数
    assert elapsed < CFG.HARNESS_WALL_CLOCK_S
