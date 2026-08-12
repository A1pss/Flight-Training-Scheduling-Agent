"""单点违规注入 —— `inject_single_violation()`（v6 §12.1）。

每个注入都是「在 :func:`tests.property.world.compliant_plan` 这份**合法**方案上
改一个地方」，并声明它**必须**命中哪条规则。v6 §12.1 点名的三处新增形态与
CC_PROMPTS 追加的第四处都在这里，且都标了 `exclusive=True`（**只准命中那一条**）：

| 形态 | 注入 | 期望 |
|---|---|---|
| ① 空域并发超容量 | `c06_airspace_over_capacity` | 只命中 **C06** |
| ② 同一跑道 20 分钟内 3 次起飞 | `c09_three_takeoffs_same_runway` | 只命中 **C09** |
| ③ 全场 7 分钟内两次起飞、**分属两条跑道** | `c09_seven_minute_across_runways` | 只命中 **C09** |
| ④ A 类架次带教员 | `c03_solo_a_class_with_instructor` | 命中 **C03 与 C05** |

第 ③ 条是 D-2 口径的唯一守门人：把 7 分钟实现成「按跑道分组」的话，它一条违规
都报不出来。第 ④ 条是 D-1 的反向验证。

## `exclusive` 的含义

`exclusive=True` 表示「注入点是**单点**的，判定集合必须**恰好等于** `expected`」。
有些违规在物理上无法孤立（比如「同一人一周飞 11 次」必然同时撞上约束14 的
`req_max`），那些注入标 `exclusive=False`，只断言 `expected ⊆ 判定集合` ——
这正是 v6 §12.1 那条属性测试的原文写法（`assert expected_rule in {...}`）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from backend.schemas.plan import SchedulePlan
from backend.validator.context import ValidationContext
from tests.property.world import (
    BASELINE_BLOCKED,
    BASELINE_DRAFTS,
    SortieDraft,
    make_plan,
)

Drafts = tuple[SortieDraft, ...]


# ─────────────────────────────────────────────────────────────────────
# 草稿层的小工具
# ─────────────────────────────────────────────────────────────────────
def _patch(drafts: Drafts, index: int, **changes: object) -> Drafts:
    out = list(drafts)
    out[index] = replace(out[index], **changes)  # type: ignore[arg-type]
    return tuple(out)


def _drop(drafts: Drafts, index: int) -> Drafts:
    return tuple(d for i, d in enumerate(drafts) if i != index)


def _add(drafts: Drafts, *extra: SortieDraft) -> Drafts:
    return (*drafts, *extra)


def solo(
    day: int, start: int, mission: str, trainee: str, aircraft: str, runway: str
) -> SortieDraft:
    return SortieDraft(
        day=day,
        start=start,
        mission_id=mission,
        trainee_id=trainee,
        aircraft_id=aircraft,
        runway_id=runway,
    )


def dual(
    day: int, start: int, mission: str, trainee: str, instructor: str, aircraft: str, runway: str
) -> SortieDraft:
    return SortieDraft(
        day=day,
        start=start,
        mission_id=mission,
        trainee_id=trainee,
        aircraft_id=aircraft,
        runway_id=runway,
        instructor_id=instructor,
    )


# ─────────────────────────────────────────────────────────────────────
# 注入定义
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Injection:
    """一次单点注入。`mutate` 只改草稿列表，方案的组装统一走 `make_plan`。"""

    name: str
    rule: str
    expected: tuple[str, ...]
    exclusive: bool
    note: str
    mutate: Callable[[Drafts], Drafts]

    def apply(self, ctx: ValidationContext) -> SchedulePlan:
        return make_plan(
            self.mutate(BASELINE_DRAFTS),
            ctx,
            blocked=BASELINE_BLOCKED,
            plan_id=f"PROP-{self.name}",
        )


def _one(
    name: str,
    rule: str,
    note: str,
    mutate: Callable[[Drafts], Drafts],
    *,
    expected: Sequence[str] | None = None,
    exclusive: bool = True,
) -> Injection:
    return Injection(
        name=name,
        rule=rule,
        expected=tuple(expected or (rule,)),
        exclusive=exclusive,
        note=note,
        mutate=mutate,
    )


#: 全部注入形态。14 条规则**每条至少一种**，v6 点名的四处各有专条。
INJECTIONS: tuple[Injection, ...] = (
    # ── 约束1 时间一致性 ──────────────────────────────────────────────
    _one(
        "c01_duration_mismatch",
        "C01",
        "着陆时刻比课目标准时长晚 7 分钟",
        lambda d: _patch(d, 0, landing_delta=7),
    ),
    _one(
        "c01_before_window",
        "C01",
        "起飞提前到 05:30，落在训练窗之外",
        lambda d: _patch(d, 0, start=-30),
    ),
    _one(
        "c01_weekday_mismatch",
        "C01",
        "星期列与日期不自洽（周一写成周日）",
        lambda d: _patch(d, 0, weekday="周日"),
    ),
    # ── 约束2 人员可用性 ──────────────────────────────────────────────
    _one(
        "c02_instructor_unavailable",
        "C02",
        "第 0 天新增一个带飞架次，教员 P402 当日不可用",
        lambda d: _add(d, dual(0, 100, "missionB-1", "P412", "P402", "AC702", "RWY-8")),
    ),
    # ── 约束3 角色配置 ────────────────────────────────────────────────
    _one(
        "c03_solo_a_class_with_instructor",
        "C03",
        "★④ A 类单飞架次硬塞一名教员（D-1 的反向验证）",
        lambda d: _patch(d, 0, instructor_id="P401"),
        expected=("C03", "C05"),
    ),
    _one(
        "c03_weekly_a_class_missing",
        "C03",
        "删掉 P413 本周唯一的 A 类架次（S-02 + S-13）",
        lambda d: _drop(d, 4),
    ),
    _one(
        "c03_instructor_as_trainee",
        "C03",
        "带飞架次的学员岗换成教员 P402",
        lambda d: _patch(d, 2, trainee_id="P402"),
        expected=("C03", "C04"),
        exclusive=False,
    ),
    # ── 约束4 资质匹配与岗位互斥 ──────────────────────────────────────
    _one(
        "c04_missing_class_qualification",
        "C04",
        "只持 A 类资质的 P413 去飞 B 类课目",
        lambda d: _add(d, dual(6, 0, "missionB-1", "P413", "P401", "AC702", "RWY-7")),
    ),
    _one(
        "c04_person_time_overlap",
        "C04",
        "P411 在第 2 天多出一个与他的 B-1 时间重叠的架次",
        lambda d: _add(d, solo(2, 10, "missionA-1", "P411", "AC701", "RWY-8")),
        expected=("C04", "C08"),
        exclusive=False,
    ),
    # ── 约束5 机型与机组编成 ──────────────────────────────────────────
    _one(
        "c05_aircraft_type_not_held",
        "C05",
        "把学员的架次换到 TX-2 机上（学员只持 TX-1）",
        lambda d: _patch(d, 0, aircraft_id="AC703"),
    ),
    _one(
        "c05_dual_flown_solo",
        "C05",
        "带飞课目去掉教员，机组只剩 1 人",
        lambda d: _patch(d, 2, instructor_id=None),
        expected=("C03", "C05"),
    ),
    # ── 约束6 资源有效性与容量 ────────────────────────────────────────
    _one(
        "c06_airspace_over_capacity",
        "C06",
        "★① 容量为 1 的 NAV 空域里两个架次时间重叠",
        lambda d: _add(d, dual(2, 10, "missionB-1", "P412", "P402", "AC701", "RWY-8")),
    ),
    _one(
        "c06_wrong_airspace",
        "C06",
        "架次的空域与课目绑定空域不符",
        lambda d: _patch(d, 0, airspace_id="NAV"),
    ),
    # ⚠️ 「机号不在册」是**引用完整性**问题，v6 §4.3 把它归在闸门2
    # （`check_referential_integrity`），不是闸门1 的 14 条。主校验器额外报 C05
    # （「查不到机型 → 无法证明机组持有该机型资质」），naive 只报 C06 ——
    # 两种读法都说得通，故此条 `exclusive=False`，且**不进对拍**
    # （对拍前置条件：方案先过闸门2，见 `test_naive_vs_main.py`）。
    _one(
        "c06_unknown_aircraft",
        "C06",
        "机号不在在册列表里（引用完整性，闸门2 的管辖范围）",
        lambda d: _patch(d, 0, aircraft_id="AC999"),
        exclusive=False,
    ),
    # ── 约束7 飞机排期冲突与周转 ──────────────────────────────────────
    _one(
        "c07_turnaround_too_short",
        "C07",
        "同机相邻架次间隔 10 分钟 < 周转 20 分钟（S-06 从着陆算到起飞）",
        lambda d: _add(d, solo(0, 40, "missionA-1", "P412", "AC701", "RWY-8")),
    ),
    _one(
        "c07_inside_maintenance",
        "C07",
        "把架次排进 AC702 第 5 天的全天维护窗",
        lambda d: _add(d, solo(5, 0, "missionA-1", "P421", "AC702", "RWY-7")),
    ),
    # ── 约束8 人员冲突与休息 ──────────────────────────────────────────
    _one(
        "c08_gap_below_minimum",
        "C08",
        "同一人同日相邻架次地面间隔 5 分钟 < 10 分钟",
        lambda d: _add(d, solo(0, 35, "missionA-1", "P411", "AC702", "RWY-8")),
    ),
    _one(
        "c08_rest_after_two",
        "C08",
        "连续 2 架次后第 3 架次前只休息 10 分钟 < 30 分钟",
        lambda d: _add(
            d,
            solo(6, 0, "missionA-1", "P411", "AC701", "RWY-7"),
            solo(6, 40, "missionA-1", "P411", "AC702", "RWY-7"),
            solo(6, 80, "missionA-2", "P411", "AC701", "RWY-7"),
        ),
    ),
    # ── 约束9 起降密度 ────────────────────────────────────────────────
    _one(
        "c09_three_takeoffs_same_runway",
        "C09",
        "★② 同一跑道 20 分钟窗口内 3 次起飞（彼此间隔 8 分钟，不触发 7 分钟那条）",
        lambda d: _add(
            d,
            solo(6, 0, "missionA-1", "P411", "AC701", "RWY-7"),
            solo(6, 8, "missionA-2", "P412", "AC702", "RWY-7"),
            solo(6, 16, "missionB-1", "P421", "AC703", "RWY-7"),
        ),
    ),
    _one(
        "c09_seven_minute_across_runways",
        "C09",
        "★③ 两次起飞相隔 3 分钟、**分属两条跑道**（D-2：7 分钟是全场口径）",
        lambda d: _add(
            d,
            solo(6, 0, "missionA-1", "P411", "AC701", "RWY-7"),
            solo(6, 3, "missionA-1", "P412", "AC702", "RWY-8"),
        ),
    ),
    _one(
        "c09_runway_not_serving_type",
        "C09",
        "TX-2 的架次落在只服务 TX-1 的 RWY-8 上",
        lambda d: _add(d, solo(6, 0, "missionA-1", "P421", "AC703", "RWY-8")),
    ),
    # ── 约束10~12 线性上限 ────────────────────────────────────────────
    _one(
        "c10_daily_minutes_exceeded",
        "C10",
        "学员单日 3×90 = 270 分钟 > 240 分钟",
        lambda d: _add(
            d,
            solo(6, 0, "missionA-2", "P411", "AC701", "RWY-7"),
            solo(6, 130, "missionA-2", "P411", "AC702", "RWY-7"),
            solo(6, 260, "missionA-2", "P411", "AC701", "RWY-8"),
        ),
    ),
    _one(
        "c11_weekly_sorties_exceeded",
        "C11",
        "学员一周 11 架次 > 10 架次（必然连带撞上约束14 的 req_max）",
        lambda d: _add(
            d,
            *[
                solo(day, start, "missionA-1", "P413", "AC702", "RWY-8")
                for day in (0, 1, 2, 3, 6)
                for start in (200, 400)
            ],
        ),
        exclusive=False,
    ),
    _one(
        "c12_person_daily_exceeded",
        "C12",
        "单人单日 4 架次 > 3 架次（用 2×A-1 + 2×A-2，避开 req_max=3 的连带违规）",
        lambda d: _add(
            d,
            solo(6, 0, "missionA-1", "P411", "AC701", "RWY-7"),
            solo(6, 60, "missionA-1", "P411", "AC702", "RWY-7"),
            solo(6, 120, "missionA-2", "P411", "AC701", "RWY-7"),
            solo(6, 240, "missionA-2", "P411", "AC702", "RWY-7"),
        ),
    ),
    _one(
        "c12_aircraft_daily_exceeded",
        "C12",
        "单机单日 7 架次 > 6 架次",
        lambda d: _add(
            d,
            solo(6, 0, "missionA-1", "P411", "AC701", "RWY-7"),
            solo(6, 50, "missionA-1", "P412", "AC701", "RWY-7"),
            solo(6, 100, "missionA-1", "P413", "AC701", "RWY-7"),
            solo(6, 150, "missionA-1", "P421", "AC701", "RWY-7"),
            solo(6, 200, "missionA-2", "P411", "AC701", "RWY-7"),
            solo(6, 310, "missionA-2", "P412", "AC701", "RWY-7"),
            solo(6, 420, "missionA-2", "P413", "AC701", "RWY-7"),
        ),
    ),
    # ── 约束13 任务完成度 ─────────────────────────────────────────────
    _one(
        "c13_blocked_mission_scheduled",
        "C13",
        "把先修未满足（BLOCKED）的 P411 × missionC-1 排上去",
        lambda d: _add(d, dual(6, 0, "missionC-1", "P411", "P401", "AC702", "RWY-7")),
    ),
    _one(
        "c13_frequency_window_missed",
        "C13",
        "删掉 P411 的 B-1，本周唯一的 7 天窗口落空",
        lambda d: _drop(d, 2),
    ),
    # ── 约束14 任务唯一性 ─────────────────────────────────────────────
    _one(
        "c14_exact_duplicate",
        "C14",
        "完全重复的架次记录（只有架次号不同）",
        lambda d: _add(d, d[0]),
        exclusive=False,
    ),
    _one(
        "c14_req_max_exceeded",
        "C14",
        "freq=7 的课目（req_max=1）在同一周排了 2 次",
        lambda d: _add(d, dual(6, 0, "missionB-1", "P411", "P401", "AC702", "RWY-7")),
    ),
)

#: 名字 → 注入，方便确定性用例按名取用
BY_NAME: dict[str, Injection] = {inj.name: inj for inj in INJECTIONS}

#: v6 §12.1 点名必测的四处形态
MANDATORY_FORMS: tuple[str, ...] = (
    "c06_airspace_over_capacity",
    "c09_three_takeoffs_same_runway",
    "c09_seven_minute_across_runways",
    "c03_solo_a_class_with_instructor",
)


# ─────────────────────────────────────────────────────────────────────
# 与布局无关的注入（可施加在**任意**随机合法方案上）
# ─────────────────────────────────────────────────────────────────────
#: 基线里「学员单飞」的三个下标（A 类，D-1 之下不带教员）
SOLO_INDICES: tuple[int, ...] = (0, 1, 4)
#: 基线里「带飞」的两个下标
DUAL_INDICES: tuple[int, ...] = (2, 3)


@dataclass(frozen=True)
class LayoutFreeInjection:
    """不依赖基线具体日期/时刻的注入 —— 施加在 `arbitrary_schedule_plan` 的产物上。

    这些注入只改**已有架次的属性**或删掉一个架次，不新增架次，所以无论随机方案
    把架次摆在哪一天、哪个时刻，破坏点与期望规则都不变。
    """

    name: str
    rule: str
    expected: tuple[str, ...]
    exclusive: bool
    note: str
    mutate: Callable[[Drafts], Drafts]


LAYOUT_FREE_INJECTIONS: tuple[LayoutFreeInjection, ...] = (
    LayoutFreeInjection(
        "lf_c01_duration",
        "C01",
        ("C01",),
        True,
        "着陆时刻比标准时长晚 7 分钟",
        lambda d: _patch(d, 0, landing_delta=7),
    ),
    LayoutFreeInjection(
        "lf_c01_weekday",
        "C01",
        ("C01",),
        True,
        "星期列改成固定的「周日」，与日期不自洽（随机日期下几乎必然对不上）",
        lambda d: _patch(d, 1, weekday="周三" if d[1].day != 2 else "周四"),
    ),
    LayoutFreeInjection(
        "lf_c06_airspace",
        "C06",
        ("C06",),
        True,
        "架次空域改成 LAD，与课目绑定空域不符",
        lambda d: _patch(d, 0, airspace_id="LAD"),
    ),
    LayoutFreeInjection(
        "lf_c05_aircraft_type",
        "C05",
        ("C05",),
        True,
        "学员单飞架次换到 TX-2 机上（并落在服务 TX-2 的 RWY-7，隔离出 C05）",
        lambda d: _patch(d, 4, aircraft_id="AC703", runway_id="RWY-7"),
    ),
    LayoutFreeInjection(
        "lf_c03_add_instructor",
        "C03",
        ("C03", "C05"),
        True,
        "★④ A 类单飞架次加一名教员",
        lambda d: _patch(d, 0, instructor_id="P401"),
    ),
    LayoutFreeInjection(
        "lf_c05_remove_instructor",
        "C05",
        ("C03", "C05"),
        True,
        "带飞架次去掉教员",
        lambda d: _patch(d, 2, instructor_id=None),
    ),
    LayoutFreeInjection(
        "lf_c03_drop_weekly",
        "C03",
        ("C03",),
        True,
        "删掉 P413 本周唯一的 A 类架次",
        lambda d: _drop(d, 4),
    ),
    LayoutFreeInjection(
        "lf_c13_drop_required",
        "C13",
        ("C13",),
        True,
        "删掉 P411 的 B-1，频率窗口落空",
        lambda d: _drop(d, 2),
    ),
    LayoutFreeInjection(
        "lf_c14_duplicate",
        "C14",
        ("C14",),
        False,
        "完全重复一个架次（必然连带撞上同人/同机/同刻的那几条）",
        lambda d: _add(d, d[0]),
    ),
    LayoutFreeInjection(
        "lf_c04_unqualified_trainee",
        "C04",
        ("C04", "C13"),
        False,
        "把 B 类带飞的学员换成只持 A 类资质的 P413（连带让 P411 的 B-1 落空）",
        lambda d: _patch(d, 2, trainee_id="P413"),
    ),
)


def inject_single_violation(
    plan: SchedulePlan, ctx: ValidationContext, injection: Injection
) -> tuple[SchedulePlan, str]:
    """v6 §12.1 的 `inject_single_violation`：合法方案 → (被改坏的方案, 期望规则)。

    `plan` 只用来做「注入前后必须不同」的对照断言 —— 破坏动作在草稿层完成，
    所以注入结果与传进来的 `plan` 同源同参（都由 `BASELINE_DRAFTS` 生成）。
    """
    broken = injection.apply(ctx)
    assert broken.content_sha256 != plan.content_sha256 or broken.plan_id != plan.plan_id, (
        f"注入 {injection.name} 没有改变任何东西"
    )
    return broken, injection.rule


def inject_layout_free(
    drafts: Drafts, ctx: ValidationContext, injection: LayoutFreeInjection
) -> tuple[SchedulePlan, str]:
    """把与布局无关的注入施加在任意（随机生成的）合法草稿列表上。"""
    return (
        make_plan(
            injection.mutate(drafts),
            ctx,
            blocked=BASELINE_BLOCKED,
            plan_id=f"PROP-{injection.name}",
        ),
        injection.rule,
    )


def rules_covered() -> frozenset[str]:
    return frozenset(inj.rule for inj in INJECTIONS)


__all__ = [
    "BY_NAME",
    "DUAL_INDICES",
    "INJECTIONS",
    "LAYOUT_FREE_INJECTIONS",
    "MANDATORY_FORMS",
    "SOLO_INDICES",
    "Injection",
    "LayoutFreeInjection",
    "dual",
    "inject_layout_free",
    "inject_single_violation",
    "rules_covered",
    "solo",
]
