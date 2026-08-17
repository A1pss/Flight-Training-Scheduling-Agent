"""E2E 夹具：真 uvicorn + 真 RQ worker + 真 Streamlit + 真 Chromium。

```
pytest tests/e2e -m e2e          # 需要 chromium：python -m playwright install chromium
```

## 为什么起真的 RQ worker

`tests/integration/test_api_live.py` 跑的是 inline runner（同一个
`execute_run`，只是同进程）。**这里要验的恰恰是「跨进程」那一半**：
API 进程提交、worker 进程执行、状态经 Redis 传递、HITL 停在 checkpoint 上
——v6 §9.2 的全部承诺都在进程边界上。

## CI 上没有浏览器就跳过

与 `@pytest.mark.ollama` 同一口径（业务方 2026-08-18 选定）：CI 不装 chromium、
不起前端，这批用例 skip；本机装了就真跑。**跳过的原因会打印出来**，
不会静默地「绿着但什么都没跑」。

## 一次求解，多条断言

整个模块共用**一次**基准周排班（~20 s 求解）。25 条断言分布在四个页签上，
但它们看的是同一次运行 —— 每条都重跑一次的话这个文件要跑一个小时。
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from backend.core.config import PROJECT_ROOT, Settings
from backend.core.db import session_scope
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot

pytestmark = pytest.mark.e2e

#: E2E 用的 token 表（三个角色）。前端用 director 那个 —— 它要能按「确认并归档」。
E2E_TOKENS = "e2e-dir:P01:director,e2e-sch:P02:scheduler,e2e-view:P03:viewer"
#: 求解 + 校验 + 报表的等待上限。基准周实测 ~20 s，给到 240 s 是数量级余量。
RUN_TIMEOUT_MS = 240_000


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, *, timeout_s: float = 90.0, what: str = "服务") -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(1.0)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.3)
    raise RuntimeError(f"{what} 在 {timeout_s}s 内没起来（端口 {port}）")


@pytest.fixture(scope="session")
def chromium() -> Iterator[Any]:
    """真 Chromium。没装就跳过整批（CI 的口径）。"""
    playwright_api = pytest.importorskip("playwright.sync_api")
    try:
        with playwright_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            yield browser
            browser.close()
    except Exception as exc:  # pragma: no cover —— 没装浏览器时跳过
        pytest.skip(f"Chromium 不可用（`python -m playwright install chromium`）：{exc}")


@pytest.fixture(scope="session")
def snapshot_id() -> str:
    with session_scope() as session:
        return ensure_baseline_snapshot(session)


@pytest.fixture(scope="session")
def stack(snapshot_id: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    """起 API + RQ worker + Streamlit，跑完全部关掉。"""
    assert snapshot_id
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    plans_root = tmp_path_factory.mktemp("e2e-plans")
    api_port, ui_port = _free_port(), _free_port()

    env = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT),
        "API_TOKENS": E2E_TOKENS,
        "FRONTEND_API_TOKEN": "e2e-dir",
        "API_BASE_URL": f"http://127.0.0.1:{api_port}",
        "JOB_RUNNER": "rq",
        "LLM_PROVIDER": "mock",
        "APP_ENV": "ci",
        "PLANS_DIR": str(plans_root),
        # 插桩环境下的求解墙钟（与 tests/conftest.py 同一口径，`Z-23`）
        "SOLVER_TIME_LIMIT_S": "300",
        "FRONTEND_POLL_INTERVAL_S": "1.5",
    }

    api = subprocess.Popen(  # noqa: S603 —— 固定命令
        [sys.executable, "-m", "uvicorn", "backend.api.main:app", "--port", str(api_port)],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    worker = _start_worker(env, settings)
    ui = subprocess.Popen(  # noqa: S603 —— 固定命令
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "frontend/app.py",
            "--server.port",
            str(ui_port),
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(api_port, what="API")
        _wait_for_port(ui_port, what="Streamlit")
        yield {
            "api_port": api_port,
            "ui_port": ui_port,
            "url": f"http://127.0.0.1:{ui_port}",
            "env": env,
            "worker": worker,
            "settings": settings,
            "plans_root": plans_root,
        }
    finally:
        for process in (ui, worker, api):
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()


def _start_worker(env: dict[str, str], settings: Settings) -> subprocess.Popen[bytes]:
    """起一个真的 RQ worker 进程。

    `--burst` **不能加**：worker 要一直在，等着人工确认之后的第二个任务。
    """
    rq = shutil.which("rq") or "rq"
    return subprocess.Popen(  # noqa: S603 —— 固定命令
        [
            rq,
            "worker",
            "--url",
            settings.REDIS_URL,
            "--worker-class",
            "rq.SimpleWorker",  # fork 出来的子进程拿不到 PYTHONPATH 里的仓库根
            settings.RQ_QUEUE,
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def restart_worker(stack: dict[str, Any]) -> None:
    """杀掉 worker 再起一个新的 —— HITL 跨进程恢复那条用例用。"""
    worker: subprocess.Popen[bytes] = stack["worker"]
    worker.terminate()
    try:
        worker.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover
        worker.kill()
    stack["worker"] = _start_worker(stack["env"], stack["settings"])


@pytest.fixture(scope="session")
def page(chromium: Any, stack: dict[str, Any]) -> Iterator[Any]:
    """一个已经跑完基准周排班、停在人工门禁的页面。

    **整个模块共用**（见模块注释：一次求解，多条断言）。
    """
    context = chromium.new_context(viewport={"width": 1600, "height": 1200})
    page = context.new_page()
    page.set_default_timeout(30_000)
    page.goto(stack["url"], wait_until="networkidle")

    # 走「表单排班」这条降级路径提交：它零 LLM、结果确定（v6 §9.3 FTS-4001）
    page.get_by_text("表单排班（LLM 不可用时的降级路径）").click()
    page.get_by_test_id("stBaseButton-secondary").filter(has_text="按表单排班").first.click()
    page.get_by_text("排班结果", exact=True).wait_for(timeout=RUN_TIMEOUT_MS)
    page.wait_for_selector("text=确认并归档", timeout=RUN_TIMEOUT_MS)
    yield page
    context.close()


@pytest.fixture(scope="session")
def plans_root(stack: dict[str, Any]) -> Path:
    return Path(stack["plans_root"])
