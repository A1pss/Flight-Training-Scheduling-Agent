"""M2-B 的窗口级护栏：校验器对求解器的单向隔离（CLAUDE.md 铁律 2）。

`tests/guardrail/test_import_bans.py` 已经在 import 层面验了三条禁令（注入违规
import → 断言 `lint-imports` 真的拦下来），`tests/guardrail/test_solver_isolation.py`
验的是求解侧「一处都不提 validator」。这里补上镜像的一半：

1. `backend/validator/` 里**一处都不提 solver / ortools**（连字符串、注释、类型
   注解都不许）；
2. 导入整个 `backend.validator` 之后，`ortools` **不在 `sys.modules` 里** ——
   间接依赖同样算数。

⚠️ **仍有一半测不出来**：「我打开 `backend/solver/` 瞄了一眼它怎么写、然后照着
实现」不留下任何 import，也不留下任何字符串。那一半靠窗口纪律，收工报告里如实
写明，不假装它被测到了。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend.core.config import PROJECT_ROOT

pytestmark = pytest.mark.guardrail

VALIDATOR_DIR = PROJECT_ROOT / "backend" / "validator"
VALIDATOR_FILES: tuple[Path, ...] = tuple(sorted(VALIDATOR_DIR.glob("*.py")))

#: CLAUDE.md §10 收工检查单里那条 `rg` 的等价形态
FORBIDDEN = re.compile(r"ortools|from backend\.solver|import solver|backend/solver")


def test_validator_files_exist() -> None:
    assert {p.name for p in VALIDATOR_FILES} >= {
        "checks.py",
        "schema.py",
        "workbook.py",
        "context.py",
    }


@pytest.mark.parametrize("path", VALIDATOR_FILES, ids=lambda p: p.name)
def test_validator_never_mentions_solver_or_ortools(path: Path) -> None:
    hits = [
        f"{path.name}:{n}: {line.strip()}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if FORBIDDEN.search(line)
    ]
    assert hits == [], "校验器里出现了求解器的痕迹：\n" + "\n".join(hits)


def test_exit_criterion_ripgrep_is_empty() -> None:
    """出口标准的原命令：`rg -n "ortools|from backend.solver|import solver" backend/validator/`。"""
    rg = shutil.which("rg")
    if rg is None:  # 环境里没有 rg 时不静默放过：上面的逐行扫描覆盖同一断言
        pytest.skip(
            "环境里没有 rg，同一断言已由 test_validator_never_mentions_solver_or_ortools 覆盖"
        )
    proc = subprocess.run(  # noqa: S603
        [rg, "-n", "ortools|from backend.solver|import solver", str(VALIDATOR_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, f"命中：\n{proc.stdout}"


def test_importing_validator_does_not_pull_in_ortools() -> None:
    """间接依赖也算：整个包导完之后 `ortools` 不许出现在 `sys.modules` 里。"""
    code = (
        "import importlib, sys;"
        "[importlib.import_module(f'backend.validator.{m}')"
        " for m in ('checks', 'schema', 'workbook', 'context')];"
        "leaked = [m for m in sys.modules if m.startswith('ortools') or m == 'backend.solver'];"
        "print(leaked)"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=True,
    )
    assert proc.stdout.strip() == "[]", proc.stdout
