"""前端访问后端的唯一出口。

## 为什么不 `import httpx`

`.importlinter` 的禁令三与 `deploy/scripts/check_egress.sh` 的 E2 都盯着这件事：
**全仓库只有 `backend/core/http.py` 可以 import httpx**（v6 §11.5 / §12.5.4）。
前端也在扫描范围内（`check_egress.sh` 的 `SCAN_DIRS=(backend frontend)`），
所以这里走 `build_client()` —— 它带着 allowlist 守卫，连本机 8000 端口是
allowlist 里的第一条，而万一有人把 `API_BASE_URL` 改成外网地址，
**请求会在 transport 层被拒**，不是「悄悄发出去了」。

## 客户端不做重试

后端已经把「什么可重试」写进了 `ErrorResponse.retryable`（v6 §9.3）。
客户端自作主张重试会把「不可重试的失败」变成三次失败，还会让幂等键之外的
副作用（比如两次上传）翻倍。要重试就由用户按按钮。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.config import Settings, get_settings
from backend.core.http import build_client


class ApiError(RuntimeError):
    """后端返回了错误。`payload` 是原始 JSON（业务错误带 `code`，故障带 `kind`）。"""

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(self.message)

    @property
    def code(self) -> str:
        return str(self.payload.get("code", ""))

    @property
    def message(self) -> str:
        return str(self.payload.get("message", f"HTTP {self.status_code}"))

    @property
    def suggestions(self) -> list[str]:
        return [str(s) for s in self.payload.get("suggestions", [])]

    @property
    def details(self) -> dict[str, Any]:
        raw = self.payload.get("details")
        return dict(raw) if isinstance(raw, dict) else {}


@dataclass
class ApiClient:
    """极薄的一层：拼 URL、带 token、把非 2xx 翻成 `ApiError`。"""

    base_url: str
    token: str
    timeout_s: float = 300.0
    settings: Settings | None = None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> ApiClient:
        cfg = settings or get_settings()
        return cls(
            base_url=cfg.API_BASE_URL,
            token=cfg.FRONTEND_API_TOKEN,
            settings=cfg,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        files: Any = None,
    ) -> Any:
        client = build_client(
            self.settings,
            timeout=self.timeout_s,
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.token}"},
        )
        with client:
            response = client.request(method, path, json=json_body, params=params, files=files)
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"kind": "internal", "message": response.text[:500]}
            raise ApiError(response.status_code, payload)
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response.content

    # ── 摄取 ─────────────────────────────────────────────────────────
    def upload(self, uploads: list[tuple[str, bytes]]) -> dict[str, Any]:
        files = [("files", (name, data, "application/octet-stream")) for name, data in uploads]
        return dict(self._request("POST", "/api/v1/ingest", files=files))

    def changeset(self, ingest_job_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/api/v1/ingest/{ingest_job_id}/changeset"))

    def confirm(self, ingest_job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return dict(
            self._request("POST", f"/api/v1/ingest/{ingest_job_id}/confirm", json_body=body)
        )

    # ── 提交 ─────────────────────────────────────────────────────────
    def chat(self, body: dict[str, Any]) -> dict[str, Any]:
        return dict(self._request("POST", "/api/v1/chat", json_body=body))

    def schedule(self, body: dict[str, Any]) -> dict[str, Any]:
        return dict(self._request("POST", "/api/v1/schedule", json_body=body))

    # ── 轮询与结果 ───────────────────────────────────────────────────
    def job(self, job_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/api/v1/jobs/{job_id}"))

    def run(self, trace_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/api/v1/runs/{trace_id}"))

    # ── 决策与产物 ───────────────────────────────────────────────────
    def approve(self, trace_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return dict(self._request("POST", f"/api/v1/schedule/{trace_id}/approve", json_body=body))

    def reject(self, trace_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return dict(self._request("POST", f"/api/v1/schedule/{trace_id}/reject", json_body=body))

    def export(self, trace_id: str) -> bytes:
        payload = self._request("GET", f"/api/v1/schedule/{trace_id}/export")
        return payload if isinstance(payload, bytes) else bytes(str(payload), "utf-8")

    def plans(self, week: str | None = None) -> dict[str, Any]:
        return dict(self._request("GET", "/api/v1/plans", params={"week": week} if week else None))

    def health(self) -> dict[str, Any]:
        return dict(self._request("GET", "/health"))


def read_upload(path: Path) -> tuple[str, bytes]:
    return path.name, path.read_bytes()


__all__ = ["ApiClient", "ApiError", "read_upload"]
