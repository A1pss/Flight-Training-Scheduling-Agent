"""属性测试 · 注入违规 → 校验器必须定位到正确的规则编号（v6 §12.1 第二条）。

```python
@given(plan=arbitrary_schedule_plan(ctx))
def test_validator_catches_injected_violations(plan):
    broken, expected_rule = inject_single_violation(plan)
    report = run_all_checks(broken, ctx)
    assert expected_rule in {v.rule_id for v in report.all_violations()}
```

本文件把这条属性拆成三层：

1. **基线必须合法** —— 手工排的 5 架次，两条通道（主校验器 + naive checker）
   同时确认。基线不合法的话，下面全部是空转。
2. **29 种确定性注入** —— 14 条规则每条至少一种形态，v6 点名的四处各有专条且
   `exclusive=True`（只准命中那一条）。
3. **随机合法方案 × 与布局无关的注入** —— 真正的 `@given` 形态。
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from backend.validator import run_all_checks
from backend.validator.context import ValidationContext
from tests.naive_checker import naive_check_all
from tests.property.injections import (
    BY_NAME,
    INJECTIONS,
    LAYOUT_FREE_INJECTIONS,
    MANDATORY_FORMS,
    Injection,
    inject_layout_free,
    inject_single_violation,
    rules_covered,
)
from tests.property.plans import arbitrary_drafts, arbitrary_schedule_plan, plan_from_drafts
from tests.property.world import compliant_plan, injection_world

pytestmark = pytest.mark.property

PROP_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


@pytest.fixture(scope="module")
def ctx() -> ValidationContext:
    return injection_world().to_validation_context()


def hard_rules(plan: object, context: ValidationContext) -> frozenset[str]:
    report = run_all_checks(plan, context)  # type: ignore[arg-type]
    return frozenset(v.rule_id for v in report.all_violations() if v.severity == "HARD")


# ─────────────────────────────────────────────────────────────────────
# 1. 基线合法性
# ─────────────────────────────────────────────────────────────────────
def test_baseline_plan_is_legal_on_both_channels(ctx: ValidationContext) -> None:
    """手工排的基线方案必须两条通道同时判过 —— 否则后面的注入用例全是空转。"""
    plan = compliant_plan(ctx)
    report = run_all_checks(plan, ctx)
    naive = naive_check_all(plan, ctx)
    assert report.all_passed, [v.detail for v in report.all_violations()]
    assert naive.passed, [v.detail for v in naive.violations]
    assert report.missing_rules() == []


def test_baseline_discloses_the_blocked_combination(ctx: ValidationContext) -> None:
    """P411 × missionC-1 先修未满足 → 必须在 `blocked_items` 里，措辞逐字。"""
    plan = compliant_plan(ctx)
    assert [(b.person_id, b.mission_id, b.reason) for b in plan.blocked_items] == [
        ("P411", "missionC-1", "missionB-1 未完成")
    ]


# ─────────────────────────────────────────────────────────────────────
# 2. 确定性注入（14 条规则逐条 + v6 点名的四处）
# ─────────────────────────────────────────────────────────────────────
def test_injections_cover_all_fourteen_rules() -> None:
    """注入集合必须覆盖 14 条规则，一条都不许漏。"""
    assert rules_covered() == frozenset(f"C{i:02d}" for i in range(1, 15))


def test_mandatory_forms_are_present() -> None:
    """v6 §12.1 点名必测的四处形态必须在注入集合里。"""
    for name in MANDATORY_FORMS:
        assert name in BY_NAME, name


@pytest.mark.parametrize("injection", INJECTIONS, ids=lambda i: i.name)
def test_main_validator_catches_injected_violation(
    injection: Injection, ctx: ValidationContext
) -> None:
    """v6 §12.1 的核心断言：`expected_rule in {v.rule_id for v in ...}`。"""
    broken, expected = inject_single_violation(compliant_plan(ctx), ctx, injection)
    found = hard_rules(broken, ctx)
    assert expected in found, (
        f"{injection.name}（{injection.note}）期望 {expected}，实际 {sorted(found)}"
    )
    for rule in injection.expected:
        assert rule in found, f"{injection.name} 期望包含 {rule}，实际 {sorted(found)}"
    if injection.exclusive:
        assert found == frozenset(injection.expected), (
            f"{injection.name} 是单点注入，判定集合应恰好为 {sorted(injection.expected)}，"
            f"实际 {sorted(found)}"
        )


@pytest.mark.parametrize("injection", INJECTIONS, ids=lambda i: i.name)
def test_naive_checker_catches_injected_violation(
    injection: Injection, ctx: ValidationContext
) -> None:
    """第三方 naive checker 必须独立地得出同样的结论。"""
    broken = injection.apply(ctx)
    found = naive_check_all(broken, ctx).violated_rules()
    for rule in injection.expected:
        assert rule in found, f"{injection.name} 期望包含 {rule}，实际 {sorted(found)}"
    if injection.exclusive:
        assert found == frozenset(injection.expected), (
            f"{injection.name}: naive 判定 {sorted(found)} ≠ 期望 {sorted(injection.expected)}"
        )


@pytest.mark.parametrize("name", MANDATORY_FORMS)
def test_mandatory_form_hits_exactly_the_named_rule(name: str, ctx: ValidationContext) -> None:
    """★①②③④ 四处必测形态：命中集合必须**恰好**等于 v6 点名的那一条（组）。"""
    injection = BY_NAME[name]
    broken = injection.apply(ctx)
    assert hard_rules(broken, ctx) == frozenset(injection.expected)
    assert naive_check_all(broken, ctx).violated_rules() == frozenset(injection.expected)


def test_seven_minute_rule_is_airport_wide_not_per_runway(ctx: ValidationContext) -> None:
    """★③ D-2 的唯一守门人：两次起飞相隔 3 分钟、分属两条跑道，**必须**命中 C09。

    7 分钟若被实现成「按跑道分组」，这里会是 0 条违规。
    """
    broken = BY_NAME["c09_seven_minute_across_runways"].apply(ctx)
    report = run_all_checks(broken, ctx)
    c09 = [v for v in report.all_violations() if v.rule_id == "C09"]
    assert len(c09) == 1, [v.detail for v in report.all_violations()]
    runways = {s.runway_id for s in broken.sorties if s.date == broken.week_start.replace()}
    assert "RWY-7" in runways or "RWY-8" in runways
    naive = naive_check_all(broken, ctx).by_rule("C09")
    assert len(naive) == 1, [v.detail for v in naive]


def test_a_class_with_instructor_is_rejected(ctx: ValidationContext) -> None:
    """★④ D-1 反向验证：A-1/A-2 的带飞列为「否」→ 学员 A 类单飞，加教员即违规。"""
    broken = BY_NAME["c03_solo_a_class_with_instructor"].apply(ctx)
    found = hard_rules(broken, ctx)
    assert found == frozenset({"C03", "C05"})


def test_airspace_capacity_is_a_hard_constraint(ctx: ValidationContext) -> None:
    """★① S-10：空域同时段容量是硬约束，超容量必须命中 C06。"""
    broken = BY_NAME["c06_airspace_over_capacity"].apply(ctx)
    assert "C06" in hard_rules(broken, ctx)


def test_three_takeoffs_in_twenty_minutes_same_runway(ctx: ValidationContext) -> None:
    """★② S-04 + D-2：同一跑道 20 分钟窗口内 3 次起飞必须命中 C09。"""
    broken = BY_NAME["c09_three_takeoffs_same_runway"].apply(ctx)
    assert "C09" in hard_rules(broken, ctx)


# ─────────────────────────────────────────────────────────────────────
# 3. 随机合法方案 × 与布局无关的注入（真正的 @given 形态）
# ─────────────────────────────────────────────────────────────────────
@given(data=st.data())
@PROP_SETTINGS
def test_generated_plans_are_legal(data: st.DataObject, ctx: ValidationContext) -> None:
    """`arbitrary_schedule_plan(ctx)` 必须**只**产出合法方案（构造保证，不靠过滤）。

    这条同时是「两条通道在合法方案上都不误报」的属性测试。
    """
    plan = data.draw(arbitrary_schedule_plan(ctx))
    report = run_all_checks(plan, ctx)
    naive = naive_check_all(plan, ctx)
    assert report.all_passed, [v.detail for v in report.all_violations()]
    assert naive.passed, [v.detail for v in naive.violations]


@given(data=st.data(), injection=st.sampled_from(LAYOUT_FREE_INJECTIONS))
@PROP_SETTINGS
def test_validator_catches_injected_violations(
    data: st.DataObject, injection: object, ctx: ValidationContext
) -> None:
    """v6 §12.1 原文那条属性测试。随机合法方案 → 注入单点违规 → 命中正确编号。"""
    drafts = data.draw(arbitrary_drafts(ctx))
    legal = plan_from_drafts(drafts, ctx)
    assert run_all_checks(legal, ctx).all_passed
    broken, expected_rule = inject_layout_free(drafts, ctx, injection)  # type: ignore[arg-type]
    found = hard_rules(broken, ctx)
    assert expected_rule in found, f"{injection.name} 期望 {expected_rule}，实际 {sorted(found)}"  # type: ignore[attr-defined]


@given(data=st.data(), injection=st.sampled_from(LAYOUT_FREE_INJECTIONS))
@PROP_SETTINGS
def test_naive_checker_catches_injected_violations(
    data: st.DataObject, injection: object, ctx: ValidationContext
) -> None:
    """同一条属性，换成第三方 naive checker 判。"""
    drafts = data.draw(arbitrary_drafts(ctx))
    broken, expected_rule = inject_layout_free(drafts, ctx, injection)  # type: ignore[arg-type]
    found = naive_check_all(broken, ctx).violated_rules()
    assert expected_rule in found, f"{injection.name} 期望 {expected_rule}，实际 {sorted(found)}"  # type: ignore[attr-defined]


@given(data=st.data(), injection=st.sampled_from(LAYOUT_FREE_INJECTIONS))
@PROP_SETTINGS
def test_two_checkers_agree_on_injected_plans(
    data: st.DataObject, injection: object, ctx: ValidationContext
) -> None:
    """**对拍**：两条独立实现在被注入的随机方案上判定集合必须逐条一致。"""
    drafts = data.draw(arbitrary_drafts(ctx))
    broken, _ = inject_layout_free(drafts, ctx, injection)  # type: ignore[arg-type]
    main = hard_rules(broken, ctx)
    naive = naive_check_all(broken, ctx).violated_rules()
    assert main == naive, (
        f"{injection.name} 分歧：仅主校验器 {sorted(main - naive)}，"  # type: ignore[attr-defined]
        f"仅 naive {sorted(naive - main)}"
    )


@given(data=st.data(), injection=st.sampled_from(LAYOUT_FREE_INJECTIONS))
@PROP_SETTINGS
def test_exclusive_layout_free_injections_stay_single_point(
    data: st.DataObject, injection: object, ctx: ValidationContext
) -> None:
    """标了 `exclusive` 的注入，无论方案怎么摆，命中集合都必须恰好是那一组。"""
    if not injection.exclusive:  # type: ignore[attr-defined]
        return
    drafts = data.draw(arbitrary_drafts(ctx))
    broken, _ = inject_layout_free(drafts, ctx, injection)  # type: ignore[arg-type]
    assert hard_rules(broken, ctx) == frozenset(injection.expected)  # type: ignore[attr-defined]
