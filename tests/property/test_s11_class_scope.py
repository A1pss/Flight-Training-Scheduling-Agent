"""S-11 复训要求的粒度 = **类别**（业务方 2026-08-12 裁定后的回归）。

## 这条裁定是怎么来的

M2-C 的属性测试 `test_solver_output_always_passes_validator` 跑出的第 1 个反例，
在**基准周真实数据**上一字不差地复现（构造就是 v6 §12.3 的 S-11 专项：把刘斌
C 类到期日提前到 2026-01-04）：

```
求解器： OPTIMAL，15 架次，其中 S000004 = 刘斌 missionC-2 复训
主校验器：C13 HARD 刘斌(P04) 的 missionC-1 …… 本周一次都未安排
naive ： C13      P04 的 missionC-1 在周内窗口 [第0天, 第6天] 内一次都没安排
```

根因不是哪一侧写错了，是 **v6 自相矛盾**：

| 出处 | 读法 |
|---|---|
| §3.2 约束13 行「S-11……**同样受本条约束**」 | 约束13 的粒度是 (person, mission) → 逐门 |
| §12.3 S-11 专项断言 ①「≥1 次刘斌的 **C-1 或 C-2**」 | 按类别 |

**业务方 2026-08-12 裁定：取类别粒度**（与 S-02「A 类整体 ≥1 次」同构，语义都是
「保持熟练度」而非「推进进度」）。落点：`validator/checks.py::check_c13` 的
`recurrent_groups`、`tests/naive_checker.py::frequency_requirements`，
以及 v6 §3.2 约束13 行的补注。**求解器一侧未改动。**

## 本文件钉住什么

裁定之后三条通道必须一致；更要紧的是钉住**反方向**——如果哪天有人把 C13 改回
逐门判，这里立刻红。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend.solver.solve import solve
from backend.validator import run_all_checks
from tests.naive_checker import frequency_requirements, naive_check_all
from tests.property.scenario import SCENARIO_WEEK_START, ScenarioSpec, build_scenario

pytestmark = pytest.mark.property


def _mature_expiry_scenario() -> ScenarioSpec:
    """成熟飞行员的 A 类（含 A-1/A-2 两门）在开周前一天到期 → 复训窗口整周落在周内。"""
    return build_scenario(
        label="s11-class-scope",
        n_instructors=1,
        n_students=1,
        n_mature=1,
        n_aircraft=2,
        n_missions=3,
        completed_depth=3,
        expiry={("P421", "A"): SCENARIO_WEEK_START - timedelta(days=1)},
    )


def test_recurrent_requirement_is_one_per_class_not_one_per_mission() -> None:
    """naive 侧独立重算出来的要求集：该类**合成一条**，覆盖类里的全部课目。"""
    ctx = _mature_expiry_scenario().to_validation_context()
    recurrent = [r for r in frequency_requirements(ctx) if r.person_id == "P421"]
    assert len(recurrent) == 1, [r.label for r in recurrent]
    assert recurrent[0].mission_ids == ("missionA-1", "missionA-2")
    assert recurrent[0].freq_days == ctx.semantics.s11_window_days


def test_solver_and_validator_agree_on_s11_scope() -> None:
    """★ 原 FTS-3003 的回归：三条通道在同一份解上必须一致。"""
    scenario = _mature_expiry_scenario()
    outcome = solve(scenario.to_bundle())
    assert outcome.status in ("OPTIMAL", "FEASIBLE")
    assert outcome.plan is not None
    ctx = scenario.to_validation_context()
    report = run_all_checks(outcome.plan, ctx)
    naive = naive_check_all(outcome.plan, ctx)
    assert report.all_passed, [v.detail for v in report.all_violations()]
    assert naive.passed, [v.detail for v in naive.violations]


def test_flying_one_mission_of_the_class_satisfies_the_recurrency() -> None:
    """裁定的实质：飞该类里**任意一门**就算完成本次复训，不必两门都飞。"""
    scenario = _mature_expiry_scenario()
    outcome = solve(scenario.to_bundle())
    assert outcome.plan is not None
    recurrent = [s for s in outcome.plan.sorties if s.is_recurrent]
    assert recurrent, "S-11 复训架次一个都没排"
    flown = {s.mission_id for s in recurrent}
    assert flown < {"missionA-1", "missionA-2"}, (
        f"只需飞该类任一门，实际飞了整类 {sorted(flown)} —— "
        "若这是有意改动，请先回到 v6 §3.2 约束13 的补注重新裁定"
    )
    ctx = scenario.to_validation_context()
    assert run_all_checks(outcome.plan, ctx).all_passed


def test_missing_the_whole_class_is_still_a_violation() -> None:
    """反方向：整类一次都不飞，仍然必须报 C13 —— 裁定放宽的是粒度，不是要求本身。"""
    scenario = _mature_expiry_scenario()
    outcome = solve(scenario.to_bundle())
    assert outcome.plan is not None
    stripped = outcome.plan.model_copy(
        update={"sorties": [s for s in outcome.plan.sorties if not s.is_recurrent]}
    )
    ctx = scenario.to_validation_context()
    main = {v.rule_id for v in run_all_checks(stripped, ctx).all_violations()}
    naive = naive_check_all(stripped, ctx).violated_rules()
    assert "C13" in main and "C13" in naive
    assert main == naive
