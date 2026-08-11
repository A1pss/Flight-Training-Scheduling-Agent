"""铁律 1 的执行性检查本身有没有用（`deploy/scripts/check_no_placeholders.sh`）。

## 为什么要给一个 grep 脚本写测试

M2-A 的 CI 上它连着炸了**三次**，三次都不是「代码里真有半成品」：

1. **扫到了 `__pycache__/*.pyc`**：CI 里 pytest 步骤先跑、留下 `.pyc`，而脚本扫的是
   **整个工作区**，`grep -r` 又不跳过二进制 → `grep: ....pyc: binary file matches`。
   任何 `.pyc` 里恰好带上那几个字节都能让 CI 红。
2. **扫到了「查占位符」的那个护栏测试自己**：`tests/guardrail/test_solver_isolation.py`
   必须字面写出那几个标记，否则它没法检查它们。
3. **扫到了本文件**：第 1、2 条的修法是「只扫 `git ls-files`（入库文件）」，
   那造出一道**「未入库 vs 已入库」的行为悬崖** —— 本文件写完时还没入库，脚本看不见它，
   门禁全绿；一 `git commit` 推上去，CI 看到的是入库版本，于是红。
   **验证时所处的状态与 CI 所处的状态不同**，这和第 2 条其实是同一个错。

第 3 条之后脚本改成扫 **`--cached --others --exclude-standard`**（入库的 + 未入库但没被
gitignore 的），本机与 CI 从此看同一批文件，且新文件在**提交之前**就会被拦下。

## 这些用例钉的是什么

**一个「命中即失败」的检查，只要它的假阳性没被钉住，就迟早被人加白名单绕过去** ——
那才是真正的危险（白名单一加，整个目录成盲区）。所以这里把四条语义钉死：
真占位符必拦（**未入库也拦**）、逐行豁免生效、被 gitignore 的产物不扫、二进制不扫。

做法与 `test_import_bans.py` 一致：**跑真实脚本**，不重写它的逻辑。
本文件**不碰 git 索引** —— 早先为了让探针「入库」而调 `git add`，那会污染开发者的
暂存区（测试崩在中途就留下一个已 add 的探针文件）。`--others` 口径让这一步彻底没必要。
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.core.config import PROJECT_ROOT

pytestmark = pytest.mark.guardrail

SCRIPT = PROJECT_ROOT / "deploy" / "scripts" / "check_no_placeholders.sh"
#: 探针放在被扫描目录里（脚本只扫 backend/ frontend/ tests/）
PROBE = PROJECT_ROOT / "tests" / "unit" / "_placeholder_scan_probe.py"
#: 被 gitignore 的目录 —— 用来验证产物不进扫描集
IGNORED_PROBE = PROJECT_ROOT / "tests" / "unit" / "__pycache__" / "_scan_probe_ignored.py"

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


@pytest.fixture
def probe() -> Iterator[Path]:
    """写一个探针文件，测完无论如何都删掉（不入索引，见模块文档）。"""
    try:
        yield PROBE
    finally:
        PROBE.unlink(missing_ok=True)


def test_script_is_wired_into_ci_and_pre_commit() -> None:
    """脚本必须真的被两处门禁调用 —— 否则测它等于测一段死代码。"""
    assert SCRIPT.is_file()
    ci = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    hook = (PROJECT_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "check_no_placeholders.sh" in ci
    assert "check_no_placeholders.sh" in hook


def test_clean_tree_passes() -> None:
    """当前仓库必须是干净的 —— 这条同时就是铁律 1 的日常门禁。

    ⚠️ 它红了通常意味着**某处新写的文字里字面出现了那几个标记**，
    而不是「真有半成品」。先看脚本报的行号再判断。
    """
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_placeholder_is_caught_even_when_untracked(probe: Path) -> None:
    """真占位符必拦，**未入库也拦**。

    这条钉的正是 CI 第三次失败：只扫入库文件时，新文件在提交前是隐形的，
    于是「本机绿、推上去红」。现在提交之前就会被拦下。
    """
    probe.write_text(f"x = 1  # {_MARKER} 这是真的半成品\n", encoding="utf-8")
    result = _run()
    assert result.returncode == 1
    assert probe.name in result.stdout
    assert "铁律 1" in result.stdout


def test_per_line_allow_mark_is_honoured(probe: Path) -> None:
    """逐行豁免生效 —— 这是「查占位符的代码」自己能过关的唯一途径。"""
    probe.write_text(f"x = 1  # {_MARKER} 说明用  # {_ALLOW}\n", encoding="utf-8")
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr


def test_allow_mark_must_be_on_the_same_physical_line(probe: Path) -> None:
    """豁免是**逐行**的：注释挪到下一行就不算。

    这条不只是抠语义 —— `ruff format` 会把过长的一行折开、把行尾注释留在收尾的
    `)` 那一行，含标记的那一行于是失去豁免。踩过一次，故钉住。
    """
    probe.write_text(f"x = (\n    1  # {_MARKER} 半成品\n)  # {_ALLOW}\n", encoding="utf-8")
    result = _run()
    assert result.returncode == 1


def test_gitignored_artifacts_are_not_scanned() -> None:
    """被 gitignore 的产物不扫 —— 这条钉住 CI 第一次失败的那个 `.pyc` 假阳性。

    `__pycache__/` / `.data/` / 覆盖率产物都属于这一类。注意口径是
    **「被 gitignore」而不是「未入库」**：未入库的新文件照样扫（上面那条用例）。
    """
    IGNORED_PROBE.parent.mkdir(parents=True, exist_ok=True)
    try:
        IGNORED_PROBE.write_text(f"x = 1  # {_MARKER} 产物里的半成品\n", encoding="utf-8")
        # 前提自证：这个路径确实被 git 忽略，否则本用例什么也没验到
        ignored = subprocess.run(  # noqa: S603
            ["/usr/bin/env", "git", "check-ignore", str(IGNORED_PROBE)],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert ignored.returncode == 0, "__pycache__ 居然没被 gitignore，本用例前提不成立"
        result = _run()
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        IGNORED_PROBE.unlink(missing_ok=True)


def test_binary_files_never_break_the_scan() -> None:
    """二进制文件里的标记不该被报出来（`grep -I`）。"""
    binary = PROJECT_ROOT / "tests" / "unit" / "_placeholder_scan_probe.bin"
    try:
        binary.write_bytes(b"\x00\x01" + _MARKER.encode() + b"\x00\xff")
        result = _run()
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        binary.unlink(missing_ok=True)


def test_allow_mark_usage_stays_rare() -> None:
    """豁免可审计 —— 全仓库的用处应当屈指可数，多了就是在绕检查。

    当前唯一一处：`tests/guardrail/test_solver_isolation.py` 里定义标记正则的那一行。
    """
    listing = subprocess.run(
        [
            "/usr/bin/env",
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "backend",
            "frontend",
            "tests",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    users = []
    for rel in listing.stdout.split():
        path = PROJECT_ROOT / rel
        if not path.is_file() or path.suffix not in (".py", ".sh"):
            continue
        if path.name == Path(__file__).name:
            continue
        if _ALLOW in path.read_text(encoding="utf-8", errors="replace"):
            users.append(rel)
    assert len(users) <= 2, f"逐行豁免被用得太多了，检查是不是在绕门禁：{users}"
