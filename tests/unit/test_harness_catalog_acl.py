"""工具目录与权限矩阵（v6 §7.7.2）。

这组用例守的是**矩阵与目录不漂移**：v6 §7.7.2 那张表里出现的每个工具都要在
目录里、每个格子的开关都要与表一致。矩阵是运行时拦截的唯一依据，它一旦与设计
方案对不上，「越权拦截率 100%」这句话就没有意义了。
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from backend.core.errors import ArchitecturalBanError, ToolPermissionDeniedError
from backend.harness.acl import (
    ACL_MATRIX,
    FORBIDDEN_NODES,
    PROBE_TOOL,
    WRITE_TOOL_ALLOWLIST,
    ToolACL,
)
from backend.harness.registry import ToolNotBoundError, ToolRegistry
from backend.harness.tools import ENTITY_SCHEMA_KEY, TOOL_CATALOG
from backend.harness.types import ALL_COMPONENTS, ComponentName, ToolSpec

# v6 §7.7.2 的表格，逐格重抄一遍（**不是** import 的那份，故意重写以便发现漂移）
EXPECTED_ROWS: dict[str, tuple[ComponentName, ...]] = {
    "resolve_person": ("route", "planner"),
    "resolve_aircraft": ("route", "planner"),
    "resolve_week": ("route", "planner"),
    "ask_user": ("route", "planner"),
    "escalate": ("route", "planner"),
    "estimate_scope": ("planner",),
    "assess_disruption": ("planner",),
    "propose_solve_intent": ("planner",),
    "translate_revision": ("planner",),
    "check_authority": ("planner",),
    "classify_doc": ("extract",),
    "parse_personnel": ("extract",),
    "parse_aircraft": ("extract",),
    "parse_missions": ("extract",),
    "parse_rules": ("extract",),
    "diff_snapshot": ("extract",),
    "propose_change": ("extract",),
    "propose_rule_dsl": ("extract",),
    "sql_query": ("extract", "knowledge", "diagnosis", "explain"),
    "prereq_cte": ("extract", "knowledge", "diagnosis", "explain"),
    "vector_search": ("extract", "knowledge", "diagnosis", "explain"),
    "bm25_search": ("extract", "knowledge", "diagnosis", "explain"),
    "rrf_fuse": ("extract", "knowledge", "diagnosis", "explain"),
    "rerank": ("extract", "knowledge", "diagnosis", "explain"),
    "memory.search": ALL_COMPONENTS,
    "memory.write": ("extract",),
    "min_conflict_set": ("diagnosis",),
    "blame_chain": ("diagnosis",),
    "probe_solve": ("diagnosis",),
    "rank_relaxations": ("diagnosis",),
    "render_workbook": ("explain",),
    "compose_report": ("explain",),
    "verify_claim": ("explain",),
}


# ─── 目录 ────────────────────────────────────────────────────────────


def test_catalog_covers_matrix_exactly() -> None:
    assert set(TOOL_CATALOG) == set(EXPECTED_ROWS)


@pytest.mark.parametrize("name", sorted(TOOL_CATALOG))
def test_every_tool_exports_a_json_schema(name: str) -> None:
    spec = TOOL_CATALOG[name]
    schema = spec.json_schema()
    assert schema["type"] == "object"
    assert issubclass(spec.params_model, BaseModel)
    assert spec.description


@pytest.mark.parametrize("name", sorted(TOOL_CATALOG))
def test_params_forbid_extra_fields(name: str) -> None:
    """多给字段必须报错——默默忽略等于让模型的幻觉悄悄进系统。"""
    assert TOOL_CATALOG[name].params_model.model_config.get("extra") == "forbid"


def test_only_memory_write_writes() -> None:
    writers = {name for name, spec in TOOL_CATALOG.items() if spec.writes}
    assert writers == set(WRITE_TOOL_ALLOWLIST) == {"memory.write"}


def test_probe_is_the_only_probe_pool_tool() -> None:
    probes = {name for name, spec in TOOL_CATALOG.items() if spec.budget_pool == "probe"}
    assert probes == {PROBE_TOOL} == {"probe_solve"}


def test_non_deterministic_tools_are_the_expected_few() -> None:
    """能缓存的必须是「同快照同参数同结果」的；这几个不是。"""
    assert {name for name, spec in TOOL_CATALOG.items() if not spec.deterministic} == {
        "ask_user",
        "escalate",
        "memory.write",
        "probe_solve",
    }


def test_entity_annotation_reaches_exported_schema() -> None:
    """`x-entity` 要出现在给模型看的 schema 里，模型才知道这里要编号不要人名。"""
    schema = TOOL_CATALOG["prereq_cte"].json_schema()
    assert schema["properties"]["person_id"][ENTITY_SCHEMA_KEY] == "person"
    assert schema["properties"]["person_id"]["pattern"] == r"^P\d+$"


def test_no_deterministic_node_in_catalog() -> None:
    """铁律 4：六个确定性节点不注册为工具。"""
    assert not (set(TOOL_CATALOG) & FORBIDDEN_NODES)


# ─── 矩阵 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("tool", "components"), sorted(EXPECTED_ROWS.items()))
def test_matrix_matches_v6_table(tool: str, components: tuple[ComponentName, ...]) -> None:
    acl = ToolACL()
    for component in ALL_COMPONENTS:
        if component in components:
            acl.assert_allowed(component, tool)
        else:
            with pytest.raises(ToolPermissionDeniedError):
                acl.assert_allowed(component, tool)


def test_matrix_has_all_six_components() -> None:
    assert set(ACL_MATRIX) == set(ALL_COMPONENTS)
    assert len(ALL_COMPONENTS) == 6


def test_unknown_tool_is_denied_not_ignored() -> None:
    with pytest.raises(ToolPermissionDeniedError, match="未知工具"):
        ToolACL().assert_allowed("planner", "definitely_not_a_tool")


def test_components_allowed_lookup() -> None:
    assert ToolACL().components_allowed("probe_solve") == frozenset({"diagnosis"})


def test_assert_exposable_rejects_superset() -> None:
    with pytest.raises(ToolPermissionDeniedError):
        ToolACL().assert_exposable("route", ("resolve_person", "propose_solve_intent"))


# ─── 注册期禁令 ──────────────────────────────────────────────────────


class _AnyParams(BaseModel):
    x: str = ""


@pytest.mark.parametrize("node", sorted(FORBIDDEN_NODES))
def test_registering_a_deterministic_node_is_an_architectural_error(node: str) -> None:
    spec = ToolSpec(name=node, description="试图注册确定性节点", params_model=_AnyParams)
    with pytest.raises(ArchitecturalBanError, match="确定性节点"):
        ToolACL.assert_registrable(spec)


def test_registering_a_second_write_tool_is_banned() -> None:
    spec = ToolSpec(
        name="propose_change", description="偷偷改成写工具", params_model=_AnyParams, writes=True
    )
    with pytest.raises(ArchitecturalBanError, match="数据写入"):
        ToolACL.assert_registrable(spec)


def test_registry_rejects_tools_outside_catalog() -> None:
    with pytest.raises(ToolNotBoundError, match="不在工具目录"):
        ToolRegistry().register("solve", lambda _a: None)


def test_registry_reports_unbound_loudly() -> None:
    registry = ToolRegistry()
    assert not registry.is_bound("vector_search")
    with pytest.raises(ToolNotBoundError, match="没有接上实现"):
        registry.handler("vector_search")


def test_registry_schemas_for_exposed_tools() -> None:
    schemas = ToolRegistry().schemas_for(("resolve_person", "memory.search"))
    assert [s.name for s in schemas] == ["resolve_person", "memory.search"]
    assert schemas[0].parameters["properties"]["surface"]["type"] == "string"
