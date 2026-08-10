"""M2-A 的两条窗口级护栏：求解器与校验器的隔离，以及「不留半成品」。

## 为什么这个文件存在

CLAUDE.md 铁律 2 的隔离**有一半是 import-linter 查不出来的**：
「我瞄了一眼 validator 怎么写的然后照着实现」不会留下 import。
`tests/guardrail/test_import_bans.py` 已经在验 import 层面的三条禁令；
这里补两条能自动化的剩余检查：

1. `backend/solver/` 与 `backend/nodes/compile_spec.py` 里**一处都不提 validator**
   （连字符串、注释、类型注解都不许）；
2. 求解链路里没有 `TODO` / `NotImplementedError` 这类半成品（铁律 1）。

「写 validator 的窗口不许打开 backend/solver/」那一半靠窗口纪律，测不出来 ——
这一点在收工报告里如实写明，不假装它被测到了。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.core.config import PROJECT_ROOT

pytestmark = pytest.mark.guardrail

#: 本窗口交付的求解链路
SOLVER_FILES: tuple[Path, ...] = (
    *sorted((PROJECT_ROOT / "backend" / "solver").glob("*.py")),
    PROJECT_ROOT / "backend" / "nodes" / "compile_spec.py",
)

#: 半成品标记（CLAUDE.md 铁律 1 + 收工检查单的 `rg` 命令同一口径）
UNFINISHED = re.compile(r"TODO|FIXME|NotImplementedError|待实现|待补充|后续补")


def test_solver_files_exist() -> None:
    names = {p.name for p in SOLVER_FILES}
    assert {
        "candidates.py",
        "model.py",
        "objective.py",
        "diagnose.py",
        "data.py",
        "solve.py",
        "reschedule.py",
        "compile_spec.py",
    } <= names


@pytest.mark.parametrize("path", SOLVER_FILES, ids=lambda p: p.name)
def test_solver_never_mentions_validator(path: Path) -> None:
    """求解链路里连「validator」这个词都不该出现在代码里。

    模块文档里说明「本模块是 validator/checks.py 的对照面」是允许的 —— 那是在
    交代隔离要求本身。所以只查**非注释、非文档**的行。
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    in_doc = False
    offenders: list[str] = []
    for raw in lines:
        line = raw.strip()
        # 极简的三引号状态机：够用，因为本仓库不写单行三引号以外的花活
        quotes = line.count('"""')
        if quotes % 2 == 1:
            in_doc = not in_doc
            continue
        if in_doc or line.startswith("#") or line.startswith('"""'):
            continue
        if "validator" in line:
            offenders.append(line)
    assert not offenders, f"{path.name} 的代码行提到了 validator：{offenders}"


@pytest.mark.parametrize("path", SOLVER_FILES, ids=lambda p: p.name)
def test_no_unfinished_markers(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    hits = UNFINISHED.findall(text)
    assert not hits, f"{path.name} 里有半成品标记：{set(hits)}"


@pytest.mark.parametrize("path", SOLVER_FILES, ids=lambda p: p.name)
def test_no_baseline_entity_literals(path: Path) -> None:
    """基准数据的编号/机型**不许出现在求解代码里**（CLAUDE.md §11、v6 §5.1.1）。

    机型名 `JL-8`/`JL-9`、机号 `AC10`…、人员编号 `P01`… 一律只能来自上传数据。
    文档与注释里举例说明是允许的（那是在解释语义），所以同样只查代码行。
    """
    forbidden = re.compile(r"\bJL-[89]\b|\"AC\d{2}\"|'AC\d{2}'|\"P0\d\"|'P0\d'")
    offenders = []
    in_doc = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.count('"""') % 2 == 1:
            in_doc = not in_doc
            continue
        if in_doc or line.startswith("#"):
            continue
        if forbidden.search(line):
            offenders.append(line)
    assert not offenders, f"{path.name} 把基准实体写成了代码常量：{offenders}"


def test_solver_does_not_import_validator_module() -> None:
    """再补一层动态检查：导入求解链路后，`sys.modules` 里不该出现 validator。"""
    import sys

    for name in list(sys.modules):
        if name.startswith("backend.validator"):
            del sys.modules[name]
    import backend.solver.candidates
    import backend.solver.diagnose
    import backend.solver.model
    import backend.solver.objective
    import backend.solver.reschedule
    import backend.solver.solve  # noqa: F401

    assert not [n for n in sys.modules if n.startswith("backend.validator")]
