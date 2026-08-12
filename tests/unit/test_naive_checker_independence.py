"""第三方 naive checker 的**独立性护栏**（v6 §12.3 度量方式第 2 条）。

`tests/naive_checker.py` 的全部价值建立在「它不是主校验器的一份拷贝」上。这条
性质里能被自动测到的那一半在这里：

- 不 import `backend.validator.checks` / `schema` / `workbook`（判定逻辑）
- 不 import `backend.solver` 任何模块、不 import `ortools`
- 14 条 check 一条不少、无半成品标记
- 不把基准数据（`P01`/`AC10`/`JL-8`/`SAA`/`RWY-1`/8 人 8 机）写成代码常量

**测不到的那一半**（「我打开 checks.py 瞄了一眼再照着写」）只能靠窗口纪律，
`reports/M2C_收工报告.md` §2 有如实交代：naive checker 的 14 条是先写完、
先跑完对拍，之后才因为 S-11 裁定去读过 `check_c13`。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from backend.core.config import PROJECT_ROOT
from tests import naive_checker

NAIVE_PATH: Path = PROJECT_ROOT / "tests" / "naive_checker.py"

#: 判定逻辑所在的模块 —— naive checker 的源码里一个都不许出现
FORBIDDEN_MODULES: tuple[str, ...] = (
    "backend.validator.checks",
    "backend.validator.schema",
    "backend.validator.workbook",
    "backend.solver",
    "ortools",
)

#: 能在**运行期**查的那一部分。
#:
#: ⚠️ `backend.validator.checks` 查不了运行期：naive checker 要用
#: `backend.validator.context`（事实视图），而 import 任何子模块都会先执行包的
#: `__init__.py`，那里就把 `checks` 拉进来了。**这不是 naive checker 引用了判定
#: 逻辑**，是 Python 的包语义。所以对 `validator.*` 只做源码静态检查
#: （`test_naive_checker_imports_no_decision_logic`），运行期只查求解器与 ortools
#: —— 那两个是真的一行都不该被拉进来。
RUNTIME_FORBIDDEN: tuple[str, ...] = ("backend.solver", "ortools")

#: 基准数据集的取值。它们描述的是「数据长什么样」，不是系统上限
#: （CLAUDE.md §11、v6 §5.1.1），一个都不许写成代码常量。
BASELINE_LITERALS: tuple[str, ...] = (
    r"\bP0\d\b",
    r"\bAC\d{2}\b",
    r"JL-8",
    r"JL-9",
    r"\bSAA\b",
    r"\bSAB\b",
    r"\bIFR\b",
    r"RWY-1",
    r"RWY-2",
    r"mission[A-H]-\d",
)

#: 半成品标记。本行**必须**字面包含这些 token（它就是查它们的那段代码），
#: 故按 `check_no_placeholders.sh` 的逐行豁免机制显式放行 —— 与
#: `tests/guardrail/test_solver_isolation.py` 同一写法。
#: ⚠️ 写成单行常量而不是内联进 `re.compile(...)`：M2-A 踩过一次
#: 「逐行豁免被 formatter 撕成两行、豁免注释与 token 分家」的坑。
_TOKENS = r"TODO|FIXME|NotImplementedError|待实现|待补充|后续补"  # placeholder-scan: allow
UNFINISHED = re.compile(_TOKENS)


def _source() -> str:
    return NAIVE_PATH.read_text(encoding="utf-8")


def _code_lines() -> list[str]:
    """去掉文档字符串与注释后的代码行 —— 说明文字里出现基准编号是正常的。"""
    text = _source()
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    return [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


def test_naive_checker_imports_no_decision_logic() -> None:
    """静态：源码里不出现被禁模块。"""
    source = _source()
    for module in FORBIDDEN_MODULES:
        assert f"import {module}" not in source, module
        assert f"from {module}" not in source, module


def test_naive_checker_pulls_in_no_solver_at_runtime() -> None:
    """动态：import 之后 `sys.modules` 里不出现求解器与 ortools（间接依赖也算）。

    `backend.validator.context` 是**允许**的 —— 它是事实视图（ORM 行 → dataclass），
    与 `backend.core.ruleset`（YAML → 对象）同一性质，不含任何一条规则的判定。
    为什么这里不查 `validator.checks`，见 :data:`RUNTIME_FORBIDDEN` 的说明。

    ⚠️ **必须在干净的子进程里查。** 直接看当前进程的 `sys.modules` 只在单独跑本文件
    时成立：全量 pytest 会话里，`tests/property/` 早就把 `backend.solver` import 进来了，
    那不是 naive checker 拉的。这条一开始就是那么写的，跑单文件绿、跑全量红 ——
    与 M1「本地手工迁移过所以全绿、CI 全新库所以全红」是同一类错：
    **验证时的进程状态必须与被验证的命题一致**。
    """
    probe = (
        "import sys, json;"
        "import tests.naive_checker as nc;"
        "assert nc.naive_check_all is not None;"
        "print(json.dumps(sorted(sys.modules)))"
    )
    completed = subprocess.run(  # noqa: S603 —— 参数是本文件写死的常量，无外部输入
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=True,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT), "LLM_PROVIDER": "mock"},
    )
    loaded = set(json.loads(completed.stdout))
    for module in RUNTIME_FORBIDDEN:
        offenders = sorted(m for m in loaded if m == module or m.startswith(f"{module}."))
        assert not offenders, f"{module} 被间接拉进来了：{offenders}"
    assert "backend.validator.context" in loaded, "事实视图是允许且必须的"


def test_naive_checker_implements_all_fourteen_rules() -> None:
    for i in range(1, 15):
        name = f"check_c{i:02d}"
        assert hasattr(naive_checker, name), name
        assert callable(getattr(naive_checker, name))
    assert set(naive_checker.RULE_TITLES) == {f"C{i:02d}" for i in range(1, 15)}


def test_naive_checker_has_no_placeholders() -> None:
    hits = [line for line in _code_lines() if UNFINISHED.search(line)]
    assert hits == [], hits


def test_naive_checker_hardcodes_no_baseline_values() -> None:
    """基准数据集的编号/机型一个都不许写死。"""
    offenders: list[str] = []
    for line in _code_lines():
        for pattern in BASELINE_LITERALS:
            if re.search(pattern, line):
                offenders.append(f"{pattern} → {line.strip()}")
    assert offenders == [], offenders


def test_naive_checker_uses_pandas() -> None:
    """v6 §12.3 明写「用 pandas 写一版」—— 换实现要先改文档。"""
    assert "import pandas" in _source()
    assert naive_checker.sortie_frame is not None
    assert naive_checker.crew_frame is not None


@pytest.mark.parametrize("rule_id", [f"C{i:02d}" for i in range(1, 15)])
def test_every_rule_has_a_title(rule_id: str) -> None:
    assert naive_checker.RULE_TITLES[rule_id]


def test_req_max_formula_is_independent_of_the_ruleset_table() -> None:
    """`req_max = ceil(7 / freq_days)` 由本文件**自己算**，不读 YAML 里那份抄录。"""
    assert naive_checker.req_max_for(3) == 3
    assert naive_checker.req_max_for(7) == 1
    assert naive_checker.req_max_for(14) == 1
    assert naive_checker.req_max_for(1) == 7
    assert naive_checker.req_max_for(2) == 4
