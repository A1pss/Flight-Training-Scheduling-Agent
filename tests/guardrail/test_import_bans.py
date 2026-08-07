"""三条依赖禁令的护栏测试（CLAUDE.md 铁律 2/3，v6 附录 A、§12.5.3 S5、§12.5.4 E2）。

**这组测试是「注入一条违规 import，断言 lint-imports 真的会拦下来」**——
出口标准明确要求验证禁令确实生效，而不是只看它「跑过了」。一个配错的
contract（写错模块名、source 写空）同样会「全绿」，那种绿是假的。

做法：把违规 import 写进临时模块，跑真实的 `lint-imports`，断言它非零退出
并点名该条 contract；测完立刻删除。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.core.config import PROJECT_ROOT

pytestmark = pytest.mark.guardrail

#: 注入点 → (违规 import 语句, 预期被触发的 contract 名)
INJECTIONS: list[tuple[str, str, str]] = [
    (
        "backend/validator/_guardrail_probe.py",
        "from backend.solver import candidates  # noqa: F401",
        "禁令一",
    ),
    (
        "backend/solver/_guardrail_probe.py",
        "from backend.skills_loader import loader  # noqa: F401",
        "禁令二",
    ),
    (
        "backend/report/_guardrail_probe.py",
        "import httpx  # noqa: F401",
        "禁令三",
    ),
]


def _lint_imports_bin() -> str:
    """定位 `lint-imports` 可执行文件（与当前解释器同一环境）。"""
    candidate = Path(sys.executable).parent / "lint-imports"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("lint-imports")
    if found is None:  # pragma: no cover —— 依赖缺失时才会走到
        pytest.skip("环境中没有 lint-imports，跳过依赖禁令护栏测试")
    return found


def _lint_imports() -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [_lint_imports_bin()],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def clean_probes() -> Iterator[None]:
    """确保探针文件在测试前后都不存在——绝不把违规代码留在仓库里。"""
    _remove_probes()
    yield
    _remove_probes()


def _remove_probes() -> None:
    for rel, _stmt, _name in INJECTIONS:
        (PROJECT_ROOT / rel).unlink(missing_ok=True)
    for pkg in ("backend/validator", "backend/solver", "backend/report", "backend/skills_loader"):
        shutil.rmtree(PROJECT_ROOT / pkg / "__pycache__", ignore_errors=True)


def test_baseline_all_contracts_kept(clean_probes: None) -> None:
    """基线：无注入时三条 contract 全部 KEPT。"""
    result = _lint_imports()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 kept, 0 broken" in result.stdout


@pytest.mark.parametrize(("rel", "stmt", "contract"), INJECTIONS, ids=[i[2] for i in INJECTIONS])
def test_injected_violation_is_caught(
    clean_probes: None, rel: str, stmt: str, contract: str
) -> None:
    """注入一条违规 import → lint-imports 必须失败并点名对应 contract。"""
    target = PROJECT_ROOT / rel
    # 被 import 的模块必须真实存在，否则 import-linter 会报「模块不存在」
    # 而不是「契约被破坏」——那样测的就不是禁令本身了。
    stub_targets = [
        PROJECT_ROOT / "backend/solver/candidates.py",
        PROJECT_ROOT / "backend/skills_loader/loader.py",
    ]
    created: list[Path] = []
    for stub in stub_targets:
        if not stub.exists():
            stub.write_text('"""护栏测试临时桩，测完即删。"""\n', encoding="utf-8")
            created.append(stub)

    target.write_text(f'"""护栏测试注入的违规 import，测完即删。"""\n\n{stmt}\n', encoding="utf-8")
    try:
        result = _lint_imports()
        assert result.returncode != 0, f"违规 import 未被拦下：\n{result.stdout}"
        assert "broken" in result.stdout.lower()
        assert contract in result.stdout, f"未点名 {contract}：\n{result.stdout}"
    finally:
        target.unlink(missing_ok=True)
        for stub in created:
            stub.unlink(missing_ok=True)


def test_no_probe_files_left_behind() -> None:
    """收尾断言：仓库里不许残留任何护栏探针文件。"""
    for rel, _stmt, _name in INJECTIONS:
        assert not (PROJECT_ROOT / rel).exists(), f"{rel} 残留未清理"
