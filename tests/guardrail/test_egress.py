"""egress 拦截 E1~E4（v6 §12.5.4）—— 全离线运行的执行性证明。

| # | 方法 | 断言 |
|---|---|---|
| E1 | monkeypatch DNS，注入一个外网域名请求 | 被 allowlist 拒绝，抛 `EgressDeniedError` |
| E2 | 全仓库 grep `import requests` / `httpx` / `urllib.request` | 除 `core/http.py` 外零命中 |
| E3 | 源码中的 URL 字面量 | 仅允许 `127.0.0.1` 与内网段 |
| E4 | 全链路跑一遍基准周，抓 socket 层出站连接 | 目标地址全部在 allowlist 内 |

## 三道防线各管一段，缺一不可

- **E2/E3 是静态的**：管「代码里写没写」。它们查的是文本形态，连被注释掉又
  复活的写法、动态拼出来的 URL 字符串都能看见，但**管不到运行时真连了哪**。
- **E1 是判定逻辑的单元证明**：管「allowlist 判得对不对」。DNS 被投毒
  （外网域名解析到内网 IP）这类把戏只有它能挡。
- **E4 是运行时的**：管「实际连了哪」。它在 socket 层抓，绕过任何库级封装
  —— 哪怕有人用 `socket` 裸写一个 HTTP 请求，也逃不掉。

**E2/E3 与 CI 用的是同一个脚本**（`deploy/scripts/check_egress.sh`），这里
调它而不是把正则再抄一遍：抄一遍就意味着本地与 CI 可能判得不一样，而
`CLAUDE.md §6` 那条「门禁的判据是脚本的退出码」正是为此写的。
"""

from __future__ import annotations

import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.core.config import PROJECT_ROOT, Settings
from backend.core.errors import EgressDeniedError
from backend.core.http import EgressGuard, build_client

pytestmark = pytest.mark.guardrail

CHECK_EGRESS = PROJECT_ROOT / "deploy" / "scripts" / "check_egress.sh"

#: 与 `Settings.EGRESS_ALLOWLIST` 的缺省一致
DEFAULT_ALLOWLIST = (
    "127.0.0.1",
    "localhost",
    "::1",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)


# ═════════════════════════════════════════════════════════════════════
# E1 —— monkeypatch DNS，注入外网域名请求
# ═════════════════════════════════════════════════════════════════════


@pytest.fixture
def poisoned_dns(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """把**任何**主机名都解析到一个外网 IP。

    这是 E1 的核心构造：它模拟的不只是「用户写了个外网域名」，还包括
    「内网 DNS 被投毒，一个看起来人畜无害的名字指向了外面」。只校验主机名
    字面量的实现会在这里放行，只校验 IP 的实现会在下面 `test_e1_...poisoned`
    里放行 —— 两层都要有才拦得住。
    """
    seen: list[str] = []

    def fake_getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        seen.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    yield seen


def test_e1_external_domain_is_denied(poisoned_dns: list[str]) -> None:
    """E1：外网域名 → `EgressDeniedError`。"""
    guard = EgressGuard(DEFAULT_ALLOWLIST)
    with pytest.raises(EgressDeniedError) as excinfo:
        guard.check_url("https://example.com/v1/models")
    assert "example.com" in str(excinfo.value)
    assert excinfo.value.details["resolved"] == ["93.184.216.34"]
    assert poisoned_dns == ["example.com"]


def test_e1_denial_carries_a_usable_error_contract() -> None:
    """E1：错的时候要说得清 —— 码、严重度、建议一个都不能少。"""
    guard = EgressGuard(DEFAULT_ALLOWLIST)
    with pytest.raises(EgressDeniedError) as excinfo:
        guard.check_host("8.8.8.8")
    err = excinfo.value
    assert err.code == "FTS-4001"
    assert err.suggestions, "拒绝出网必须给出可执行的下一步"
    assert "全离线" in "".join(err.suggestions)


def test_e1_poisoned_name_pointing_inside_is_still_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E1 反向：域名解析到内网段的**放行**，证明拦的是地址不是名字。

    这条与上一条一起，才说明 allowlist 是按「解析后的 IP」判的。只有拒绝用例
    时，一个「什么都拒绝」的实现也能全绿。
    """

    def resolve_inside(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.1.2.3", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve_inside)
    EgressGuard(DEFAULT_ALLOWLIST).check_host("intranet.corp")


def test_e1_split_resolution_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """E1：一个名字解析出「一个内网 + 一个外网」时**整体拒绝**。

    这是最容易写错的一格：`any(...)` 会放行，`all(...)` 才对。攻击形态就是
    在 DNS 里给同一个名字挂两条 A 记录，赌实现用的是 `any`。
    """

    def split(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.1.2.3", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 80)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", split)
    with pytest.raises(EgressDeniedError):
        EgressGuard(DEFAULT_ALLOWLIST).check_host("mixed.corp")


def test_e1_client_factory_refuses_external_base_url(poisoned_dns: list[str]) -> None:
    """E1：连 `build_client(base_url=...)` 这一步都过不去，不必等到发请求。"""
    settings = Settings(_env_file=None, LLM_PROVIDER="mock")
    with pytest.raises(EgressDeniedError):
        build_client(settings, base_url="https://api.openai.com")


def test_e1_guard_is_wired_into_the_transport(poisoned_dns: list[str]) -> None:
    """E1：真发一次请求，拦截发生在 transport 层（所以重定向每一跳都会被查）。"""
    settings = Settings(_env_file=None, LLM_PROVIDER="mock")
    client = build_client(settings)
    try:
        with pytest.raises(EgressDeniedError):
            client.get("https://example.com/")
    finally:
        client.close()


def test_e1_loopback_is_allowed() -> None:
    """E1 反向：本机地址必须放行，否则 Ollama 自己就调不通了。"""
    guard = EgressGuard(DEFAULT_ALLOWLIST)
    for host in ("127.0.0.1", "localhost", "10.0.0.1", "192.168.1.5", "172.16.0.9"):
        guard.check_host(host)


# ═════════════════════════════════════════════════════════════════════
# E2 / E3 —— 静态扫描（调 CI 用的同一个脚本）
# ═════════════════════════════════════════════════════════════════════


def _run_check_egress(root: Path | None = None) -> subprocess.CompletedProcess[str]:
    """跑 `check_egress.sh`。

    ⚠️ **必须跑目标仓库里的那一份脚本**：脚本自己按 `BASH_SOURCE` 推 `ROOT`
    再 `cd` 过去，拿真仓库那份配 `cwd=tmp` 是没用的（它会切回真仓库扫）。
    这个坑值得留在注释里——第一版就是这么写的，于是「种了违规还是绿」。
    """
    base = root or PROJECT_ROOT
    return subprocess.run(  # noqa: S603 - 固定路径的仓库内脚本
        ["bash", str(base / "deploy" / "scripts" / "check_egress.sh")],  # noqa: S607
        cwd=str(base),
        capture_output=True,
        text=True,
        check=False,
    )


def test_e2_e3_repository_passes_the_ci_scan() -> None:
    """E2+E3：当前仓库跑 CI 那个脚本必须 exit 0。"""
    result = _run_check_egress()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "✅ E2 通过" in result.stdout
    assert "✅ E3 通过" in result.stdout


def test_e2_only_core_http_may_import_httpx() -> None:
    """E2：`import httpx` 的唯一合法落点是 `backend/core/http.py`。"""
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "backend", "frontend"],  # noqa: S607
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    offenders: list[str] = []
    for rel in listing:
        if not rel.endswith(".py") or rel == "backend/core/http.py":
            continue
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(
                ("import httpx", "from httpx", "import requests", "from requests")
            ):
                offenders.append(f"{rel}: {stripped}")
            if stripped.startswith(("import urllib.request", "from urllib.request")):
                offenders.append(f"{rel}: {stripped}")
    assert offenders == [], "E2 失败，检出直接 import：\n" + "\n".join(offenders)


def test_e3_scanner_actually_catches_a_planted_violation(tmp_path: Path) -> None:
    """E3 的**自检**：往扫描范围里种一条违规，脚本必须变红。

    没有这一条，`check_egress.sh` 完全可能因为正则写错而永远返回 0 ——
    一个永远绿的门禁与没有门禁是同一件事。这里用一份 tmp 仓库副本来种，
    **不碰真仓库**（CLAUDE.md §6：不在「CI 看不到的那一侧」验证）。
    """
    repo = tmp_path / "repo"
    (repo / "backend" / "core").mkdir(parents=True)
    (repo / "deploy" / "scripts").mkdir(parents=True)
    (repo / "deploy" / "scripts" / "check_egress.sh").write_text(
        CHECK_EGRESS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "backend" / "core" / "http.py").write_text("import httpx\n", encoding="utf-8")

    clean = _run_check_egress(root=repo)
    assert clean.returncode == 0, "干净副本本应通过：" + clean.stdout

    (repo / "backend" / "leaky.py").write_text(
        'import requests\n\nURL = "https://pypi.org/simple/"\n', encoding="utf-8"
    )
    dirty = _run_check_egress(root=repo)
    assert dirty.returncode == 1, "种了违规却没红 —— 扫描器是橡皮图章：" + dirty.stdout
    assert "❌ E2 失败" in dirty.stdout
    assert "❌ E3 失败" in dirty.stdout
    assert "backend/leaky.py" in dirty.stdout


def test_e3_internal_urls_are_not_flagged(tmp_path: Path) -> None:
    """E3 反向：内网与回环 URL 不许被误报，否则大家会开始加 `# noqa`。"""
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    (repo / "deploy" / "scripts").mkdir(parents=True)
    (repo / "deploy" / "scripts" / "check_egress.sh").write_text(
        CHECK_EGRESS.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (repo / "backend" / "ok.py").write_text(
        'A = "http://127.0.0.1:11434"\nB = "http://10.0.0.5:5433"\nC = "http://192.168.1.1"\n',
        encoding="utf-8",
    )
    assert _run_check_egress(root=repo).returncode == 0


__all__: list[str] = []
