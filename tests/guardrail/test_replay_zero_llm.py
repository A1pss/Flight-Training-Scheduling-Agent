"""重放期间对 Ollama 的实际请求数 = 0（v6 §12.5.2 的第二条指标）。

**证据要在网络层取，不在代码层取。** 「我检查了一遍代码，重放不会调模型」不是
证据——真正要排除的恰恰是「某个分支悄悄回退到真机」这种代码里看不出来的事。

所以这里起一个**真的 HTTP 服务器**冒充 Ollama，把 `OLLAMA_HOST` 指过去，
数它收到多少个请求：

- **阳性对照**：先用 `OllamaProvider` 真发一次请求，计数器 0 → 1。
  没有这条，「计数器一直是 0」既可能是重放没调模型，也可能是计数器根本不工作。
- **正式测量**：跑一次完整重放，计数器**必须仍是 1**（即重放期间 +0）。
- **socket 层复核**：同时挂一个 `socket.connect` 计数器，断言重放期间**没有任何
  到 11434 的连接尝试**——连 TCP 握手都不该发生。
"""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from backend.core.errors import LLMUnavailableError
from backend.harness.context import ContextBlock
from backend.harness.harness import Harness
from backend.harness.recorder import replay
from backend.harness.types import AgentSpec
from backend.llm.mock import tool_response
from backend.llm.ollama import OllamaProvider
from backend.llm.types import LLMRequest
from tests.fixtures.harness_fixtures import build_harness, harness_settings

pytestmark = pytest.mark.guardrail

ROUTE = AgentSpec(name="route", tools=("resolve_person",))


class _CountingOllamaHandler(BaseHTTPRequestHandler):
    """冒充 Ollama `/api/chat`：只数请求，回一个最小合法响应。"""

    def do_POST(self) -> None:
        self.server.request_count += 1  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = b'{"message": {"content": "\\u4f60\\u597d", "role": "assistant"}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        """别把请求日志刷进测试输出。"""


class CountingOllamaServer(ThreadingHTTPServer):
    request_count = 0


@pytest.fixture
def fake_ollama() -> Any:
    """起一个本地假 Ollama，返回 (host:port, 服务器实例)。"""
    CountingOllamaServer.request_count = 0
    server = CountingOllamaServer(("127.0.0.1", 0), _CountingOllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    try:
        yield f"{host}:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_flow(harness: Harness) -> dict[str, Any]:
    out = harness.call(ROUTE, [ContextBlock(kind="summary", content="快照：8 人 8 机")])
    return {"person_id": out.results[0].value["person_id"]}


def _record(trace_root: Path, ollama_host: str) -> dict[str, Any]:
    cfg = harness_settings(OLLAMA_HOST=ollama_host)
    harness, _, _ = build_harness(
        [tool_response("resolve_person", {"surface": "何超"})],
        settings=cfg,
        trace_root=trace_root,
    )
    state = _run_flow(harness)
    harness.recorder.finish(state)
    return state


def test_counter_itself_works(fake_ollama: tuple[str, CountingOllamaServer]) -> None:
    """阳性对照：真发一次请求，计数器必须动。计数器不会动的话，后面的 0 毫无意义。"""
    host, server = fake_ollama
    provider = OllamaProvider(harness_settings(OLLAMA_HOST=host, LLM_PROVIDER="ollama"))
    assert server.request_count == 0
    provider.chat(LLMRequest(messages=[{"role": "user", "content": "你好"}]))
    assert server.request_count == 1


def test_replay_sends_zero_requests_to_ollama(
    tmp_path: Path,
    fake_ollama: tuple[str, CountingOllamaServer],
    capsys: pytest.CaptureFixture[str],
) -> None:
    host, server = fake_ollama

    # ① 阳性对照，确认这台假 Ollama 确实在数
    OllamaProvider(harness_settings(OLLAMA_HOST=host, LLM_PROVIDER="ollama")).chat(
        LLMRequest(messages=[{"role": "user", "content": "对照"}])
    )
    baseline = server.request_count
    assert baseline == 1

    # ② 录一条轨迹（走 mock，本身也不碰 Ollama）
    original = _record(tmp_path, host)
    after_record = server.request_count

    # ③ socket 层计数：重放期间任何到该端口的连接都记下来
    port = int(host.split(":")[1])
    attempts: list[tuple[str, int]] = []
    real_connect = socket.socket.connect

    def spy_connect(self: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and len(address) == 2:
            attempts.append((str(address[0]), int(address[1])))
        return real_connect(self, address)

    socket.socket.connect = spy_connect  # type: ignore[method-assign]
    try:
        result = replay("trace_test", _run_flow, root=tmp_path, settings=harness_settings())
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]

    with capsys.disabled():
        print("\n── 重放零 LLM 调用实测（v6 §12.5.2）──")
        print(
            f"   假 Ollama 收到的请求数：阳性对照后 {baseline} → 录制后 {after_record} "
            f"→ 重放后 {server.request_count}（重放期间 +{server.request_count - after_record}）"
        )
        print(
            f"   重放期间到 127.0.0.1:{port} 的 TCP 连接尝试：{sum(1 for a in attempts if a[1] == port)} 次"
        )
        print(f"   重放期间全部出站连接尝试：{attempts or '无'}")
        print(f"   重放一致性：consistent={result.consistent}，diff={result.diff}")

    assert result.consistent is True
    assert result.final_state == original
    assert server.request_count == after_record  # 重放期间 +0
    assert [a for a in attempts if a[1] == port] == []


def test_replay_refuses_to_fall_back_to_the_real_provider(
    tmp_path: Path, fake_ollama: tuple[str, CountingOllamaServer]
) -> None:
    """轨迹里没有的请求 → 抛，而不是「去问一下真模型」。"""
    host, server = fake_ollama
    _record(tmp_path, host)
    before = server.request_count

    def flow_with_an_extra_call(harness: Harness) -> dict[str, Any]:
        state = _run_flow(harness)
        harness.call(ROUTE, [ContextBlock(kind="summary", content="轨迹里没有的一次")])
        return state

    with pytest.raises(LLMUnavailableError):
        replay("trace_test", flow_with_an_extra_call, root=tmp_path, settings=harness_settings())
    assert server.request_count == before


def test_replay_does_not_touch_tools_either(
    tmp_path: Path, fake_ollama: tuple[str, CountingOllamaServer]
) -> None:
    """工具也走轨迹：重放期间真 handler 一次都不该被调用。"""
    host, _ = fake_ollama
    _record(tmp_path, host)

    from tests.fixtures.harness_fixtures import registry_with_test_handlers

    registry, handlers = registry_with_test_handlers()
    replay("trace_test", _run_flow, root=tmp_path, settings=harness_settings(), registry=registry)
    assert handlers["resolve_person"].calls == 0
