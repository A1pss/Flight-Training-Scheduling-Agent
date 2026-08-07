"""OllamaProvider —— 真机调用（v6 §11.2）。

**出网必须走 `core/http.py`**：本模块不 import httpx/requests/urllib，只用
`build_client()`。Ollama 跑在 `127.0.0.1:11434`，命中 allowlist 的回环段，
所以「禁止 egress」与「能调本地模型」并不矛盾。
"""

from __future__ import annotations

import json
from typing import Any

from backend.core.config import Settings, get_settings
from backend.core.errors import LLMSchemaError, LLMUnavailableError
from backend.core.http import build_client


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
        payload: dict[str, Any] = {
            "model": self._cfg.LLM_MODEL,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": self._cfg.LLM_NUM_CTX,
                "seed": self._cfg.SOLVER_SEED,
            },
        }
        if schema is not None:
            # Ollama 的受约束解码：format 传 JSON Schema
            payload["format"] = schema

        with build_client(self._cfg, base_url=self._cfg.OLLAMA_BASE_URL) as client:
            try:
                resp = client.post("/api/chat", json=payload)
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
            body = resp.json()

        try:
            content = body["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMSchemaError(
                "Ollama 响应缺少 message.content 字段",
                details={"body_keys": sorted(body) if isinstance(body, dict) else None},
            ) from exc

        if not isinstance(content, str):
            raise LLMSchemaError(
                f"Ollama 返回的 content 类型应为 str，实际 {type(content).__name__}"
            )
        return content

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
