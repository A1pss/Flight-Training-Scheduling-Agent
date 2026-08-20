"""`tool_calls_200` 的内容断言（v6 §12.5.1）。

这一集的标签是**算出来的**，所以断言的重点不是「标得对不对」，而是
「算的方式对不对」：权重是不是真的来自观测频次、越权对是不是真的取自补集、
两个预算池有没有被混成一个。
"""

from __future__ import annotations

from collections import Counter

import pytest

from backend.datasets.loader import load_eval_dataset
from backend.datasets.schemas import ToolCallItem
from backend.harness.acl import ACL_MATRIX, FORBIDDEN_NODES
from backend.harness.tools import TOOL_CATALOG
from tests.datasets import tool_call_catalog


@pytest.fixture(scope="module")
def items() -> list[ToolCallItem]:
    _manifest, rows = load_eval_dataset("tool_calls_200")
    return [row for row in rows if isinstance(row, ToolCallItem)]


def test_committed_file_matches_builder(items: list[ToolCallItem]) -> None:
    built = [ToolCallItem.model_validate(row) for row in tool_call_catalog.build()]
    assert [i.model_dump() for i in items] == [b.model_dump() for b in built]


def test_strata(items: list[ToolCallItem]) -> None:
    assert Counter(i.stratum for i in items) == {
        "valid": 200,
        "acl_violation": 30,
        "budget_exhaustion": 30,
    }


def test_every_tool_gets_at_least_the_floor(items: list[ToolCallItem]) -> None:
    """★ 每个工具至少 2 条。

    只按频率分配会让 `escalate` / `memory.write` / `render_workbook` 这类
    「轨迹集里没出现过」的工具一条都分不到 —— 而 §12.5.1 的契约通过率是**调用级**
    指标，覆盖不到的工具在那个数里是隐形的。
    """
    counts = Counter(i.tool for i in items if i.stratum == "valid")
    assert set(counts) == set(TOOL_CATALOG)
    assert min(counts.values()) >= tool_call_catalog.FLOOR_PER_TOOL


def test_weights_follow_observed_frequency(items: list[ToolCallItem]) -> None:
    """配额的排序应当与观测频次的排序一致（同频次的允许并列）。"""
    counts = Counter(i.tool for i in items if i.stratum == "valid")
    freq = tool_call_catalog.observed_frequency()
    hottest = max(freq, key=lambda t: freq[t])
    coldest = [t for t in TOOL_CATALOG if freq.get(t, 0) == 0]
    assert counts[hottest] > tool_call_catalog.FLOOR_PER_TOOL
    for tool in coldest:
        assert counts[tool] == tool_call_catalog.FLOOR_PER_TOOL


def test_valid_params_pass_the_real_contract(items: list[ToolCallItem]) -> None:
    """valid 层的每一组参数都要过工具**真实的** Pydantic 契约。

    这是「标签天然正确」这句话的全部依据 —— 参数过不了契约，`accept` 这个标签
    就是错的，而那会把 §12.5.1 的一次通过率测成模型的问题。
    """
    for item in items:
        if item.stratum == "valid":
            TOOL_CATALOG[item.tool].params_model.model_validate(item.expected_params)


def test_acl_pairs_are_really_out_of_matrix(items: list[ToolCallItem]) -> None:
    for item in items:
        if item.stratum == "acl_violation" and item.tool_exists:
            assert item.tool not in ACL_MATRIX[item.component], item.item_id


def test_acl_layer_has_both_failure_modes(items: list[ToolCallItem]) -> None:
    """★ 「有工具没权限」与「凭空编的工具名」是两种不同的失败模式。

    后者在没有调用期拦截时会以 `KeyError` 出现在执行器里 —— 那不是越权拦截，
    统计与日志全错位（§7.7.2 那段注释说的正是这件事）。只测前一种等于漏了一半。
    """
    acl = [i for i in items if i.stratum == "acl_violation"]
    invented = [i for i in acl if not i.tool_exists]
    assert len(invented) == 6
    assert {i.tool for i in invented} == FORBIDDEN_NODES
    assert len(acl) - len(invented) == 24


def test_acl_layer_covers_every_component(items: list[ToolCallItem]) -> None:
    components = {i.component for i in items if i.stratum == "acl_violation"}
    assert len(components) == 6


def test_two_budget_pools_are_kept_apart(items: list[ToolCallItem]) -> None:
    """★ Harness 预算**抛错**（FTS-4003），探针池**不抛错**（优雅返回载荷）。

    两者互不挤占（§3.9.2）。把它们混成一个数，「预算熔断正确率」就失去意义了。
    """
    budget = [i for i in items if i.stratum == "budget_exhaustion"]
    harness = [i for i in budget if i.expected_error_code == "FTS-4003"]
    probe = [i for i in budget if i.expected_error_code is None]
    assert len(harness) == 24
    assert len(probe) == 6
    assert {i.tool for i in probe} == {"probe_solve"}
    assert all(i.component == "diagnosis" for i in probe)


def test_probe_tool_never_leaves_diagnosis(items: list[ToolCallItem]) -> None:
    """探针是确定性边界的唯一例外，且只有 Diagnosis 有权用（铁律 4 / §7.7.2）。"""
    for item in items:
        if item.tool == "probe_solve" and item.stratum != "acl_violation":
            assert item.component == "diagnosis", item.item_id
