"""编排层的权限矩阵护栏（v6 §7.7.2 / §12.5.3 S5 / CLAUDE.md 铁律 4）。

M4-A 已经逐条测过 Harness 侧的三层拦截（30 条越权场景）。**本文件测的是
编排层这一侧**：图里真实存在的组件与节点，能不能绕过那三层。

三个断言方向：

1. **六个确定性节点在 ACL 里物理不可达** —— 它们不在工具目录里，注册期、
   装配期、调用期三处都抛；
2. **各组件实际暴露的工具是其 ACL 行的子集** —— Planner 拿的九个工具、
   Diagnosis 拿的四个工具，都得对得上矩阵；
3. **v6 §12.5.3 S5 的静态检查** —— `solver/`、`nodes/`、`validator/` 里没有
   `skills_loader` 的 import 路径；`validator/` 里没有 `solver` 的 import 路径。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.agents.diagnosis import DIAGNOSIS_AGENT, DIAGNOSIS_TOOLS
from backend.components.explain import EXPLAIN_AGENT
from backend.components.extract import EXTRACT_AGENT
from backend.core.config import PROJECT_ROOT
from backend.core.errors import ArchitecturalBanError, ToolPermissionDeniedError
from backend.harness import ACL_MATRIX, TOOL_CATALOG, ToolACL
from backend.harness.acl import WRITE_TOOL_ALLOWLIST
from backend.nodes import DETERMINISTIC_NODE_NAMES
from backend.planner.intent import PLANNER_AGENT, PLANNER_TOOLS
from backend.routing.classify import ROUTE_AGENT

pytestmark = pytest.mark.guardrail

ACL = ToolACL()

#: 图里真实存在的六个 LLM 组件（§7.7.2 权限矩阵的六列）
COMPONENTS = ("route", "planner", "extract", "knowledge", "diagnosis", "explain")


# ─────────────────────────────────────────────────────────────────────
# ① 六个确定性节点物理不可达
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("node", sorted(DETERMINISTIC_NODE_NAMES))
def test_deterministic_nodes_are_not_in_the_tool_catalog(node: str) -> None:
    """不注册为工具 —— **物理上就调不到**（§7.7.2 最后两行）。"""
    assert node not in TOOL_CATALOG


@pytest.mark.parametrize("component", COMPONENTS)
@pytest.mark.parametrize("node", sorted(DETERMINISTIC_NODE_NAMES))
def test_no_component_can_call_a_deterministic_node(component: str, node: str) -> None:
    """36 条：六个组件 × 六个节点，全部拦下且升为架构级禁令。"""
    with pytest.raises(ArchitecturalBanError):
        ACL.assert_allowed(component, node)  # type: ignore[arg-type]


def test_architectural_ban_is_fts_4004_critical() -> None:
    """`Z-12`：越权单列 FTS-4004；踩架构禁令时升为 CRITICAL、不可重试。"""
    with pytest.raises(ArchitecturalBanError) as caught:
        ACL.assert_allowed("planner", "solve")
    error = caught.value
    assert error.code.value == "FTS-4004"
    assert error.severity == "CRITICAL"
    assert error.retryable is False


def test_planner_calling_solve_is_denied() -> None:
    """出口标准逐条：Planner 调 `solve` 被拦截。"""
    assert not ACL.is_allowed("planner", "solve")


def test_knowledge_calling_memory_write_is_denied() -> None:
    """出口标准逐条：Knowledge 调 `memory.write` 被拦截（只有 extract 有权）。"""
    with pytest.raises(ToolPermissionDeniedError):
        ACL.assert_allowed("knowledge", "memory.write")
    assert ACL.is_allowed("extract", "memory.write")


def test_memory_write_is_the_only_write_tool() -> None:
    assert frozenset({"memory.write"}) == WRITE_TOOL_ALLOWLIST
    assert {name for name, spec in TOOL_CATALOG.items() if spec.writes} == {"memory.write"}


def test_advance_progress_is_not_a_tool_at_all() -> None:
    """`memory.advance_progress` 不属于任何 LLM 组件 —— 它是 `commit_plan_node` 的职责。"""
    assert "memory.advance_progress" not in TOOL_CATALOG


# ─────────────────────────────────────────────────────────────────────
# ② 组件暴露的工具是 ACL 行的子集
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "agent",
    [ROUTE_AGENT, PLANNER_AGENT, EXTRACT_AGENT, EXPLAIN_AGENT, DIAGNOSIS_AGENT],
    ids=lambda a: a.name,
)
def test_agent_specs_expose_only_allowed_tools(agent: object) -> None:
    spec = agent  # type: ignore[assignment]
    ACL.assert_exposable(spec.name, spec.tools)  # type: ignore[attr-defined]


def test_planner_tools_match_the_matrix_row() -> None:
    assert set(PLANNER_TOOLS) <= ACL_MATRIX["planner"]


def test_diagnosis_tools_are_the_four_from_v6() -> None:
    assert set(DIAGNOSIS_TOOLS) == {
        "min_conflict_set",
        "blame_chain",
        "probe_solve",
        "rank_relaxations",
    }
    assert set(DIAGNOSIS_TOOLS) <= ACL_MATRIX["diagnosis"]


def test_exposing_a_tool_outside_the_row_is_rejected() -> None:
    from backend.harness import AgentSpec

    with pytest.raises(ToolPermissionDeniedError):
        ACL.assert_exposable("route", AgentSpec(name="route", tools=("probe_solve",)).tools)


def test_probe_solve_is_diagnosis_only() -> None:
    """§7.7.2 的唯一例外，且只有 Diagnosis 能用。"""
    assert ACL.components_allowed("probe_solve") == frozenset({"diagnosis"})


# ─────────────────────────────────────────────────────────────────────
# ③ §12.5.3 S5：静态 import 检查
# ─────────────────────────────────────────────────────────────────────
def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _python_files(*relatives: str) -> list[Path]:
    files: list[Path] = []
    for relative in relatives:
        files.extend(sorted((PROJECT_ROOT / relative).rglob("*.py")))
    return files


def test_s5_solver_nodes_validator_never_import_skills_loader() -> None:
    """S5 前半：三个包里没有 `skills_loader` 的 import 路径。"""
    offenders = [
        str(path.relative_to(PROJECT_ROOT))
        for path in _python_files("backend/solver", "backend/nodes", "backend/validator")
        if any(m.startswith("backend.skills_loader") for m in _imported_modules(path))
    ]
    assert offenders == [], f"这些文件 import 了 skills_loader：{offenders}"


def test_s5_validator_never_imports_solver() -> None:
    """S5 后半：`validator/` 里没有 `solver` 的 import 路径（铁律 2）。"""
    offenders = [
        str(path.relative_to(PROJECT_ROOT))
        for path in _python_files("backend/validator")
        if any(m.startswith("backend.solver") for m in _imported_modules(path))
    ]
    assert offenders == [], f"这些文件 import 了 solver：{offenders}"


def test_nodes_do_not_reach_skills_loader_through_the_graph_package() -> None:
    """间接读到也是读到：`backend.nodes → backend.graph` 那条路必须是干净的。

    `backend/graph/__init__.py` 只 re-export 状态、事件与 Store；
    `build_graph`（它 import 了 `skills_loader`）不在其中。谁哪天顺手把它加进
    `__init__` 的 `__all__`，这条会红。
    """
    init = PROJECT_ROOT / "backend/graph/__init__.py"
    imported = _imported_modules(init)
    assert "backend.graph.graph" not in imported
    assert not any(m.startswith("backend.skills_loader") for m in imported)


def test_components_may_read_skills_that_is_the_point() -> None:
    """反面对照：读知识层的那一侧**应该**能 import 它，否则测试就成了空转。"""
    imported = _imported_modules(PROJECT_ROOT / "backend/components/extract.py")
    assert any(m.startswith("backend.skills_loader") for m in imported)
