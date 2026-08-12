"""黄金用例：固定输入 → 固定输出**逐字节**比对（v6 §12.1「黄金用例 ~40」）。

## 比的是什么

每个用例是一个**完全确定**的合成场景（`build_scenario` 的一组固定参数），
跑一遍 `solve()` 之后，把三样东西一起落成基线快照：

1. **方案本身** —— 状态、架次数、`content_sha256`、以及每个架次的全部字段
   （日期/星期/起降/课目/空域/机号/跑道/复训标记/机组）；
2. **阻塞项与欠账** —— 披露内容一个字都不能变；
3. **两条校验通道的判定** —— 主校验器逐条 `passed` / `checked_items` / 违规数，
   以及第三方 naive checker 的判定。

于是任何一处「求解器换了个等价最优解」「校验器少查了几项」「阻塞项措辞被改写」
都会在这里变成一个 diff，而不是等到验收现场才发现。

## 为什么用合成场景而不是基准周

基准周单次求解 20 秒（其中 8 秒是铁律 9 要求的单线程规范化阶段），40 个用例就是
13 分钟，进不了常规 pytest。合成场景每个约 0.2 秒，40 个 8 秒。基准周的逐字节
可复现另有专测（`tests/integration/test_solver_baseline_live.py`，M2-A 交付）。

**可复现性的边界照 v6 §3.11.1**：`OPTIMAL` 逐字节可复现，`FEASIBLE`（被预算截断）
不保证。合成场景规模极小、全部证到最优，所以这里可以逐字节比。用例里断言了这一点
（`test_all_golden_cases_reach_optimal`），一旦哪个用例掉到 `FEASIBLE`，先修的是
那个用例的规模，而不是把断言放宽。

## 更新基线的正确姿势

```bash
conda run -n schedule pytest tests/golden -q --force-regen   # 重新生成
git diff tests/golden/                                        # ★ 必须逐行读
```
`--force-regen` 之后**一定要读 diff**：黄金用例的价值全在「变化是被人看见的」。
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any

import pytest

from backend.solver.solve import solve
from backend.validator import run_all_checks
from tests.naive_checker import naive_check_all
from tests.property.scenario import SCENARIO_WEEK_START, ScenarioSpec, build_scenario

pytestmark = pytest.mark.golden

WEEK = SCENARIO_WEEK_START


def _day(offset: int) -> Any:
    return WEEK + timedelta(days=offset)


#: 40 个固定场景。**参数一旦定下就不要改** —— 改了等于换了一个用例，
#: 基线快照的历史比对价值随之失效。要加覆盖就往后追加新的。
GOLDEN_CASES: dict[str, dict[str, Any]] = {
    "g01_base": {},
    "g02_two_types": {"two_types": True},
    "g03_single_aircraft": {"n_aircraft": 1},
    "g04_three_aircraft": {"n_aircraft": 3},
    "g05_two_missions": {"n_missions": 2},
    "g06_four_missions": {"n_missions": 4},
    "g07_one_student": {"n_students": 1},
    "g08_three_students": {"n_students": 3},
    "g09_two_instructors": {"n_instructors": 2},
    "g10_with_mature_pilot": {"n_mature": 1},
    # S-11：成熟飞行员到期 → 复训（窗口整周落在周内 / 落在周中）
    "g11_s11_recurrent_from_week_start": {
        "n_mature": 1,
        "n_missions": 3,
        "completed_depth": 3,
        "expiry": {("P421", "A"): WEEK - timedelta(days=1)},
    },
    "g12_s11_recurrent_midweek": {
        "n_mature": 1,
        "n_missions": 3,
        "completed_depth": 3,
        "expiry": {("P421", "A"): WEEK + timedelta(days=2)},
    },
    # 约束2 字面：学员/教员到期即剔除
    "g13_student_expiry": {"n_missions": 3, "expiry": {("P411", "B"): WEEK + timedelta(days=1)}},
    "g14_instructor_expiry": {"n_missions": 3, "expiry": {("P401", "B"): WEEK}},
    "g15_three_seats": {"seats": 3},
    "g16_long_turnaround": {"turnaround": 40},
    "g17_short_turnaround": {"turnaround": 10},
    "g18_airspace_capacity_two": {"airspace_capacity": 2},
    "g19_airspace_capacity_one": {"airspace_capacity": 1},
    # 先修链深度 → BLOCKED 的形态
    "g20_nothing_completed": {"n_missions": 4, "completed_depth": 0},
    "g21_one_completed": {"n_missions": 4, "completed_depth": 1},
    "g22_all_completed": {"n_missions": 4, "completed_depth": 4},
    "g23_window_to_ten": {"window": (time(6, 0), time(10, 0))},
    "g24_window_to_fourteen": {"window": (time(6, 0), time(14, 0))},
    "g25_person_off_one_day": {"unavailable": {"P411": (_day(1),)}},
    "g26_instructor_off_all_week": {
        "n_instructors": 2,
        "unavailable": {"P401": tuple(_day(i) for i in range(7))},
    },
    "g27_aircraft_down_one_day": {"maintenance": {"AC701": (_day(2),)}},
    "g28_aircraft_down_three_days": {"maintenance": {"AC701": (_day(2), _day(3), _day(4))}},
    "g29_airspace_closed": {"n_missions": 3, "airspace_capacity_override": {"NAV": 0}},
    "g30_runway_eight_closed": {"closed_runways": ("RWY-8",)},
    "g31_runway_seven_closed": {"closed_runways": ("RWY-7",)},
    "g32_long_durations": {"durations": (70, 65, 60, 55)},
    "g33_short_durations": {"durations": (20, 22, 25, 28)},
    "g34_all_freq_three": {"freq_days": (3, 3, 3, 3)},
    "g35_all_freq_seven": {"freq_days": (7, 7, 7, 7)},
    "g36_all_freq_fourteen": {"freq_days": (14, 14, 14, 14)},
    "g37_absence_plus_maintenance": {
        "unavailable": {"P411": (_day(0),)},
        "maintenance": {"AC701": (_day(3),)},
    },
    "g38_closure_plus_runway": {
        "n_missions": 3,
        "airspace_capacity_override": {"NAV": 0},
        "closed_runways": ("RWY-8",),
    },
    "g39_window_plus_expiry": {
        "n_missions": 3,
        "window": (time(6, 0), time(12, 0)),
        "expiry": {("P411", "B"): WEEK + timedelta(days=3)},
    },
    "g40_everything_mild": {
        "n_instructors": 2,
        "n_students": 2,
        "n_mature": 1,
        "n_aircraft": 3,
        "n_missions": 4,
        "two_types": True,
        "unavailable": {"P412": (_day(5),)},
        "maintenance": {"AC702": (_day(6),)},
        "airspace_capacity_override": {"BLD": 1},
        "closed_runways": (),
    },
}


def _scenario(name: str) -> ScenarioSpec:
    return build_scenario(label=name, **GOLDEN_CASES[name])


def _snapshot(name: str) -> dict[str, Any]:
    """一个用例的完整基线快照。"""
    scenario = _scenario(name)
    outcome = solve(scenario.to_bundle())
    payload: dict[str, Any] = {
        "status": outcome.status,
        "num_candidates": outcome.stats.num_candidates,
        "num_sorties": len(outcome.sorties),
        "blocked_items": [
            {
                "person_id": b.person_id,
                "mission_id": b.mission_id,
                "reason": b.reason,
                "missing_prereqs": list(b.missing_prereqs),
            }
            for b in outcome.blocked_items
        ],
    }
    if outcome.plan is None:
        return payload

    plan = outcome.plan
    payload["content_sha256"] = plan.content_sha256
    payload["relaxation_tier"] = plan.relaxation_tier
    payload["debts"] = [d.model_dump(mode="json") for d in plan.debts]
    payload["sorties"] = [
        {
            "sortie_id": s.sortie_id,
            "date": s.date.isoformat(),
            "weekday": s.weekday,
            "takeoff": s.takeoff.strftime("%H:%M"),
            "landing": s.landing.strftime("%H:%M"),
            "mission_id": s.mission_id,
            "airspace_id": s.airspace_id,
            "aircraft_id": s.aircraft_id,
            "runway_id": s.runway_id,
            "is_recurrent": s.is_recurrent,
            "crew": [f"{m.person_id}/{m.role}" for m in s.crew],
        }
        for s in plan.sorties
    ]

    ctx = scenario.to_validation_context()
    report = run_all_checks(plan, ctx)
    payload["validation"] = {
        "all_passed": bool(report.all_passed),
        "total_checked_items": report.total_checked_items,
        "rules": {
            r.rule_id: {
                "passed": r.passed,
                "checked": r.checked_items,
                "violations": len(r.violations),
            }
            for r in report.results
        },
        "notes": report.all_notes(),
    }
    naive = naive_check_all(plan, ctx)
    payload["naive"] = {"passed": naive.passed, "violated_rules": sorted(naive.violated_rules())}
    return payload


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_golden_plan(name: str, data_regression: Any) -> None:
    """固定输入 → 固定输出逐字节比对。"""
    data_regression.check(_snapshot(name), basename=name)


def test_golden_catalog_size() -> None:
    """v6 §12.1 要求「黄金用例 ~40」。少于 40 就是覆盖退化了。"""
    assert len(GOLDEN_CASES) >= 40


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_all_golden_cases_reach_optimal(name: str) -> None:
    """全部用例必须证到 `OPTIMAL` —— 逐字节比对的前提（v6 §3.11.1）。

    掉到 `FEASIBLE` 说明该用例被预算截断了，此时解不保证唯一，**该修的是用例的
    规模或预算，不是把这条断言放宽**。
    """
    outcome = solve(_scenario(name).to_bundle())
    assert outcome.status in ("OPTIMAL", "INFEASIBLE"), (
        f"{name}: {outcome.status} —— 黄金用例不能停在 FEASIBLE/UNKNOWN"
    )


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_golden_plans_pass_both_checkers(name: str) -> None:
    """黄金用例同时是一批「求解器出解必过双通道」的确定性回归。"""
    scenario = _scenario(name)
    outcome = solve(scenario.to_bundle())
    if outcome.plan is None:
        return
    ctx = scenario.to_validation_context()
    assert run_all_checks(outcome.plan, ctx).all_passed
    assert naive_check_all(outcome.plan, ctx).passed
