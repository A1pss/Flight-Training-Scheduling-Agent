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
from backend.llm.mock import DEFAULT_RESPONSE, MockProvider
from backend.llm.ollama import OllamaProvider, parse_json_output
from backend.llm.provider import LLMProvider, build_provider, request_key
from backend.llm.replay import ReplayProvider, record_entry

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
    assert ReplayProvider(cfg).complete(MESSAGES) == "second"  # b 后装载，覆盖 a


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
