"""OllamaProvider —— 真机调用（v6 §11.2）。

**出网必须走 `core/http.py`**：本模块不 import httpx/requests/urllib，只用
`build_client()`。Ollama 跑在 `127.0.0.1:11434`，命中 allowlist 的回环段，
所以「禁止 egress」与「能调本地模型」并不矛盾。

支持四件事，对应 v6 §7.7.1 的双模式调用与预算控制：

| 能力 | 落点 | 实测状态（本机 Ollama 0.6.8 + qwen2.5:14b-instruct-q4_K_M） |
|---|---|---|
| 原生 tool calling | `tools=[...]` | ✅ 返回 `message.tool_calls[].function.{name,arguments}` |
| 受约束 JSON 解码 | `format=<schema>` | ✅ 返回严格符合 schema 的 JSON 文本 |
| temperature / seed | `options` | ✅ |
| logprobs | `logprobs` / `top_logprobs` | ❌ **0.6.8 不返回任何 logprob 字段** |

logprobs 那一行是环境限制而非实现缺失：请求侧照发、响应侧照解析，装上支持
该字段的 Ollama 就生效。取不到时 `sequence_logprob=None`，**绝不拿别的量凑**
（铁律 6）。M4-B 的置信度校准（§7.3.5）要按这一条重新拿主意。
"""

from __future__ import annotations

import json
from typing import Any

from backend.core.config import Settings, get_settings
from backend.core.errors import LLMSchemaError, LLMUnavailableError
from backend.core.http import build_client
from backend.llm.types import LLMRequest, LLMResponse, RawToolCall


class OllamaProvider:
    """通过 Ollama `/api/chat` 做一次补全。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._cfg = settings or get_settings()

    # ── LLMProvider 契约 ─────────────────────────────────────────────
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
    ) -> str:
        """M0 冻结的薄封装：只要文本。"""
        return self.chat(
            LLMRequest(messages=messages, format_schema=schema, temperature=temperature)
        ).text

    def chat(self, request: LLMRequest) -> LLMResponse:
        payload = self._build_payload(request)
        body = self._post("/api/chat", payload)
        return self._parse(body)

    # ── 请求构造 ─────────────────────────────────────────────────────
    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": request.temperature,
            "num_ctx": self._cfg.LLM_NUM_CTX,
            "seed": self._cfg.SOLVER_SEED,
        }
        payload: dict[str, Any] = {
            "model": self._cfg.LLM_MODEL,
            "messages": request.messages,
            "stream": False,
            "options": options,
        }
        if request.format_schema is not None:
            # Ollama 的受约束解码：format 传 JSON Schema
            payload["format"] = request.format_schema
        if request.tools:
            payload["tools"] = [t.to_ollama() for t in request.tools]
        if request.logprobs:
            payload["logprobs"] = True
            if request.top_logprobs is not None:
                payload["top_logprobs"] = request.top_logprobs
        return payload

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        with build_client(self._cfg, base_url=self._cfg.OLLAMA_BASE_URL) as client:
            try:
                resp = client.post(path, json=payload)
            except Exception as exc:
                raise LLMUnavailableError(
                    f"Ollama 调用失败：{exc}",
                    details={"base_url": self._cfg.OLLAMA_BASE_URL},
                    suggestions=[
                        "确认 deploy/native/start_ollama.sh 已启动",
                        "LLM 不可用不影响排班：改用 /api/v1/schedule 表单入口（FTS-4001）",
                    ],
                ) from exc

            if resp.status_code != 200:
                raise LLMUnavailableError(
                    f"Ollama 返回 HTTP {resp.status_code}",
                    details={"body": resp.text[:512]},
                )
            return resp.json()

    # ── 响应解析 ─────────────────────────────────────────────────────
    def _parse(self, body: Any) -> LLMResponse:
        try:
            message = body["message"]
            content = message["content"]
        except (KeyError, TypeError) as exc:
            raise LLMSchemaError(
                "Ollama 响应缺少 message.content 字段",
                details={"body_keys": sorted(body) if isinstance(body, dict) else None},
            ) from exc

        if not isinstance(content, str):
            raise LLMSchemaError(
                f"Ollama 返回的 content 类型应为 str，实际 {type(content).__name__}"
            )

        token_logprobs = _extract_token_logprobs(body, message)
        return LLMResponse(
            text=content,
            tool_calls=_extract_tool_calls(message),
            prompt_tokens=_as_int(body.get("prompt_eval_count")),
            completion_tokens=_as_int(body.get("eval_count")),
            sequence_logprob=(sum(token_logprobs) if token_logprobs else None),
            token_logprobs=token_logprobs,
            model=str(body.get("model", "")),
            finish_reason=str(body.get("done_reason", "")),
        )

    # ── healthcheck 辅助 ─────────────────────────────────────────────
    def model_digest(self) -> str:
        """取当前 `LLM_MODEL` 的 digest，供 §11.5「模型完整性」双重校验。"""
        with build_client(self._cfg, base_url=self._cfg.OLLAMA_BASE_URL) as client:
            resp = client.post("/api/show", json={"model": self._cfg.LLM_MODEL})
            if resp.status_code != 200:
                raise LLMUnavailableError(
                    f"取模型 digest 失败：HTTP {resp.status_code}",
                    details={"model": self._cfg.LLM_MODEL},
                )
            data = resp.json()
        digest = data.get("digest") or data.get("details", {}).get("digest", "")
        return str(digest)

    def list_models(self) -> list[str]:
        with build_client(self._cfg, base_url=self._cfg.OLLAMA_BASE_URL) as client:
            resp = client.get("/api/tags")
            if resp.status_code != 200:
                raise LLMUnavailableError(f"列举模型失败：HTTP {resp.status_code}")
            data = resp.json()
        return [str(m["name"]) for m in data.get("models", [])]


def _as_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _extract_tool_calls(message: Any) -> tuple[RawToolCall, ...]:
    """从 `message.tool_calls` 解析工具调用。

    **不在这里做任何契约校验**——参数缺字段、类型写错都要原样送到
    `harness.validation`，那里才是失败模式分类的唯一落点（§12.5.1）。
    """
    if not isinstance(message, dict):
        return ()
    raw = message.get("tool_calls")
    if not isinstance(raw, list):
        return ()

    calls: list[RawToolCall] = []
    for item in raw:
        fn = item.get("function", {}) if isinstance(item, dict) else {}
        if not isinstance(fn, dict):
            continue
        args = fn.get("arguments", {})
        if not isinstance(args, (dict, str)):
            args = json.dumps(args, ensure_ascii=False)
        calls.append(RawToolCall(name=str(fn.get("name", "")), arguments=args))
    return tuple(calls)


def _extract_token_logprobs(body: Any, message: Any) -> tuple[float, ...]:
    """兼容两种可能的 logprobs 落点，取不到就返回空。

    Ollama 0.6.8 两处都没有。这段代码是为「装上支持 logprobs 的版本后立刻
    可用」而写的，不是猜测性兜底：取不到就是空元组，上层据此把
    `sequence_logprob` 置 None。
    """
    for container in (message, body):
        if not isinstance(container, dict):
            continue
        entries = container.get("logprobs")
        if isinstance(entries, dict):
            entries = entries.get("content")
        if not isinstance(entries, list):
            continue
        values: list[float] = []
        for entry in entries:
            if isinstance(entry, (int, float)):
                values.append(float(entry))
            elif isinstance(entry, dict) and isinstance(entry.get("logprob"), (int, float)):
                values.append(float(entry["logprob"]))
        if values:
            return tuple(values)
    return ()


def parse_json_output(raw: str) -> Any:
    """把受约束解码的输出解析为对象；失败即 FTS-4002（由上层重试 2 次）。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMSchemaError(
            f"LLM 输出不是合法 JSON：{exc}",
            details={"raw_prefix": raw[:256]},
        ) from exc


__all__ = ["OllamaProvider", "parse_json_output"]
