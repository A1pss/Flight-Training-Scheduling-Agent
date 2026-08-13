"""工具权限矩阵的**运行时**强制（v6 §7.7.2）。

矩阵原样抄自 v6 §7.7.2，一行一行对得上；改任何一格都要先改设计方案。

三层拦截，缺一不可：

1. **注册期**：六个确定性节点、以及除 `memory.write` 外的任何写工具，
   一旦出现在工具表里就抛 `ArchitecturalBanError` —— 它们不该存在，
   而不是「存在但没人有权调」。
2. **装配期**：`AgentSpec.tools` 必须是该组件 ACL 行的子集，超出即抛。
   模型连见都见不到越权工具的 schema。
3. **调用期**：模型返回的每个 tool call 逐个过 `assert_allowed`。
   这一层才是真正兜底的那层 —— 前两层都可能被将来的重构绕开，
   而 §7.7.2 写的是「**运行时**拦截，不依赖提示词自觉」。

> **为什么装配期拦了还要调用期再拦一次**：模型可以凭空编出一个没给它的工具名
> （14B 上这不是罕见事）。没有第三层，那次调用会一路走到执行器才因为「找不到
> handler」失败 —— 那是 `KeyError`，不是越权拦截，统计与日志全错位。
"""

from __future__ import annotations

from typing import Final

from backend.core.errors import ArchitecturalBanError, ToolPermissionDeniedError
from backend.harness.tools import TOOL_CATALOG
from backend.harness.types import (
    ALL_COMPONENTS,
    COMPONENT_LABELS,
    ComponentName,
    ToolSpec,
)

#: v6 §7.7.2 最后一行之上的那一行：**六个确定性节点**，一个都不许注册为工具。
#: v5.2 只列了前三个，v6 补全为六个（CLAUDE.md 铁律 4）。
FORBIDDEN_NODES: Final[frozenset[str]] = frozenset(
    {
        "solve",
        "validate",
        "compile_spec",
        "resume_guard",
        "human_gate",
        "commit_plan",
    }
)

#: 唯一允许写数据的工具（§7.7.2 最后一行「任何数据写入（除 memory）✖」）。
#: 注意 `memory.advance_progress` **不在**这里 —— 进度推进是 `commit_plan_node`
#: 的职责，发生在人工确认之后，不属于任何 LLM 组件。
WRITE_TOOL_ALLOWLIST: Final[frozenset[str]] = frozenset({"memory.write"})

#: 只读探针：§7.7.2 的唯一例外，且只有 Diagnosis 能用（§3.9.2 独立预算池）。
PROBE_TOOL: Final[str] = "probe_solve"


def _rows() -> tuple[tuple[tuple[str, ...], tuple[ComponentName, ...]], ...]:
    """v6 §7.7.2 的表格，逐行照抄。"""
    return (
        (("resolve_person", "resolve_aircraft", "resolve_week"), ("route", "planner")),
        (("ask_user", "escalate"), ("route", "planner")),
        (("estimate_scope", "assess_disruption", "propose_solve_intent"), ("planner",)),
        (("translate_revision", "check_authority"), ("planner",)),
        (
            (
                "classify_doc",
                "parse_personnel",
                "parse_aircraft",
                "parse_missions",
                "parse_rules",
                "diff_snapshot",
                "propose_change",
            ),
            ("extract",),
        ),
        (("propose_rule_dsl",), ("extract",)),
        (
            ("sql_query", "prereq_cte", "vector_search", "bm25_search", "rrf_fuse", "rerank"),
            ("extract", "knowledge", "diagnosis", "explain"),
        ),
        (("memory.search",), ALL_COMPONENTS),
        (("memory.write",), ("extract",)),
        (
            ("min_conflict_set", "blame_chain", PROBE_TOOL, "rank_relaxations"),
            ("diagnosis",),
        ),
        (("render_workbook", "compose_report", "verify_claim"), ("explain",)),
    )


def _build_matrix() -> dict[ComponentName, frozenset[str]]:
    acc: dict[ComponentName, set[str]] = {c: set() for c in ALL_COMPONENTS}
    for tools, components in _rows():
        for tool in tools:
            if tool not in TOOL_CATALOG:
                raise ArchitecturalBanError(
                    f"权限矩阵里的 {tool!r} 不在工具目录中，矩阵与目录已漂移",
                    details={"tool": tool},
                )
            for component in components:
                acc[component].add(tool)
    return {component: frozenset(tools) for component, tools in acc.items()}


#: 组件 → 可调用工具集合。
ACL_MATRIX: Final[dict[ComponentName, frozenset[str]]] = _build_matrix()


class ToolACL:
    """权限矩阵的运行时执行器。"""

    def __init__(self, matrix: dict[ComponentName, frozenset[str]] | None = None) -> None:
        self._matrix = dict(matrix or ACL_MATRIX)

    # ── 注册期 ───────────────────────────────────────────────────────
    @staticmethod
    def assert_registrable(spec: ToolSpec) -> None:
        """注册一个工具前的架构级体检。"""
        if spec.name in FORBIDDEN_NODES:
            raise ArchitecturalBanError(
                f"{spec.name!r} 是确定性节点，禁止注册为任何 LLM 组件的工具"
                "（v6 §7.7.2 最后两行 / CLAUDE.md 铁律 4）",
                details={"tool": spec.name, "forbidden_nodes": sorted(FORBIDDEN_NODES)},
                suggestions=["确定性节点由图直接调用，不经 Harness、不进工具表"],
            )
        if spec.writes and spec.name not in WRITE_TOOL_ALLOWLIST:
            raise ArchitecturalBanError(
                f"{spec.name!r} 声明了写数据，但除 {sorted(WRITE_TOOL_ALLOWLIST)} 外"
                "任何数据写入都被禁止（v6 §7.7.2 最后一行）",
                details={"tool": spec.name},
            )

    # ── 装配期 ───────────────────────────────────────────────────────
    def assert_exposable(self, component: ComponentName, tools: tuple[str, ...]) -> None:
        """核对要暴露给模型的工具子集。少给可以，多给不行。"""
        for tool in tools:
            self.assert_allowed(component, tool)

    # ── 调用期 ───────────────────────────────────────────────────────
    def assert_allowed(self, component: ComponentName, tool: str) -> None:
        """放行则静默返回；越权即抛（§7.7.2「越权即抛」）。"""
        if tool in FORBIDDEN_NODES:
            raise ArchitecturalBanError(
                f"组件「{COMPONENT_LABELS.get(component, component)}」试图调用确定性节点 "
                f"{tool!r} —— 六个确定性节点不注册为工具，物理上就调不到",
                details={"component": component, "tool": tool},
            )

        spec = TOOL_CATALOG.get(tool)
        if spec is None:
            raise ToolPermissionDeniedError(
                f"未知工具 {tool!r}：不在工具目录中",
                details={
                    "component": component,
                    "tool": tool,
                    "allowed": sorted(self.allowed_tools(component)),
                },
                suggestions=["只能调用本次请求给出的工具表里的名字，不要自行编造"],
            )

        if spec.writes and tool not in WRITE_TOOL_ALLOWLIST:
            raise ArchitecturalBanError(
                f"{tool!r} 会写数据，除 memory 外任何数据写入禁止",
                details={"component": component, "tool": tool},
            )

        if tool not in self._matrix.get(component, frozenset()):
            raise ToolPermissionDeniedError(
                f"组件「{COMPONENT_LABELS.get(component, component)}」无权调用 {tool!r}",
                details={
                    "component": component,
                    "tool": tool,
                    "allowed": sorted(self.allowed_tools(component)),
                },
                suggestions=[f"该工具只对 {sorted(self.components_allowed(tool))} 开放"],
            )

    # ── 查询 ─────────────────────────────────────────────────────────
    def allowed_tools(self, component: ComponentName) -> frozenset[str]:
        return self._matrix.get(component, frozenset())

    def components_allowed(self, tool: str) -> frozenset[ComponentName]:
        return frozenset(c for c, tools in self._matrix.items() if tool in tools)

    def is_allowed(self, component: ComponentName, tool: str) -> bool:
        try:
            self.assert_allowed(component, tool)
        except ToolPermissionDeniedError:
            return False
        return True


#: 进程级默认实例（矩阵是常量，无状态，共享安全）。
DEFAULT_ACL: Final[ToolACL] = ToolACL()


__all__ = [
    "ACL_MATRIX",
    "DEFAULT_ACL",
    "FORBIDDEN_NODES",
    "PROBE_TOOL",
    "WRITE_TOOL_ALLOWLIST",
    "ToolACL",
]
