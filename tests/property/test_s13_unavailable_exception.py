"""S-13 的例外：**整周不可用的学员不计入约束3**（业务方 2026-08-12 裁定，v6 `Z-9`）。

## 这条例外是怎么来的

M2-C 的 200 场景实测（`SP-ABS-05`~`SP-ABS-08`）：**任何一名学员整周请假，整周就
判 INFEASIBLE**。八个人各请一周假的对照很干净 ——

| 请假的人 | 裁定前 |
|---|---|
| 三名教员、成熟飞行员 | `OPTIMAL`，14 架次 |
| **四名学员，各自** | **`INFEASIBLE`，0 架次** |

根因是约束3 + S-13：要求**每名学员**每周至少飞 1 次「每周必飞」类课目，**不论
完成状态**。人整周不在，这条就永远满足不了，而 Tier 0 下它是硬约束 —— 于是不只是
那名学员没得排，是全队都排不出来。学员请一周病假是常规事件，不该有这个后果。

## 判据只看「人在不在」，不看「排不排得上」

这是本条例外最容易被写歪的地方，两侧实现都必须守住：

- **豁免**：该学员本周**每一天**都在 `person_unavailability` 里；
- **不豁免**：只要还有一天可用 —— 哪怕那天飞机全在修、空域关了、跑道关了，
  导致他事实上一个候选都没有。那是**资源不足**，必须如实判不可行。

把判据写成「无可行候选即豁免」会让约束3 在资源紧张时**静默失效**，那比不加这条
例外更糟：排班员会以为「系统说可行」，而实际上有人整周没飞 A 类却没人告诉他。
`test_exception_does_not_swallow_resource_shortage` 就是钉这一条的。

## ⚠️ 这条例外**不能**让「学员整周请假」重新可解

约束13（进度推进）是**另一条**约束，它对该学员未完成且先修满足的每门课目各下一个
频率滑窗要求；人不在同样满足不了。裁定后 `SP-ABS-05` 的冲突集从 `C03 + C13` 变成
只剩 `C13`，**状态仍是 INFEASIBLE**。这是刻意的：约束13 的语义是「推进进度」，
落了就是真落了，该走松弛阶梯 Tier 1（频率窗口软化 + 欠账显式披露），而不是由这条
例外顺手抹掉。见 `reports/M2D_S13例外_收工说明.md`。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend.core.ruleset import get_semantics
from backend.solver.candidates import enumerate_candidates
from backend.solver.solve import solve
from backend.validator import run_all_checks
from tests.naive_checker import naive_check_all
from tests.property.scenario import SCENARIO_WEEK_START, ScenarioSpec, build_scenario
from tests.property.world import BASELINE_BLOCKED, BASELINE_DRAFTS, injection_world, make_plan

pytestmark = pytest.mark.property

WEEK = SCENARIO_WEEK_START
ALL_WEEK = tuple(WEEK + timedelta(days=i) for i in range(7))


def _world(unavailable: dict[str, tuple[object, ...]]) -> ScenarioSpec:
    """注入世界 + 指定人员的不可用日期。"""
    base = injection_world()
    persons = tuple(
        p
        if p.person_id not in unavailable
        else type(p)(**{**p.__dict__, "unavailable": unavailable[p.person_id]})  # type: ignore[arg-type]
        for p in base.persons
    )
    return base.with_(persons=persons)


def _c03_requirement_ids(scenario: ScenarioSpec) -> set[str]:
    bundle = scenario.to_bundle()
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    return {r.req_id for r in cset.requirements if r.rule_id == 3}


# ─────────────────────────────────────────────────────────────────────
# 开关本身
# ─────────────────────────────────────────────────────────────────────
def test_switch_is_on_and_does_not_change_s13_value() -> None:
    """例外是 S-13 的**子字段**，不改 `value` —— 约束3 仍然对全部学员生效。"""
    sem = get_semantics()
    assert sem.s13_all_students is True
    assert sem.s13_exclude_unavailable is True
    assert sem.value("S-13") == "all_students"


def test_semantics_version_was_bumped() -> None:
    """行为变了就必须换版本号 —— `semantics_version` 参与 `content_sha256`。"""
    assert get_semantics().version == "1.1.0"


# ─────────────────────────────────────────────────────────────────────
# 求解器侧：整周不可用 → 不生成约束3 要求
# ─────────────────────────────────────────────────────────────────────
def test_solver_drops_c03_for_a_fully_unavailable_student() -> None:
    scenario = _world({"P411": ALL_WEEK})
    ids = _c03_requirement_ids(scenario)
    assert not any(rid.startswith("C03|P411|") for rid in ids), ids
    # 其余两名学员的约束3 照旧
    assert any(rid.startswith("C03|P412|") for rid in ids)
    assert any(rid.startswith("C03|P413|") for rid in ids)


def test_solver_keeps_c03_when_one_day_is_still_available() -> None:
    """只差一天也不豁免 —— 判据是「整周都不在」，不是「大部分时间不在」。"""
    scenario = _world({"P411": ALL_WEEK[:6]})
    ids = _c03_requirement_ids(scenario)
    assert any(rid.startswith("C03|P411|") for rid in ids), ids


def test_solver_solves_when_a_student_is_out_all_week() -> None:
    """整周请假的学员被豁免后，其余人照常排得出来。

    ⚠️ 这里挑的是 `P413` —— 注入世界里**唯一没有未完成课目**的学员。换成 `P411`
    这条会红，因为他的 `missionB-1` 还欠着，约束13 顶上来（见本文件末尾那条边界
    用例）。**例外只解开约束3 这一条**。
    """
    scenario = _world({"P413": ALL_WEEK})
    outcome = solve(scenario.to_bundle())
    assert outcome.status in ("OPTIMAL", "FEASIBLE"), outcome.status
    assert outcome.plan is not None
    assert all(m.person_id != "P413" for s in outcome.plan.sorties for m in s.crew), (
        "整周不可用的人不该出现在任何机组里"
    )


def test_exception_does_not_swallow_resource_shortage() -> None:
    """★ 学员在岗、但每周必飞类课目的空域全关 → **仍须判不可行**。

    这条钉住例外的边界：豁免的条件是「人不在」，不是「排不上」。写成后者会让
    约束3 在资源紧张时静默失效。
    """
    base = injection_world()
    weekly_airspaces = {m.airspace_id for m in base.missions if m.weekly_required}
    scenario = base.with_(airspace_capacity_override=dict.fromkeys(sorted(weekly_airspaces), 0))
    ids = _c03_requirement_ids(scenario)
    assert any(rid.startswith("C03|") for rid in ids), "人还在，约束3 必须照常下"
    assert solve(scenario.to_bundle()).status == "INFEASIBLE"


# ─────────────────────────────────────────────────────────────────────
# 校验器侧 + 第三方 naive checker
# ─────────────────────────────────────────────────────────────────────
def _plan_without(student: str, scenario: ScenarioSpec) -> object:
    """去掉某名学员全部架次的方案（用注入世界的手工基线改）。"""
    ctx = scenario.to_validation_context()
    drafts = tuple(d for d in BASELINE_DRAFTS if d.trainee_id != student)
    return make_plan(drafts, ctx, blocked=BASELINE_BLOCKED, plan_id="S13-EXC")


def test_both_checkers_exempt_a_fully_unavailable_student() -> None:
    scenario = _world({"P413": ALL_WEEK})
    ctx = scenario.to_validation_context()
    plan = _plan_without("P413", scenario)
    main = {v.rule_id for v in run_all_checks(plan, ctx).all_violations()}  # type: ignore[arg-type]
    naive = naive_check_all(plan, ctx).violated_rules()  # type: ignore[arg-type]
    assert "C03" not in main, main
    assert "C03" not in naive, naive
    assert main == naive


def test_both_checkers_still_require_an_available_student() -> None:
    """同一份方案，学员在岗时必须报 C03 —— 否则例外就是把约束3 关掉了。"""
    scenario = injection_world()
    ctx = scenario.to_validation_context()
    plan = _plan_without("P413", scenario)
    main = {v.rule_id for v in run_all_checks(plan, ctx).all_violations()}  # type: ignore[arg-type]
    naive = naive_check_all(plan, ctx).violated_rules()  # type: ignore[arg-type]
    assert "C03" in main, main
    assert "C03" in naive, naive
    assert main == naive


def test_both_checkers_still_require_a_partially_available_student() -> None:
    """只有一天可用也照样要求 —— 与求解器侧同一条判据。"""
    scenario = _world({"P413": ALL_WEEK[:6]})
    ctx = scenario.to_validation_context()
    plan = _plan_without("P413", scenario)
    assert "C03" in {v.rule_id for v in run_all_checks(plan, ctx).all_violations()}  # type: ignore[arg-type]
    assert "C03" in naive_check_all(plan, ctx).violated_rules()  # type: ignore[arg-type]


def test_solver_output_still_passes_both_checkers_with_an_absent_student() -> None:
    """端到端：整周请假的场景下，求解器出的解仍须过两条通道。"""
    scenario = _world({"P413": ALL_WEEK})
    outcome = solve(scenario.to_bundle())
    assert outcome.plan is not None
    ctx = scenario.to_validation_context()
    assert run_all_checks(outcome.plan, ctx).all_passed
    assert naive_check_all(outcome.plan, ctx).passed


# ─────────────────────────────────────────────────────────────────────
# 例外的边界：它治不好约束13
# ─────────────────────────────────────────────────────────────────────
def test_exception_does_not_rescue_constraint_13() -> None:
    """★ 有未完成课目的学员整周请假 → 仍然 INFEASIBLE，且冲突集只剩 C13。

    这不是缺陷，是分工：约束13 管「推进进度」，落了就是真落了，该走 Tier 1
    松弛并把欠账显式披露，而不是由约束3 的例外顺手抹掉。
    """
    from backend.solver.diagnose import diagnose

    scenario = build_scenario(
        label="s13-c13-boundary",
        n_instructors=1,
        n_students=2,
        n_missions=3,
        completed_depth=2,  # B-1 未完成 → 该学员有约束13 要求
        unavailable={"P411": ALL_WEEK},
    )
    bundle = scenario.to_bundle()
    outcome = solve(bundle)
    assert outcome.status == "INFEASIBLE"
    result = diagnose(bundle, cset=outcome.cset, time_limit_s=30.0)
    rules = {rid for item in result.conflicts for rid in item.rule_ids}
    assert "C13" in rules
    assert "C03" not in rules, f"约束3 不该再出现在冲突集里，实际 {sorted(rules)}"
