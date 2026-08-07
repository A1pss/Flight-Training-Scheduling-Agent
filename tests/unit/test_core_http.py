"""`backend/core/http.py` 的 egress allowlist 单测（v6 §11.5 / §12.5.4 E1）。

出口标准要求：**内网地址放行、外网地址抛 EgressDeniedError**。
本文件同时覆盖 E1 的手法——monkeypatch DNS 解析，注入一个外网域名请求。
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from backend.core.config import Settings
from backend.core.errors import EgressDeniedError, ErrorCode
from backend.core.http import EgressGuard, build_client

ALLOWLIST = (
    "127.0.0.1",
    "localhost",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)


@pytest.fixture
def guard() -> EgressGuard:
    return EgressGuard(ALLOWLIST)


# ─── 放行 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",  # Ollama / PG / Redis 都在这里
        "localhost",
        "10.1.2.3",  # RFC1918
        "172.16.5.5",
        "192.168.0.11",
        "::1",
    ],
)
def test_internal_hosts_allowed(guard: EgressGuard, host: str) -> None:
    guard.check_host(host)  # 不抛即通过


def test_ollama_url_allowed(guard: EgressGuard) -> None:
    guard.check_url("http://127.0.0.1:11434/api/chat")


# ─── 拒绝 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "1.1.1.1",
        "172.32.0.1",  # 刚好落在 172.16/12 之外
        "11.0.0.1",  # 刚好落在 10/8 之外
        "169.254.169.254",  # 云元数据服务，典型的 SSRF 目标
    ],
)
def test_external_ips_denied(guard: EgressGuard, host: str) -> None:
    with pytest.raises(EgressDeniedError, match="不在 egress allowlist 内"):
        guard.check_host(host)


def test_empty_host_denied(guard: EgressGuard) -> None:
    with pytest.raises(EgressDeniedError):
        guard.check_host("")


def test_denied_error_carries_fts_code(guard: EgressGuard) -> None:
    with pytest.raises(EgressDeniedError) as exc:
        guard.check_host("8.8.8.8")
    assert exc.value.code == ErrorCode.LLM_UNAVAILABLE
    assert exc.value.details["host"] == "8.8.8.8"


# ─── E1：monkeypatch DNS，注入外网域名 ───────────────────────────────


def test_e1_external_domain_denied_via_dns(
    guard: EgressGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    """把一个域名解析到外网 IP → 必须被拒（§12.5.4 E1）。"""

    def fake_getaddrinfo(*_a: Any, **_k: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(EgressDeniedError):
        guard.check_host("example.invalid")


def test_domain_resolving_to_loopback_allowed(
    guard: EgressGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    """反向：域名解析到回环则放行——内网 DNS 别名是合法用法。"""

    def fake_getaddrinfo(*_a: Any, **_k: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    guard.check_host("ollama.internal")


def test_unresolvable_host_denied(guard: EgressGuard, monkeypatch: pytest.MonkeyPatch) -> None:
    """解析失败不等于安全——空解析结果一律拒绝，不做「反正连不上」的假设。"""

    def boom(*_a: Any, **_k: Any) -> list[Any]:
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(EgressDeniedError):
        guard.check_host("whatever.invalid")


def test_mixed_resolution_denied(guard: EgressGuard, monkeypatch: pytest.MonkeyPatch) -> None:
    """一个 IP 在内网、另一个在外网 → 必须拒绝（DNS rebinding 的典型形态）。"""

    def fake_getaddrinfo(*_a: Any, **_k: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(EgressDeniedError):
        guard.check_host("rebind.invalid")


# ─── build_client ────────────────────────────────────────────────────


def test_build_client_rejects_external_base_url() -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(EgressDeniedError):
        build_client(cfg, base_url="http://8.8.8.8:80")


def test_build_client_accepts_loopback_base_url() -> None:
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    with build_client(cfg, base_url="http://127.0.0.1:11434") as client:
        assert str(client.base_url).startswith("http://127.0.0.1:11434")


def test_transport_blocks_external_request_at_send_time() -> None:
    """校验发生在 transport 层：即便绕开 base_url 校验，实际发送仍会被拦。"""
    cfg = Settings(_env_file=None)  # type: ignore[call-arg]
    with build_client(cfg) as client, pytest.raises(EgressDeniedError):
        client.get("http://8.8.8.8/")


def test_allowlist_parsed_from_settings() -> None:
    cfg = Settings(_env_file=None, EGRESS_ALLOWLIST="127.0.0.1,10.0.0.0/8")  # type: ignore[call-arg]
    assert cfg.egress_allowlist == ("127.0.0.1", "10.0.0.0/8")
