"""三态 Provider 单测（v6 §11.2）。

出口标准：**mock / replay 零 LLM 调用**（本文件全程不碰网络），
**ollama 标记为 integration 可选跑**（见 `tests/integration/test_ollama_live.py`）。

`OllamaProvider` 的协议行为用一个假 transport 覆盖——既验证了真实的请求
构造与错误分支，又不需要一个会飘的外部服务（CLAUDE.md §11 反模式：
「单元测试依赖真实 Ollama」）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from backend.core.config import Settings, get_settings
from backend.core.errors import LLMSchemaError, LLMUnavailableError
from backend.llm.mock import DEFAULT_RESPONSE, MockProvider, tool_response
from backend.llm.ollama import OllamaProvider, parse_json_output
from backend.llm.provider import (
    LLMProvider,
    build_provider,
    request_fingerprint,
    request_key,
)
from backend.llm.replay import ReplayProvider, record_entry
from backend.llm.types import LLMRequest, ToolSchema

MESSAGES = [{"role": "user", "content": "给何超排班，出本周的训练计划"}]


# ─── 工厂 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "cls"),
    [("mock", MockProvider), ("replay", ReplayProvider), ("ollama", OllamaProvider)],
)
def test_build_provider_tri_state(name: str, cls: type, tmp_path: Path) -> None:
    cfg = Settings(  # type: ignore[call-arg]
        _env_file=None,
        LLM_PROVIDER=name,
        MOCK_FIXTURE_DIR=tmp_path,
        REPLAY_TRACE_DIR=tmp_path,
    )
    provider = build_provider(cfg)
    assert isinstance(provider, cls)
    assert isinstance(provider, LLMProvider)


def test_build_provider_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setattr(cfg, "LLM_PROVIDER", "telepathy", raising=False)
    with pytest.raises(ValueError, match="未知的 LLM_PROVIDER"):
        build_provider(cfg)


def test_build_provider_defaults_to_settings_singleton() -> None:
    get_settings.cache_clear()
    assert isinstance(build_provider(), MockProvider)


# ─── request_key：可复现性（铁律 9）──────────────────────────────────


def test_request_key_is_deterministic() -> None:
    a = request_key(MESSAGES, None, 0.0)
    b = request_key(MESSAGES, None, 0.0)
    assert a == b and len(a) == 64


def test_request_key_ignores_dict_order() -> None:
    m1 = [{"role": "user", "content": "hi"}]
    m2 = [{"content": "hi", "role": "user"}]
    assert request_key(m1, None, 0.0) == request_key(m2, None, 0.0)


def test_request_key_varies_with_inputs() -> None:
    base = request_key(MESSAGES, None, 0.0)
    assert request_key(MESSAGES, None, 0.7) != base
    assert request_key(MESSAGES, {"type": "object"}, 0.0) != base


# ─── MockProvider：零 LLM 调用 ───────────────────────────────────────


def test_mock_returns_default_when_no_stub_file(tmp_path: Path) -> None:
    cfg = Settings(_env_file=None, MOCK_FIXTURE_DIR=tmp_path)  # type: ignore[call-arg]
    provider = MockProvider(cfg)
    assert provider.complete(MESSAGES) == DEFAULT_RESPONSE
    assert provider.call_count == 1


def test_mock_matches_rule_and_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "stubs.json").write_text(
        json.dumps(
            {
                "rules": [
                    {"when_contains": ["排班", "何超"], "response": '{"intent":"schedule"}'},
                    {"when_contains": ["查询"], "response": '{"intent":"query"}'},
                ],
                "default": "FALLBACK",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = Settings(_env_file=None, MOCK_FIXTURE_DIR=tmp_path)  # type: ignore[call-arg]
    p = MockProvider(cfg)
    assert p.complete(MESSAGES) == '{"intent":"schedule"}'
    assert p.complete(MESSAGES) == '{"intent":"schedule"}'  # 确定性
    assert p.complete([{"role": "user", "content": "查询课目"}]) == '{"intent":"query"}'
    assert p.complete([{"role": "user", "content": "别的"}]) == "FALLBACK"


def test_mock_first_matching_rule_wins(tmp_path: Path) -> None:
    (tmp_path / "stubs.json").write_text(
        json.dumps(
            {
                "rules": [
                    {"when_contains": ["排班"], "response": "A"},
                    {"when_contains": ["排班"], "response": "B"},
                ]
            }
        ),
        encoding="utf-8",
    )
    cfg = Settings(_env_file=None, MOCK_FIXTURE_DIR=tmp_path)  # type: ignore[call-arg]
    assert MockProvider(cfg).complete(MESSAGES) == "A"


# ─── ReplayProvider：零 LLM 调用，查不到就抛 ─────────────────────────


def test_replay_returns_recorded_response(tmp_path: Path) -> None:
    (tmp_path / "run1.jsonl").write_text(
        record_entry(MESSAGES, '{"intent":"schedule"}') + "\n", encoding="utf-8"
    )
    cfg = Settings(_env_file=None, REPLAY_TRACE_DIR=tmp_path)  # type: ignore[call-arg]
    p = ReplayProvider(cfg)
    assert p.size == 1
    assert p.complete(MESSAGES) == '{"intent":"schedule"}'


def test_replay_raises_on_miss_never_falls_back(tmp_path: Path) -> None:
    """§12.5.2 要求重放零 LLM 调用——查不到必须抛，绝不静默回退到真机。"""
    cfg = Settings(_env_file=None, REPLAY_TRACE_DIR=tmp_path)  # type: ignore[call-arg]
    with pytest.raises(LLMUnavailableError, match="拒绝回退到真机调用"):
        ReplayProvider(cfg).complete(MESSAGES)


def test_replay_missing_dir_is_empty(tmp_path: Path) -> None:
    cfg = Settings(_env_file=None, REPLAY_TRACE_DIR=tmp_path / "nope")  # type: ignore[call-arg]
    assert ReplayProvider(cfg).size == 0


def test_replay_rejects_malformed_line(tmp_path: Path) -> None:
    (tmp_path / "bad.jsonl").write_text("{not json}\n", encoding="utf-8")
    cfg = Settings(_env_file=None, REPLAY_TRACE_DIR=tmp_path)  # type: ignore[call-arg]
    with pytest.raises(LLMUnavailableError, match="不是合法 JSON"):
        ReplayProvider(cfg)


def test_replay_rejects_missing_keys(tmp_path: Path) -> None:
    (tmp_path / "bad.jsonl").write_text('{"response": "x"}\n', encoding="utf-8")
    cfg = Settings(_env_file=None, REPLAY_TRACE_DIR=tmp_path)  # type: ignore[call-arg]
    with pytest.raises(LLMUnavailableError, match="缺少 request_key"):
        ReplayProvider(cfg)


def test_replay_load_order_is_stable(tmp_path: Path) -> None:
    """多文件按文件名排序装载，保证任何机器上结果一致（铁律 9）。"""
    key = request_key(MESSAGES, None, 0.0)
    (tmp_path / "b.jsonl").write_text(
        json.dumps({"request_key": key, "response": "second"}) + "\n", encoding="utf-8"
    )
    (tmp_path / "a.jsonl").write_text(
        json.dumps({"request_key": key, "response": "first"}) + "\n", encoding="utf-8"
    )
    cfg = Settings(_env_file=None, REPLAY_TRACE_DIR=tmp_path)  # type: ignore[call-arg]
    provider = ReplayProvider(cfg)
    # a 先装载 → 严格按次序回放时先出 "first"
    assert provider.complete(MESSAGES) == "first"
    assert provider.complete(MESSAGES) == "second"


def test_replay_is_strictly_ordered(tmp_path: Path) -> None:
    """M4-A 改动：按次序回放 + 逐次核对指纹。

    只按指纹查表的话，「少调一次 / 多调一次 / 两次调用换个顺序」全都查得到、
    全都「通过」——而这三件事恰恰是重构最容易引入的 bug（v6 §12.5.2）。
    """
    first = [{"role": "user", "content": "第一问"}]
    second = [{"role": "user", "content": "第二问"}]
    (tmp_path / "run.jsonl").write_text(
        record_entry(first, "A") + "\n" + record_entry(second, "B") + "\n", encoding="utf-8"
    )
    cfg = Settings(_env_file=None, REPLAY_TRACE_DIR=tmp_path)  # type: ignore[call-arg]

    provider = ReplayProvider(cfg)
    assert provider.complete(first) == "A"
    assert provider.complete(second) == "B"
    assert provider.remaining == 0

    # 换个顺序问 → 指纹对不上，抛
    out_of_order = ReplayProvider(cfg)
    with pytest.raises(LLMUnavailableError, match="请求指纹不匹配"):
        out_of_order.complete(second)


def test_replay_exhaustion_is_an_error_not_a_fallback(tmp_path: Path) -> None:
    (tmp_path / "run.jsonl").write_text(record_entry(MESSAGES, "A") + "\n", encoding="utf-8")
    cfg = Settings(_env_file=None, REPLAY_TRACE_DIR=tmp_path)  # type: ignore[call-arg]
    provider = ReplayProvider(cfg)
    provider.complete(MESSAGES)
    with pytest.raises(LLMUnavailableError, match="已耗尽"):
        provider.complete(MESSAGES)


def test_replay_rewind(tmp_path: Path) -> None:
    """同一份轨迹连跑两遍——重放一致性要比两次结果。"""
    (tmp_path / "run.jsonl").write_text(record_entry(MESSAGES, "A") + "\n", encoding="utf-8")
    cfg = Settings(_env_file=None, REPLAY_TRACE_DIR=tmp_path)  # type: ignore[call-arg]
    provider = ReplayProvider(cfg)
    assert provider.complete(MESSAGES) == "A"
    provider.rewind()
    assert provider.complete(MESSAGES) == "A"


def test_replay_skips_non_llm_trace_events(tmp_path: Path) -> None:
    """Harness 轨迹里还有 tool / note 事件，Provider 只认 llm 那些行。"""
    lines = [
        json.dumps({"kind": "tool", "seq": 0, "component": "route", "tool": "resolve_person"}),
        json.dumps(
            {
                "kind": "llm",
                "request_key": request_key(MESSAGES, None, 0.0),
                "response": {"text": "从轨迹来的"},
            }
        ),
    ]
    (tmp_path / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cfg = Settings(_env_file=None, REPLAY_TRACE_DIR=tmp_path)  # type: ignore[call-arg]
    provider = ReplayProvider(cfg)
    assert provider.size == 1
    assert provider.complete(MESSAGES) == "从轨迹来的"


def test_replay_non_strict_mode_falls_back_to_lookup(tmp_path: Path) -> None:
    """非严格模式仍可用（按指纹查表），但**不是默认**。"""
    (tmp_path / "run.jsonl").write_text(
        record_entry([{"role": "user", "content": "x"}], "X")
        + "\n"
        + record_entry(MESSAGES, "Y")
        + "\n",
        encoding="utf-8",
    )
    cfg = Settings(_env_file=None, REPLAY_TRACE_DIR=tmp_path)  # type: ignore[call-arg]
    provider = ReplayProvider(cfg, strict_order=False)
    assert provider.complete(MESSAGES) == "Y"


# ─── OllamaProvider：用假 transport 覆盖协议行为，不碰真实服务 ───────


class _FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.text = json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body

    def json(self) -> Any:
        return self._body


class _FakeClient:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response
        self.last_payload: dict[str, Any] | None = None

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def post(self, _path: str, json: dict[str, Any] | None = None) -> _FakeResponse:
        self.last_payload = json
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    def get(self, _path: str) -> _FakeResponse:
        assert not isinstance(self._response, Exception)
        return self._response


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr("backend.llm.ollama.build_client", lambda *_a, **_k: client)


def test_ollama_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(_FakeResponse(200, {"message": {"content": "你好"}}))
    _patch_client(monkeypatch, client)
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    assert OllamaProvider(cfg).complete(MESSAGES) == "你好"
    assert client.last_payload is not None
    assert client.last_payload["model"] == cfg.LLM_MODEL
    assert client.last_payload["stream"] is False
    assert client.last_payload["options"]["seed"] == 42  # 可复现性


def test_ollama_passes_schema_for_constrained_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(_FakeResponse(200, {"message": {"content": "{}"}}))
    _patch_client(monkeypatch, client)
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    schema = {"type": "object", "properties": {"intent": {"type": "string"}}}
    OllamaProvider(cfg).complete(MESSAGES, schema=schema)
    assert client.last_payload is not None
    assert client.last_payload["format"] == schema


def test_ollama_network_error_becomes_fts_4001(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeClient(OSError("connection refused")))
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    with pytest.raises(LLMUnavailableError, match="Ollama 调用失败"):
        OllamaProvider(cfg).complete(MESSAGES)


def test_ollama_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeClient(_FakeResponse(500, {"error": "boom"})))
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    with pytest.raises(LLMUnavailableError, match="HTTP 500"):
        OllamaProvider(cfg).complete(MESSAGES)


def test_ollama_malformed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeClient(_FakeResponse(200, {"nope": 1})))
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    with pytest.raises(LLMSchemaError, match=r"缺少 message\.content"):
        OllamaProvider(cfg).complete(MESSAGES)


def test_ollama_non_string_content(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeClient(_FakeResponse(200, {"message": {"content": 42}})))
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    with pytest.raises(LLMSchemaError, match="类型应为 str"):
        OllamaProvider(cfg).complete(MESSAGES)


def test_ollama_model_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeClient(_FakeResponse(200, {"digest": "sha256:abc"})))
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    assert OllamaProvider(cfg).model_digest() == "sha256:abc"


def test_ollama_digest_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeClient(_FakeResponse(404, {})))
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    with pytest.raises(LLMUnavailableError, match="取模型 digest 失败"):
        OllamaProvider(cfg).model_digest()


def test_ollama_list_models(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        _FakeClient(_FakeResponse(200, {"models": [{"name": "qwen2.5:14b-instruct-q4_K_M"}]})),
    )
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    assert OllamaProvider(cfg).list_models() == ["qwen2.5:14b-instruct-q4_K_M"]


def test_ollama_list_models_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, _FakeClient(_FakeResponse(503, {})))
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    with pytest.raises(LLMUnavailableError, match="列举模型失败"):
        OllamaProvider(cfg).list_models()


def test_parse_json_output() -> None:
    assert parse_json_output('{"a": 1}') == {"a": 1}
    with pytest.raises(LLMSchemaError, match="不是合法 JSON"):
        parse_json_output("not json")


# ─── M4-A 新增：chat() 全量契约（工具 / token / logprobs）───────────


def test_ollama_chat_sends_tools_and_parses_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "model": "qwen2.5:14b-instruct-q4_K_M",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "resolve_person", "arguments": {"surface": "何超"}}}
            ],
        },
        "prompt_eval_count": 155,
        "eval_count": 21,
        "done_reason": "stop",
    }
    client = _FakeClient(_FakeResponse(200, body))
    _patch_client(monkeypatch, client)
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]

    tools = (ToolSchema(name="resolve_person", description="解析人名", parameters={}),)
    response = OllamaProvider(cfg).chat(LLMRequest(messages=MESSAGES, tools=tools))

    assert client.last_payload is not None
    assert client.last_payload["tools"][0]["function"]["name"] == "resolve_person"
    assert response.tool_calls[0].name == "resolve_person"
    assert response.tool_calls[0].arguments == {"surface": "何超"}
    # token 计数取实测值（预算记账靠它，铁律 6）
    assert (response.prompt_tokens, response.completion_tokens) == (155, 21)
    assert response.total_tokens == 176
    assert response.finish_reason == "stop"


def test_ollama_keeps_malformed_arguments_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """畸形参数要原样留住，才能在 Harness 里判成 json_malformed 而不是被吞掉。"""
    body = {
        "message": {
            "content": "",
            "tool_calls": [{"function": {"name": "resolve_person", "arguments": '{"surface": '}}],
        }
    }
    _patch_client(monkeypatch, _FakeClient(_FakeResponse(200, body)))
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    response = OllamaProvider(cfg).chat(LLMRequest(messages=MESSAGES))
    assert response.tool_calls[0].arguments == '{"surface": '


def test_ollama_requests_logprobs_when_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(_FakeResponse(200, {"message": {"content": "x"}}))
    _patch_client(monkeypatch, client)
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    OllamaProvider(cfg).chat(LLMRequest(messages=MESSAGES, logprobs=True, top_logprobs=3))
    assert client.last_payload is not None
    assert client.last_payload["logprobs"] is True
    assert client.last_payload["top_logprobs"] == 3


def test_ollama_logprobs_absent_means_none_not_a_made_up_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本机 Ollama 0.6.8 不返回 logprobs —— 那就是 None，不许拿别的量凑（铁律 6）。"""
    _patch_client(monkeypatch, _FakeClient(_FakeResponse(200, {"message": {"content": "x"}})))
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    response = OllamaProvider(cfg).chat(LLMRequest(messages=MESSAGES, logprobs=True))
    assert response.sequence_logprob is None
    assert response.token_logprobs == ()


def test_ollama_parses_logprobs_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """装上支持该字段的 Ollama 就该立刻可用——解析路径先写好。"""
    body = {
        "message": {
            "content": "你好",
            "logprobs": {"content": [{"logprob": -0.5}, {"logprob": -1.5}]},
        }
    }
    _patch_client(monkeypatch, _FakeClient(_FakeResponse(200, body)))
    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")  # type: ignore[call-arg]
    response = OllamaProvider(cfg).chat(LLMRequest(messages=MESSAGES, logprobs=True))
    assert response.token_logprobs == (-0.5, -1.5)
    assert response.sequence_logprob == pytest.approx(-2.0)


def test_request_fingerprint_covers_tools() -> None:
    base = LLMRequest(messages=MESSAGES)
    with_tools = LLMRequest(
        messages=MESSAGES, tools=(ToolSchema(name="resolve_person", parameters={}),)
    )
    assert request_fingerprint(base) != request_fingerprint(with_tools)
    assert request_fingerprint(base) == request_fingerprint(LLMRequest(messages=MESSAGES))


# ─── M4-A 新增：MockProvider 的场景桩 ───────────────────────────────


def test_mock_scenario_replays_in_order(tmp_path: Path) -> None:
    cfg = Settings(_env_file=None, MOCK_FIXTURE_DIR=tmp_path)  # type: ignore[call-arg]
    provider = MockProvider(cfg)
    provider.register_scenario(
        "retry",
        [tool_response("resolve_person", {}), tool_response("resolve_person", {"surface": "何超"})],
    )
    provider.activate("retry")

    first = provider.chat(LLMRequest(messages=MESSAGES))
    second = provider.chat(LLMRequest(messages=MESSAGES))
    assert first.tool_calls[0].arguments == {}
    assert second.tool_calls[0].arguments == {"surface": "何超"}
    assert provider.remaining == 0


def test_mock_scenario_exhaustion_raises(tmp_path: Path) -> None:
    """耗尽即抛，不循环最后一条——循环会把「少调了一次」伪装成通过。"""
    cfg = Settings(_env_file=None, MOCK_FIXTURE_DIR=tmp_path)  # type: ignore[call-arg]
    provider = MockProvider(cfg)
    provider.register_scenario("one", ["只有一条"])
    provider.activate("one")
    provider.chat(LLMRequest(messages=MESSAGES))
    with pytest.raises(LLMUnavailableError, match="已耗尽"):
        provider.chat(LLMRequest(messages=MESSAGES))


def test_mock_unknown_scenario_raises(tmp_path: Path) -> None:
    cfg = Settings(_env_file=None, MOCK_FIXTURE_DIR=tmp_path)  # type: ignore[call-arg]
    with pytest.raises(LLMUnavailableError, match="未登记的 mock 场景"):
        MockProvider(cfg).activate("nope")


def test_mock_deactivate_returns_to_content_matching(tmp_path: Path) -> None:
    (tmp_path / "stubs.json").write_text(
        json.dumps({"rules": [{"when_contains": ["排班"], "response": "按内容匹配"}]}),
        encoding="utf-8",
    )
    cfg = Settings(_env_file=None, MOCK_FIXTURE_DIR=tmp_path)  # type: ignore[call-arg]
    provider = MockProvider(cfg)
    provider.register_scenario("s", ["按场景"])
    provider.activate("s")
    assert provider.complete(MESSAGES) == "按场景"
    provider.deactivate()
    assert provider.complete(MESSAGES) == "按内容匹配"
