"""铁律 1 的执行性检查本身有没有用（`deploy/scripts/check_no_placeholders.sh`）。

## 为什么要给一个 grep 脚本写测试

M2-A 的 CI 上它连着炸了两次，两次都不是「代码里真有半成品」：

1. **扫到了 `__pycache__/*.pyc`**：CI 里 pytest 步骤先跑、留下 `.pyc`，而脚本扫的是
   **工作区**而不是**受版本控制的文件**，`grep -r` 又不跳过二进制 →
   `grep: ....pyc: binary file matches`。任何 `.pyc` 里恰好带上那几个字节都能让 CI 红。
2. **扫到了「查占位符」的那个护栏测试自己**：`tests/guardrail/test_solver_isolation.py`
   必须字面包含 `TODO` 等标记，否则它没法检查它们。

**一个「命中即失败」的检查，只要它的假阳性没被钉住，就迟早被人加白名单绕过去** ——
那才是真正的危险（白名单一加，整个目录成盲区）。所以这里把三条语义钉死：
真占位符必拦、逐行豁免生效、未入库文件不扫。

做法与 `test_import_bans.py` 一致：**跑真实脚本**，不重写它的逻辑。
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.core.config import PROJECT_ROOT

pytestmark = pytest.mark.guardrail

SCRIPT = PROJECT_ROOT / "deploy" / "scripts" / "check_no_placeholders.sh"
#: 探针文件放在被扫描目录里（脚本只扫 backend/ frontend/ tests/）
PROBE = PROJECT_ROOT / "tests" / "unit" / "_placeholder_scan_probe.py"

#: 拼出来而不是字面写 —— 本文件也在扫描范围内
_MARKER = "TO" + "DO"
_ALLOW = "placeholder-scan" + ": allow"


def _run() -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/usr/bin/env", "bash", str(SCRIPT)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _git(*args: str) -> None:
    subprocess.run(  # noqa: S603
        ["/usr/bin/env", "git", *args], cwd=PROJECT_ROOT, capture_output=True, check=False
    )


@pytest.fixture
def probe() -> Iterator[Path]:
    """写一个探针文件，测完无论如何都撤干净（含索引）。"""
    try:
        yield PROBE
    finally:
        _git("rm", "-qf", "--cached", str(PROBE.relative_to(PROJECT_ROOT)))
        PROBE.unlink(missing_ok=True)


def test_script_exists_and_is_wired_into_ci_and_pre_commit() -> None:
    """脚本必须真的被两处门禁调用 —— 否则测它等于测一段死代码。"""
    assert SCRIPT.is_file()
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    hook = (PROJECT_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "check_no_placeholders.sh" in ci
    assert "check_no_placeholders.sh" in hook


def test_clean_tree_passes() -> None:
    """当前仓库必须是干净的（这条同时是铁律 1 的日常门禁）。"""
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_placeholder_in_tracked_file_fails(probe: Path) -> None:
    """真占位符 + 已入索引 → 必须拦下，并点名文件与行号。"""
    probe.write_text(f"x = 1  # {_MARKER} 这是真的半成品\n", encoding="utf-8")
    _git("add", str(probe.relative_to(PROJECT_ROOT)))
    result = _run()
    assert result.returncode == 1
    assert probe.name in result.stdout
    assert "铁律 1" in result.stdout


def test_per_line_allow_mark_is_honoured(probe: Path) -> None:
    """逐行豁免生效 —— 这是「查占位符的代码」自己能过关的唯一途径。"""
    probe.write_text(f"x = 1  # {_MARKER} 说明用  # {_ALLOW}\n", encoding="utf-8")
    _git("add", str(probe.relative_to(PROJECT_ROOT)))
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr


def test_untracked_files_are_not_scanned(probe: Path) -> None:
    """未入库的文件不扫 —— 这条钉住的是 CI 上那个 `.pyc` 假阳性。

    `__pycache__` / 覆盖率产物 / `.data` 都属于这一类。**注意语义是「未入库」而不是
    「被 .gitignore 忽略」**：`git add` 过的新文件在索引里，照样会被扫到
    （上一个用例就是这么触发的），所以 pre-commit 场景不会漏。
    """
    probe.write_text(f"x = 1  # {_MARKER} 未入库的半成品\n", encoding="utf-8")
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr


def test_binary_artifacts_never_break_the_scan(probe: Path) -> None:
    """就算把标记塞进一个已入库的二进制文件，也不该报出来（`grep -I`）。"""
    binary = PROJECT_ROOT / "tests" / "unit" / "_placeholder_scan_probe.bin"
    try:
        binary.write_bytes(b"\x00\x01" + _MARKER.encode() + b"\x00\xff")
        _git("add", "-f", str(binary.relative_to(PROJECT_ROOT)))
        result = _run()
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        _git("rm", "-qf", "--cached", str(binary.relative_to(PROJECT_ROOT)))
        binary.unlink(missing_ok=True)


def test_allow_mark_usage_stays_rare() -> None:
    """豁免是可审计的 —— 全仓库的用处应当屈指可数，多了就是在绕检查。

    当前唯一一处：`tests/guardrail/test_solver_isolation.py` 里定义标记正则的那一行。
    """
    tracked = subprocess.run(
        ["/usr/bin/env", "git", "ls-files", "backend", "frontend", "tests"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    users = []
    for rel in tracked:
        path = PROJECT_ROOT / rel
        if not path.is_file() or path.suffix not in (".py", ".sh"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _ALLOW in text and path.name != Path(__file__).name:
            users.append(rel)
    assert len(users) <= 2, f"逐行豁免被用得太多了，检查是不是在绕门禁：{users}"
