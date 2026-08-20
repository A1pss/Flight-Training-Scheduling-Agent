"""`trajectory_100` 的内容断言（v6 §12.6）。"""

from __future__ import annotations

from collections import Counter

import pytest

from backend.datasets.loader import load_eval_dataset
from backend.datasets.schemas import GRAPH_NODES, PIPELINE_STAGES, TrajectoryItem
from backend.harness.acl import ACL_MATRIX
from tests.datasets import trajectory_catalog


@pytest.fixture(scope="module")
def items() -> list[TrajectoryItem]:
    _manifest, rows = load_eval_dataset("trajectory_100")
    return [row for row in rows if isinstance(row, TrajectoryItem)]


def test_committed_file_matches_builder(items: list[TrajectoryItem]) -> None:
    built = [TrajectoryItem.model_validate(row) for row in trajectory_catalog.build_full()]
    assert [i.model_dump() for i in items] == [b.model_dump() for b in built]


def test_flow_split(items: list[TrajectoryItem]) -> None:
    assert len(items) == 100
    assert Counter(i.flow for i in items) == {
        "query": 30,
        "diagnosis": 25,
        "schedule": 15,
        "reschedule": 10,
        "revision": 10,
        "ingest": 10,
    }


def test_autonomous_flows_are_more_than_half(items: list[TrajectoryItem]) -> None:
    """★ §12.6.2 的硬性要求：两处受控自治应占标注集一半以上。

    排班与重排的期望路径是固定序列，在那里轨迹评估只验「没跑偏」；真正考察
    自主决策质量的是 Knowledge 检索循环与 Diagnosis 探测循环。
    """
    autonomous = sum(1 for i in items if i.flow in {"query", "diagnosis"})
    assert autonomous > len(items) / 2, autonomous


def test_every_item_has_alternatives_and_forbidden(items: list[TrajectoryItem]) -> None:
    """**每一条都要有替代路径与禁止路径。**

    只写期望路径的标注等于把「不同但同样合理」全判为错（§12.6.2 明确要避免的），
    而只写替代不写禁止的标注等于「可接受」没有边界 —— 两头都要有。

    **唯一的例外**是零工具调用的降级路径（Harness 不可用时的诊断）：那条路上
    没有任何可变的部分，硬编一条「替代」反而是在描述不存在的行为。
    """
    for item in items:
        assert item.forbidden_paths, item.item_id
        if any(e.startswith("tool:") for e in item.expected_path):
            assert item.acceptable_paths, item.item_id


def test_deterministic_nodes_are_never_skipped(items: list[TrajectoryItem]) -> None:
    """★ 规则 D：凡是走到 `solve` 的排班/重排路径，`compile_spec` 必须在它之前。

    这一条同时校验期望路径与全部可接受路径 —— 替代路径里漏一个 `compile_spec`，
    等于悄悄承认「可以跳过语义编译」。
    """
    for item in items:
        if item.flow not in {"schedule", "reschedule"}:
            continue
        for path in [item.expected_path, *item.acceptable_paths]:
            if "solve" in path:
                assert "compile_spec" in path, f"{item.item_id}: {path}"
                assert path.index("compile_spec") < path.index("solve"), item.item_id
            if "commit_plan" in path:
                assert "validate" in path, f"{item.item_id}: {path}"


def test_revision_flows_have_two_gate_roundtrips(items: list[TrajectoryItem]) -> None:
    """★ `Z-19`：修订轮是**两次门禁往返**。

    走到 `solve` 的修订路径里，`human_gate` 必须出现 ≥2 次（回显一次、方案确认一次）。
    只有一次 = 翻译完直接重解，正是 v6 反模式清单点名的「先重解再展示」。
    """
    for item in items:
        if item.flow != "revision":
            continue
        for path in [item.expected_path, *item.acceptable_paths]:
            if "solve" in path:
                assert path.count("human_gate") >= 2, f"{item.item_id}: {path}"


def test_diagnosis_never_reaches_commit(items: list[TrajectoryItem]) -> None:
    """INFEASIBLE 却走到 `commit_plan`，是把不可行当可行交付（铁律 8）。"""
    for item in items:
        if item.flow != "diagnosis":
            continue
        for path in [item.expected_path, *item.acceptable_paths]:
            assert "commit_plan" not in path, f"{item.item_id}: {path}"


def test_ingest_never_commits_without_gate(items: list[TrajectoryItem]) -> None:
    """★ v6 §5.1 的人工确认是硬性门禁：`commit` 之前必须有 `gate`。"""
    for item in items:
        if item.flow != "ingest":
            continue
        for path in [item.expected_path, *item.acceptable_paths]:
            if "ingest.commit" in path:
                assert "ingest.gate" in path, f"{item.item_id}: {path}"
                assert path.index("ingest.gate") < path.index("ingest.commit")


def test_forbidden_paths_are_really_forbidden(items: list[TrajectoryItem]) -> None:
    """禁止路径不许与本条的期望/可接受路径相同 —— 自相矛盾的标注会让判定器两边都对。"""
    for item in items:
        for path in item.forbidden_paths:
            assert path != item.expected_path, item.item_id
            assert path not in item.acceptable_paths, item.item_id


def test_all_path_elements_are_real(items: list[TrajectoryItem]) -> None:
    """路径元素必须是真的图节点 / 摄取阶段 / 已登记工具 —— 不许有想象出来的节点。"""
    for item in items:
        for path in [item.expected_path, *item.acceptable_paths, *item.forbidden_paths]:
            for element in path:
                assert (
                    element in GRAPH_NODES
                    or element in PIPELINE_STAGES
                    or element.startswith("tool:")
                ), f"{item.item_id}: {element}"


def test_steps_respect_the_acl_matrix(items: list[TrajectoryItem]) -> None:
    """★ 每一步的工具都必须在该组件的 ACL 行里。

    给 `planner` 标一次 `probe_solve` 看起来很合理（探一下影响面嘛），但探针只给了
    `diagnosis`（§7.7.2 的唯一例外 + 独立预算池）。一份把越权调用标成「期望路径」
    的数据集，会把 §12.5.1 的越权拦截率直接测成负数。
    """
    for item in items:
        for step in item.steps:
            assert step.tool in ACL_MATRIX[step.component], f"{item.item_id}: {step.tool}"


def test_probe_solve_only_in_diagnosis(items: list[TrajectoryItem]) -> None:
    """`probe_solve` 是确定性边界的唯一例外，且只有 Diagnosis 能用。"""
    for item in items:
        if "tool:probe_solve" in item.expected_path:
            assert item.flow == "diagnosis", item.item_id
