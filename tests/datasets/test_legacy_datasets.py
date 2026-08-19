"""`plan_scenarios` 与 `golden_40` 的核对断言（M9-A 只核对，不改数据）。"""

from __future__ import annotations

from collections import Counter

import pytest

from backend.datasets.loader import load_eval_dataset
from backend.datasets.schemas import GoldenCaseItem, PlanScenarioItem
from tests.datasets import legacy_catalog


@pytest.fixture(scope="module")
def scenarios() -> list[PlanScenarioItem]:
    _manifest, rows = load_eval_dataset("plan_scenarios")
    return [row for row in rows if isinstance(row, PlanScenarioItem)]


@pytest.fixture(scope="module")
def golden() -> list[GoldenCaseItem]:
    _manifest, rows = load_eval_dataset("golden_40")
    return [row for row in rows if isinstance(row, GoldenCaseItem)]


def test_scenario_counts_match_the_spec(scenarios: list[PlanScenarioItem]) -> None:
    """§12.3 那张表逐项对上：1 + 60 + 60 + 40 + 30 + 9 = 200。"""
    assert len(scenarios) == 200
    assert Counter(s.category for s in scenarios) == legacy_catalog.EXPECTED_COUNTS


def test_single_point_covers_runway_closure(scenarios: list[PlanScenarioItem]) -> None:
    """★ 本窗口点名要核的一条：单点扰动**必须含跑道关闭**。

    §12.3 原文列了五种单点扰动（1 人请假 / 1 机维修 / 1 资质到期 /
    1 空域容量降为 0 / **1 跑道关闭**）。少了跑道那一族，约束9 在单点层面
    就没有观测点 —— 而 I5 整族的设计目的正是验它。
    """
    families = {s.family for s in scenarios if s.category == "single"}
    assert families >= legacy_catalog.EXPECTED_SINGLE_FAMILIES, sorted(families)


def test_infeasible_is_five_families_not_four(scenarios: list[PlanScenarioItem]) -> None:
    """★ 另一条点名要核的：不可行是 **I1~I5 五族**，每族 6 个变体。

    v5.2 只有四族；v6 在 M2-A 实测之后重写了 I1/I4/I5 的构造并补齐到五族
    （旧构造实测**全部可行**，因为它们建立在「A 类需教员带飞」这个已被 D-1
    推翻的前提上）。数据集里若只有四族，等于回到了那个已作废的版本。
    """
    infeasible = [s for s in scenarios if s.category == "infeasible"]
    assert Counter(s.family for s in infeasible) == dict.fromkeys(("I1", "I2", "I3", "I4", "I5"), 6)


def test_every_infeasible_scenario_is_annotated(scenarios: list[PlanScenarioItem]) -> None:
    """最小冲突集的召回率要 100%，前提是每条都有人工标注的真实冲突源。"""
    for scenario in scenarios:
        if scenario.category == "infeasible":
            assert scenario.annotated_conflict_rules, scenario.scenario_id


def test_disturbance_scenarios_do_not_presume_feasibility(
    scenarios: list[PlanScenarioItem],
) -> None:
    """★ 单点/组合扰动一律 `EITHER` —— **不预设可行与否**。

    预设了就会诱导「为了对上预期而放宽约束」，那正是 CLAUDE.md §7 第 4 条
    与反模式清单第 2 条同时点名的行为。
    """
    for scenario in scenarios:
        if scenario.category in {"single", "combo"}:
            assert scenario.expected_status == "EITHER", scenario.scenario_id


def test_golden_cases_are_forty(golden: list[GoldenCaseItem]) -> None:
    assert len(golden) == 40
    assert Counter(g.status for g in golden) == {"OPTIMAL": 38, "INFEASIBLE": 2}


def test_no_golden_case_is_feasible(golden: list[GoldenCaseItem]) -> None:
    """★ 黄金用例里**不许出现 FEASIBLE**。

    它是被预算截断的结果，不保证逐字节可复现（§3.11.1）—— 拿它比对会得到一个
    会飘的门禁，而那种门禁教人重跑、不教人查问题。哪个用例掉到 FEASIBLE，
    要修的是那个用例的规模。
    """
    assert {g.status for g in golden} <= {"OPTIMAL", "INFEASIBLE"}


def test_optimal_cases_carry_a_fingerprint(golden: list[GoldenCaseItem]) -> None:
    for case in golden:
        if case.status == "OPTIMAL":
            assert case.content_sha256
            assert case.validator_passed and case.naive_passed
        else:
            assert case.content_sha256 is None
            assert case.num_sorties == 0


def test_golden_index_matches_the_baseline_files(golden: list[GoldenCaseItem]) -> None:
    """索引与 `tests/golden/` 下的 yml 一一对应 —— 少一份或多一份都要看得见。"""
    files = sorted(p.stem for p in legacy_catalog.GOLDEN_DIR.glob("*.yml"))
    assert [g.case_id for g in golden] == files
