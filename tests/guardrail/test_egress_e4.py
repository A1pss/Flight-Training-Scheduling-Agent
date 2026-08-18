"""egress 拦截 **E4**（v6 §12.5.4）：全链路跑一遍基准周，抓 socket 层出站连接。

> E4 | 全链路跑一遍基准周，抓 socket 层出站连接 | 目标地址全部在 allowlist 内

## 为什么在 socket 层抓，而不是在 `core/http.py` 里数

`core/http.py` 的 allowlist 只管**走它的那些请求**。E2 的静态扫描能保证没人
直接 `import httpx`，但它挡不住：

- 用 `socket` 裸写一个 HTTP 请求；
- 某个第三方库（psycopg / redis-py / chromadb / sentence-transformers）自己
  连出去 —— 它们不经过我们的工厂，import-linter 与 E2 都看不见；
- 某个「离线」模型加载器顺手去 HuggingFace 查一下版本。

**最后那一类是最现实的威胁**：`transformers` / `sentence-transformers` 在缓存
命中时也可能发一个 HEAD 去对版本，一旦离线机上跑就是几十秒超时。E4 是唯一
能在交付前把它抓出来的测试。

所以这里把 `socket.socket.connect` / `connect_ex` / `socket.create_connection`
三个入口一起包起来 —— **凡是要出去的，必须先经过这里**。

## 判据

抓到的每一个目标地址都必须在 `Settings.EGRESS_ALLOWLIST` 内。实际会抓到的是
PG（127.0.0.1:5433）与 Redis（127.0.0.1:6380）——那正是「全离线」的样子：
一次完整排班除了本机数据库谁都不连。

⚠️ **AF_UNIX 不计**：本地 socket 文件根本没有「出站」这回事（它不经过网络栈）。
把它算进来只会让判据变成「代码里有没有用 unix socket」，与 egress 无关。
"""

from __future__ import annotations

import ipaddress
import os
import socket
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.api.runner import InlineRunner
from backend.api.store import RedisStore
from backend.core.config import Settings, get_settings
from backend.core.db import get_session_factory, session_scope
from backend.core.errors import EgressDeniedError
from backend.core.http import EgressGuard
from tests.conftest import TEST_SOLVER_TIME_LIMIT_S
from tests.fixtures.api_fixtures import BASELINE_TODAY, SCHEDULER, build_test_app, make_settings
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot

pytestmark = [pytest.mark.guardrail, pytest.mark.integration]

MONDAY = "2026-01-05"


@pytest.fixture(scope="module", autouse=True)
def instrumented_solver_budget() -> Iterator[None]:
    """插桩期的求解墙钟（`Z-23`：300 s，产品默认仍是 60 s）。

    ⚠️ 与 `tests/integration/test_api_live.py` 同一条理由：`tests/conftest.py`
    那把是**函数作用域**的，盖不住本模块 module 作用域里真跑的那次求解
    （M6 §3.13 实测栽过一次）。**判据一个字没改**，只是把已裁定的插桩预算
    真正送到这次求解上。
    """
    previous = os.environ.get("SOLVER_TIME_LIMIT_S")
    os.environ["SOLVER_TIME_LIMIT_S"] = TEST_SOLVER_TIME_LIMIT_S
    get_settings.cache_clear()
    yield
    if previous is None:
        os.environ.pop("SOLVER_TIME_LIMIT_S", None)
    else:
        os.environ["SOLVER_TIME_LIMIT_S"] = previous
    get_settings.cache_clear()


class SocketRecorder:
    """把 socket 层的每一次出站目标记下来。"""

    def __init__(self) -> None:
        self.targets: list[tuple[str, int]] = []

    def note(self, address: Any) -> None:
        # AF_UNIX 的地址是字符串（socket 文件路径），不是 (host, port)。
        # 它不经过网络栈，不构成 egress，直接不计。
        if not isinstance(address, tuple) or len(address) < 2:
            return
        host, port = str(address[0]), int(address[1])
        self.targets.append((host, port))


@pytest.fixture(scope="module")
def recorder() -> Iterator[SocketRecorder]:
    """包住三个出站入口。**module 作用域**，覆盖整次基准周排班。"""
    rec = SocketRecorder()
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection

    def patched_connect(self: socket.socket, address: Any) -> Any:
        rec.note(address)
        return real_connect(self, address)

    def patched_connect_ex(self: socket.socket, address: Any) -> Any:
        rec.note(address)
        return real_connect_ex(self, address)

    def patched_create(address: Any, *args: Any, **kwargs: Any) -> Any:
        rec.note(address)
        return real_create(address, *args, **kwargs)

    socket.socket.connect = patched_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = patched_connect_ex  # type: ignore[method-assign]
    socket.create_connection = patched_create  # type: ignore[assignment]
    try:
        yield rec
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.create_connection = real_create  # type: ignore[assignment]


@pytest.fixture(scope="module")
def baseline_targets(
    recorder: SocketRecorder, tmp_path_factory: pytest.TempPathFactory
) -> list[tuple[str, int]]:
    """真跑一次基准周排班（真 PG + 真 Redis + 真求解），返回抓到的出站目标。"""
    store = RedisStore.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]
    try:
        store.set("fts:api:e4probe", "1", 5)
    except Exception as exc:  # pragma: no cover —— 没起 Redis 时跳过
        pytest.skip(f"Redis 不可连（先跑 deploy/native/start_redis.sh）：{exc}")

    with session_scope() as session:
        snapshot_id = ensure_baseline_snapshot(session)
    assert snapshot_id

    root = tmp_path_factory.mktemp("e4-plans")
    app, _ = build_test_app(
        settings=make_settings(PLANS_DIR=root),
        store=store,
        runner=InlineRunner(store),
        session_factory=get_session_factory(),
        today=BASELINE_TODAY,
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/schedule",
        headers=SCHEDULER,
        # ⚠️ **每次跑都要一个新的幂等键**：固定串会在第二次跑时命中 Redis 里的
        # 幂等记录，于是「基准周排班」根本没再跑一遍，而断言仍然全绿
        # （缓存的响应里方案还在）。E4 要抓的是**这次运行**连了哪 ——
        # 没真跑，抓到的就是空气。实测踩过一次：第二轮只花了 2.6 s。
        json={"week_start": MONDAY, "client_request_id": f"e4-egress-{uuid.uuid4().hex}"},
    )
    assert response.status_code == 202, response.text
    trace_id = response.json()["trace_id"]
    run = client.get(f"/api/v1/runs/{trace_id}", headers=SCHEDULER)
    assert run.status_code == 200, run.text
    # 排班真的跑完了才有资格谈「这次排班没出网」
    assert run.json()["plan"]["sorties"], "基准周没排出架次，E4 抓到的连接不代表全链路"
    return list(recorder.targets)


def test_e4_baseline_run_touches_the_network_at_all(
    baseline_targets: list[tuple[str, int]],
) -> None:
    """先证明**抓到了东西**。

    这条看起来多余，其实是 E4 最重要的自检：如果 patch 没生效、或者这次运行
    根本没跑起来，`targets` 会是空列表，而「空列表全部在 allowlist 内」是
    **真的**——一个什么都没测的测试会绿得非常好看。
    """
    assert baseline_targets, "一次连接都没抓到 —— patch 没生效或链路没真跑"


def test_e4_all_outbound_targets_are_in_the_allowlist(
    baseline_targets: list[tuple[str, int]],
) -> None:
    """E4 主断言：全链路的每一个出站目标都在 allowlist 内。"""
    guard = EgressGuard(Settings(_env_file=None).egress_allowlist)
    violations: list[str] = []
    for host, port in baseline_targets:
        try:
            guard.check_host(host)
        except EgressDeniedError:
            violations.append(f"{host}:{port}")
    unique = sorted({f"{h}:{p}" for h, p in baseline_targets})
    assert violations == [], (
        f"E4 失败，出站到 allowlist 外：{sorted(set(violations))}\n全部目标：{unique}"
    )


def test_e4_only_local_services_are_contacted(baseline_targets: list[tuple[str, int]]) -> None:
    """E4 补强：抓到的端口应当只有本机服务那几个。

    比「都在 allowlist 内」更紧一档 —— allowlist 含整个 RFC1918 内网段，
    而一次基准周排班**本来就只该连本机的 PG 与 Redis**。真出现别的目标时，
    哪怕它在内网段内，也值得看一眼是谁连的。
    """
    settings = Settings(_env_file=None)
    expected_ports = {settings.PG_PORT, settings.REDIS_PORT}
    unexpected = sorted({f"{h}:{p}" for h, p in baseline_targets if p not in expected_ports})
    assert unexpected == [], f"出现了预期外的出站目标：{unexpected}"


def test_e4_reports_the_captured_targets(baseline_targets: list[tuple[str, int]]) -> None:
    """把抓到的目标打出来 —— 收工报告要贴这份证据。"""
    tally: dict[str, int] = {}
    for host, port in baseline_targets:
        tally[f"{host}:{port}"] = tally.get(f"{host}:{port}", 0) + 1
    print("\n[E4] 基准周全链路的 socket 出站目标：")
    for target in sorted(tally):
        print(f"  {target}  ×{tally[target]}")
    assert tally


# ═════════════════════════════════════════════════════════════════════
# E4 补强：/proc 层的实连接快照
# ═════════════════════════════════════════════════════════════════════
#
# ⚠️ **Python 层的 patch 有一个真实的盲区，实测撞到了**：上面那组只抓到
# `127.0.0.1:6380`（Redis），**PG 一次都没抓到**。原因是 psycopg 走的是
# libpq —— 连接在 C 层建立，根本不经过 `socket.socket.connect`。
#
# 这个盲区不是小事：一个用 C 扩展出网的库（libpq、某些 gRPC 绑定、
# 静态链接了 curl 的东西）在上面那组测试里是**完全隐形**的。所以 E4 还要有
# 一条不依赖 Python 调用栈的判据：直接问内核。
#
# 做法是把本进程的 socket inode（`/proc/self/fd` 的 symlink）与
# `/proc/net/tcp{,6}` 的连接表对上，取出**本进程自己**的对端地址。
# 比 `ss -tnp` 稳（不依赖外部命令），比读整张 `/proc/net/tcp` 准
# （那是整个 netns 的表，会把别人的连接也算进来）。


def _own_socket_inodes() -> set[str]:
    """本进程持有的 socket inode 集合。"""
    inodes: set[str] = set()
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.is_dir():  # pragma: no cover - 非 Linux
        return inodes
    for entry in fd_dir.iterdir():
        try:
            target = str(entry.readlink())
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[len("socket:[") : -1])
    return inodes


def _hex_to_addr(raw: str) -> tuple[str, int]:
    """`/proc/net/tcp` 的 `0100007F:1A0B` → `("127.0.0.1", 6667)`。

    IPv4 部分是**小端**存放的四个字节；IPv6 是四组小端 32 位。
    """
    host_hex, _, port_hex = raw.partition(":")
    port = int(port_hex, 16)
    if len(host_hex) == 8:
        octets = [int(host_hex[i : i + 2], 16) for i in range(0, 8, 2)]
        return ".".join(str(o) for o in reversed(octets)), port
    words = [host_hex[i : i + 8] for i in range(0, len(host_hex), 8)]
    packed = b"".join(bytes.fromhex(w)[::-1] for w in words)
    return str(ipaddress.ip_address(packed)), port


def _own_tcp_peers() -> list[tuple[str, int]]:
    """本进程当前建立的 TCP 连接对端。"""
    inodes = _own_socket_inodes()
    peers: list[tuple[str, int]] = []
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        path = Path(table)
        if not path.is_file():  # pragma: no cover - 非 Linux
            continue
        for line in path.read_text(encoding="utf-8").splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10:
                continue
            if fields[9] not in inodes:
                continue
            host, port = _hex_to_addr(fields[2])
            if port == 0:
                continue  # 未连接的监听/半开 socket
            peers.append((host, port))
    return peers


@pytest.mark.skipif(not Path("/proc/net/tcp").is_file(), reason="需要 Linux 的 /proc")
def test_e4_kernel_level_peers_are_all_in_the_allowlist(
    baseline_targets: list[tuple[str, int]],
) -> None:
    """E4（内核层）：直接问 `/proc`，本进程的每一条 TCP 连接都在 allowlist 内。

    这条**不依赖 Python 调用栈**，所以 libpq 这类在 C 层建连的库也逃不掉。
    `baseline_targets` 作为前置依赖：先真跑完一次基准周排班，再看连接表。
    """
    assert baseline_targets  # 确保排班真跑过
    guard = EgressGuard(Settings(_env_file=None).egress_allowlist)
    peers = _own_tcp_peers()
    violations = []
    for host, port in peers:
        try:
            guard.check_host(host)
        except EgressDeniedError:
            violations.append(f"{host}:{port}")
    print("\n[E4/proc] 本进程的 TCP 对端：")
    for target in sorted({f"{h}:{p}" for h, p in peers}):
        print(f"  {target}")
    assert violations == [], f"内核层检出 allowlist 外的连接：{sorted(set(violations))}"


@pytest.mark.skipif(not Path("/proc/net/tcp").is_file(), reason="需要 Linux 的 /proc")
def test_e4_kernel_probe_can_actually_see_a_connection() -> None:
    """`/proc` 探针的**自检**：故意连一个本机端口，确认它看得见。

    与 E3 那条「种一条违规」同一个用意 —— 一个永远返回空列表的探针，
    配上「空列表里没有违规」这个断言，会绿得毫无意义。
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    try:
        assert ("127.0.0.1", port) in _own_tcp_peers(), "/proc 探针看不见一条真实连接"
    finally:
        client.close()
        listener.close()
