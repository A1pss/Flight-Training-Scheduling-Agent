"""全仓库**唯一**允许出网的受限 HTTP 工厂（v6 §11.5 / §12.5.4）。

三条设计要点：

1. **除本模块外，全仓库禁止 import `requests` / `httpx` / `urllib.request`**，
   由 `.importlinter` 的禁令三在 CI 强制（v6 附录 A ③）。
2. allowlist 同时校验**字面主机名**与**解析后的 IP**。只校验主机名挡不住
   把外网域名解析到内网的把戏，只校验 IP 挡不住 DNS 被 monkeypatch 的
   护栏测试（§12.5.4 E1）——两层都要。
3. 校验发生在 **transport 层**而非调用点，所以**重定向的每一跳都会被重新
   校验**：一个 allowlist 内的地址 302 到外网，同样会被拒。
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable, Sequence
from typing import Final

import httpx

from backend.core.config import Settings, get_settings
from backend.core.errors import EgressDeniedError

#: 无需 DNS 解析即可放行的主机名字面量（全部指向本机）。
_LOOPBACK_NAMES: Final[frozenset[str]] = frozenset({"localhost", "localhost.localdomain"})


def _parse_allowlist(
    entries: Iterable[str],
) -> tuple[frozenset[str], tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]]:
    """把 allowlist 条目拆成「主机名集合」与「网段列表」。

    条目可以是主机名（``localhost``）、裸 IP（``127.0.0.1``）或 CIDR
    （``10.0.0.0/8``）。裸 IP 一律按 /32（/128）网段处理。
    """
    names: set[str] = set()
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in entries:
        entry = raw.strip()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            names.add(entry.lower())
    return frozenset(names), tuple(nets)


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """解析主机名为 IP 列表。解析失败返回空列表（→ 视为不可放行）。

    护栏测试 §12.5.4 E1 正是 monkeypatch 本函数用到的
    :func:`socket.getaddrinfo` 来注入一个「外网域名」。
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return []
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        try:
            out.append(ipaddress.ip_address(str(sockaddr[0])))
        except ValueError:
            continue
    return out


class EgressGuard:
    """allowlist 判定器。与 httpx 解耦，便于单测直接断言。"""

    def __init__(self, allowlist: Sequence[str]) -> None:
        self._names, self._nets = _parse_allowlist(allowlist)

    @property
    def allowlist_names(self) -> frozenset[str]:
        return self._names

    def _ip_allowed(self, ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return any(ip in net for net in self._nets)

    def check_host(self, host: str) -> None:
        """放行则静默返回；否则抛 :class:`EgressDeniedError`。"""
        if not host:
            raise EgressDeniedError(
                "出网请求缺少主机名，已拒绝",
                details={"host": host, "allowlist": sorted(self._names)},
            )

        normalized = host.lower().strip("[]")

        # ① 主机名字面量命中 allowlist
        if normalized in self._names or normalized in _LOOPBACK_NAMES:
            return

        # ② 主机名本身就是 IP → 直接查网段
        try:
            literal_ip = ipaddress.ip_address(normalized)
        except ValueError:
            pass
        else:
            if self._ip_allowed(literal_ip):
                return
            raise EgressDeniedError(
                f"目标地址 {host} 不在 egress allowlist 内，已拒绝出网",
                details={"host": host, "resolved": [str(literal_ip)]},
                suggestions=["检查是否误用了外网服务；本项目要求全离线运行（v6 §11.5）"],
            )

        # ③ 主机名 → 解析为 IP → 逐个查网段。空解析结果视为不可放行。
        resolved = _resolve(normalized)
        if resolved and all(self._ip_allowed(ip) for ip in resolved):
            return

        raise EgressDeniedError(
            f"目标地址 {host} 不在 egress allowlist 内，已拒绝出网",
            details={
                "host": host,
                "resolved": [str(ip) for ip in resolved],
                "allowlist": sorted(self._names),
            },
            suggestions=["检查是否误用了外网服务；本项目要求全离线运行（v6 §11.5）"],
        )

    def check_url(self, url: str | httpx.URL) -> None:
        self.check_host(httpx.URL(url).host)


class _GuardedTransport(httpx.BaseTransport):
    """在每一次实际发送前校验目标主机的 transport 包装。

    包在 transport 层而不是用 ``event_hooks``，是为了让**重定向的每一跳**
    都重新走一次校验。
    """

    def __init__(self, guard: EgressGuard, inner: httpx.BaseTransport) -> None:
        self._guard = guard
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._guard.check_url(request.url)
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def build_client(
    settings: Settings | None = None,
    *,
    timeout: float | None = None,
    base_url: str = "",
    headers: dict[str, str] | None = None,
) -> httpx.Client:
    """构造一个受 allowlist 约束的 :class:`httpx.Client`。

    **这是全仓库获取 HTTP 客户端的唯一入口。**
    """
    cfg = settings or get_settings()
    guard = EgressGuard(cfg.egress_allowlist)
    if base_url:
        guard.check_url(base_url)
    return httpx.Client(
        transport=_GuardedTransport(guard, httpx.HTTPTransport(retries=0)),
        timeout=timeout if timeout is not None else cfg.LLM_TIMEOUT_S,
        base_url=base_url,
        headers=headers or {},
        follow_redirects=False,
    )


__all__ = ["EgressGuard", "build_client"]
