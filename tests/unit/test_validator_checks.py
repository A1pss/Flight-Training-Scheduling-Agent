"""14 条 check 的单元测试：每条一个合规样本 + 至少两个违规样本。

样本全部由 `tests/fixtures/validator_facts.py` 手工构造（**没有跑过求解器**，
CLAUDE.md 铁律 2）。违规样本一律在合规样本上做**单点改写**，这样「校验器精确
定位到正确的 rule_id」才是被真正验证的 —— 一次改十处的样本谁都能报错。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, time
from typing import Any

import pytest

from backend.core.ruleset import get_semantics
from backend.models.entities import AircraftMaintenance
from backend.schemas.plan import CrewMember, SchedulePlan, Sortie
from backend.schemas.validation import CheckResult, ValidationReport
from backend.validator.checks import (
    ALL_CHECKS,
    RULE_TITLES,
    S11_AUTHORIZED_REWRITE_NOTE,
    check_c01,
    check_c02,
    check_c03,
    check_c04,
    check_c05,
    check_c06,
    check_c07,
    check_c08,
    check_c09,
    check_c10,
    check_c11,
    check_c12,
    check_c13,
    check_c14,
    expected_crew_size,
    run_all_checks,
)
from backend.validator.context import ContextRows, ValidationContext, context_from_rows
from tests.fixtures.validator_facts import (
    BLOCKED_EXPECTED,
    SNAPSHOT,
    WEEK_START,
    baseline_context,
    baseline_rows,
    compliant_plan,
    compliant_sorties,
    debt,
    image4_plan,
    make_plan,
    make_sortie,
)


# ─────────────────────────────────────────────────────────────────────
# 夹具与小工具
# ─────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def ctx() -> ValidationContext:
    return baseline_context()


@pytest.fixture(scope="module")
def plan() -> SchedulePlan:
    return compliant_plan()


def context_with(mutate: Callable[[ContextRows], None]) -> ValidationContext:
    """在基准事实上做单点改写后重新装配上下文。"""
    rows = baseline_rows()
    mutate(rows)
    return context_from_rows(rows, week_start=WEEK_START, snapshot_id=SNAPSHOT)


def variant(
    *,
    replace: Mapping[str, Mapping[str, Any]] | None = None,
    drop: Iterable[str] = (),
    add: Iterable[Sortie] = (),
    **plan_kwargs: Any,
) -> SchedulePlan:
    """在合规样本上做单点改写。`validate=False` 让违规样本能绕过闸门2 直达闸门1。"""
    dropped = set(drop)
    sorties: list[Sortie] = []
    for s in compliant_sorties():
        if s.sortie_id in dropped:
            continue
        patch = (replace or {}).get(s.sortie_id)
        sorties.append(s.model_copy(update=dict(patch)) if patch else s)
    sorties.extend(add)
    return make_plan(sorties, validate=False, **plan_kwargs)


def details(result: CheckResult) -> str:
    return " || ".join(v.detail for v in result.violations)


def rules_violated(report: ValidationReport) -> set[str]:
    return {v.rule_id for v in report.all_violations()}


# ─────────────────────────────────────────────────────────────────────
# 合规样本
# ─────────────────────────────────────────────────────────────────────
def test_compliant_plan_passes_all_fourteen(plan: SchedulePlan, ctx: ValidationContext) -> None:
    report = run_all_checks(plan, ctx)
    assert report.missing_rules() == []
    assert len(report.results) == 14
    assert report.all_passed, [v.detail for v in report.all_violations()]


def test_every_check_reports_real_checked_items(plan: SchedulePlan, ctx: ValidationContext) -> None:
    """`checked_items` 不许是 0，也不许是写死的常数（v6 §4.2 脚注）。"""
    report = run_all_checks(plan, ctx)
    zero = [r.rule_id for r in report.results if r.checked_items == 0]
    assert zero == [], f"这些规则检查了 0 项，是假通过：{zero}"
    half = make_plan(compliant_sorties()[:7], validate=False)
    smaller = run_all_checks(half, ctx)
    shrunk = {
        r.rule_id
        for r in smaller.results
        if r.checked_items < next(x.checked_items for x in report.results if x.rule_id == r.rule_id)
    }
    # 架次砍半后，至少这些逐架次计数的规则必须跟着变小；恒定值说明写死了常数
    assert {"C01", "C02", "C04", "C05", "C06", "C09", "C14"} <= shrunk


def test_report_carries_versions_and_titles(plan: SchedulePlan, ctx: ValidationContext) -> None:
    report = run_all_checks(plan, ctx)
    assert report.ruleset_version == ctx.ruleset.version
    assert report.semantics_version == ctx.semantics.version
    assert [r.rule_title for r in report.results] == [
        RULE_TITLES[f"C{i:02d}"] for i in range(1, 15)
    ]
    assert report.duration_ms > 0
    assert len(ALL_CHECKS) == 14


# ─────────────────────────────────────────────────────────────────────
# C01 时间一致性
# ─────────────────────────────────────────────────────────────────────
def test_c01_detects_wrong_duration(ctx: ValidationContext) -> None:
    broken = variant(replace={"S000001": {"landing": time(6, 40)}})  # A-1 时长 30 → 写成 40
    result = check_c01(broken, ctx)
    assert not result.passed
    assert "标准时长 30" in details(result)
    assert result.checked_items == 14


def test_c01_detects_outside_training_window(ctx: ValidationContext) -> None:
    broken = variant(replace={"S000001": {"takeoff": time(5, 30), "landing": time(6, 0)}})
    assert "越出训练窗" in details(check_c01(broken, ctx))


def test_c01_detects_weekday_mismatch(ctx: ValidationContext) -> None:
    broken = variant(replace={"S000001": {"weekday": "周三"}})
    assert "星期列 周三 与日期" in details(check_c01(broken, ctx))


def test_c01_detects_cross_day_landing(ctx: ValidationContext) -> None:
    broken = variant(replace={"S000001": {"takeoff": time(17, 50), "landing": time(6, 20)}})
    assert "跨日" in details(check_c01(broken, ctx))


# ─────────────────────────────────────────────────────────────────────
# C02 人员可用性（含 S-11）
# ─────────────────────────────────────────────────────────────────────
def test_c02_detects_unavailable_person(ctx: ValidationContext) -> None:
    """吴鹏 P03 在 2026-01-05 不可用（v6 §1.2.3 的真实扰动）。"""
    broken = variant(
        replace={
            "S000002": {
                "crew": [
                    CrewMember(person_id="P03", name="吴鹏", role="教员"),
                    CrewMember(person_id="P06", name="张勇", role="学员"),
                ]
            }
        }
    )
    result = check_c02(broken, ctx)
    assert not result.passed
    assert "在 2026-01-05 不可用" in details(result)


def test_c02_detects_expired_qualification_for_student(ctx: ValidationContext) -> None:
    """学员按约束2 **字面**执行：到期次日起该类课目不得安排。"""

    def expire_c_class(rows: ContextRows) -> None:
        for q in rows.person_qualifications:
            if q.person_id == "P06" and q.mission_class == "C":
                q.expiry_date = date(2026, 1, 7)

    result = check_c02(compliant_plan(), context_with(expire_c_class))
    assert not result.passed  # S000007 是 01-08 的 missionC-1
    assert "2026-01-07 到期" in details(result)


def test_c02_allows_expiry_day_itself(ctx: ValidationContext) -> None:
    """到期日**当日**仍可执行（`expiry_inclusive`）。"""

    def expire_on_the_day(rows: ContextRows) -> None:
        for q in rows.person_qualifications:
            if q.person_id == "P06" and q.mission_class == "C":
                q.expiry_date = date(2026, 1, 8)  # S000007 当日

    assert check_c02(compliant_plan(), context_with(expire_on_the_day)).passed


def test_c02_s11_mature_pilot_recurrent_is_not_a_violation(ctx: ValidationContext) -> None:
    """★ S-11：刘斌 2026-01-08 之后飞 C-1 **不报违规**，且报告标注授权改写。"""
    recurrent = make_sortie(
        "S000015",
        3,  # 2026-01-08，C 类到期日 01-07 之后
        "07:20",
        "missionC-1",
        "AC61",
        (("P04", "复训"),),
        is_recurrent=True,
    )
    result = check_c02(variant(add=[recurrent]), ctx)
    assert result.passed, details(result)
    assert S11_AUTHORIZED_REWRITE_NOTE in result.notes
    assert any("S-11 生效实例" in n and "刘斌" in n for n in result.notes)


def test_c02_note_present_even_without_recurrent_sortie(
    plan: SchedulePlan, ctx: ValidationContext
) -> None:
    """只要 S-11 开关为 on，授权改写声明就必须出现（v6 §10.4 区块6 强制项）。"""
    assert S11_AUTHORIZED_REWRITE_NOTE in check_c02(plan, ctx).notes


def test_c02_mature_pilot_after_expiry_must_be_marked_recurrent(ctx: ValidationContext) -> None:
    """S-11 是「转复训」，不是「随便飞」：不标 `is_recurrent` 就是违规。"""
    unmarked = make_sortie(
        "S000015", 3, "07:20", "missionC-1", "AC61", (("P04", "单飞"),), is_recurrent=False
    )
    result = check_c02(variant(add=[unmarked]), ctx)
    assert not result.passed
    assert "应按 S-11 标记为复训架次" in details(result)


# ─────────────────────────────────────────────────────────────────────
# C03 角色配置
# ─────────────────────────────────────────────────────────────────────
def test_c03_detects_instructor_on_a_class_sortie(ctx: ValidationContext) -> None:
    """★ D-1 反向验证：A-1/A-2 的带飞列是「否」→ 学员单飞，带教员即违规。"""
    broken = variant(
        replace={
            "S000005": {  # 何超 missionA-2 单飞 → 硬塞一个教员进去
                "crew": [
                    CrewMember(person_id="P01", name="孙军", role="教员"),
                    CrewMember(person_id="P08", name="何超", role="学员"),
                ]
            }
        }
    )
    c03, c05 = check_c03(broken, ctx), check_c05(broken, ctx)
    assert not c03.passed and not c05.passed
    assert "应为 1 人机组，实际 2 人" in details(c03)
    assert "机组编成应为 1 人" in details(c05)


def test_c03_detects_mature_pilot_in_instructor_seat(ctx: ValidationContext) -> None:
    """image 2/3 的老问题：刘斌身份是成熟飞行员，不能被标成「教员」带飞。"""
    broken = variant(
        replace={
            "S000002": {
                "crew": [
                    CrewMember(person_id="P04", name="刘斌", role="教员"),
                    CrewMember(person_id="P06", name="张勇", role="学员"),
                ]
            }
        }
    )
    assert "不得在 S000002 上担任「教员」角色" in details(check_c03(broken, ctx))
    assert "占据了 S000002 的教员岗" in details(check_c04(broken, ctx))


def test_c03_detects_dual_sortie_without_instructor(ctx: ValidationContext) -> None:
    broken = variant(
        replace={
            "S000002": {
                "crew": [
                    CrewMember(person_id="P05", name="罗磊", role="学员"),
                    CrewMember(person_id="P06", name="张勇", role="学员"),
                ]
            }
        }
    )
    assert "必须为 1 教员 + 1 学员" in details(check_c03(broken, ctx))


def test_c03_detects_solo_sortie_with_instructor_role(ctx: ValidationContext) -> None:
    broken = variant(
        replace={"S000001": {"crew": [CrewMember(person_id="P01", name="孙军", role="教员")]}}
    )
    assert "单人架次角色必须为 单飞/复训" in details(check_c03(broken, ctx))


def test_c03_weekly_class_applies_to_every_student(ctx: ValidationContext) -> None:
    """S-13：约束3 对**全部 4 名学员**生效，不论完成状态。"""
    broken = variant(drop={"S000014"})  # 张勇唯一一次 A 类
    result = check_c03(broken, ctx)
    assert not result.passed
    assert "张勇(P06) 本周 A 类课目" in details(result)
    assert "少于每周必飞下限 1 次" in details(result)


def test_c03_weekly_class_is_data_driven_not_hardcoded_a(ctx: ValidationContext) -> None:
    """「每周必飞」的类别来自课目表的 `weekly_required`，不是写死的「A 类」。"""

    def make_f_weekly(rows: ContextRows) -> None:
        for m in rows.missions:
            if m.mission_id == "missionF-1":
                m.weekly_required = True

    result = check_c03(compliant_plan(), context_with(make_f_weekly))
    assert not result.passed
    assert "本周 F 类课目" in details(result)  # 何超没有 F-1（先修未达标）


def test_c03_weekly_class_shortfall_is_soft_when_tier2_and_disclosed(
    ctx: ValidationContext,
) -> None:
    """Tier 2 显式松弛约束3 且欠账已披露 → SOFT，不拦下方案（v6 §3.10 D-6）。"""
    broken = variant(
        drop={"S000014"},
        relaxation_tier=2,
        debts=[debt("P06", "missionA-1", required=1, scheduled=0, relaxed_by="TIER2")],
    )
    result = check_c03(broken, ctx)
    assert result.violations and all(v.severity == "SOFT" for v in result.violations)
    assert result.passed  # SOFT 不算失败，但报告里看得见


def test_c03_undisclosed_shortfall_stays_hard_even_when_relaxed(ctx: ValidationContext) -> None:
    """欠账不披露就仍是 HARD —— 「欠账 100% 显式披露」是 v6 §0.3 的可测断言。"""
    broken = variant(drop={"S000014"}, relaxation_tier=2)
    result = check_c03(broken, ctx)
    assert not result.passed
    assert all(v.severity == "HARD" for v in result.violations)


# ─────────────────────────────────────────────────────────────────────
# C04 资质匹配与岗位互斥
# ─────────────────────────────────────────────────────────────────────
def test_c04_detects_missing_class_qualification(ctx: ValidationContext) -> None:
    """学员只持 A/B/C/F 四类资质：让罗磊去飞 E 类。"""
    broken = variant(
        add=[
            make_sortie(
                "S000016", 6, "09:00", "missionE-1", "AC84", (("P01", "教员"), ("P05", "学员"))
            )
        ]
    )
    assert "不持 E 类资质" in details(check_c04(broken, ctx))


def test_c04_detects_overlapping_sorties_for_one_person(ctx: ValidationContext) -> None:
    broken = variant(
        add=[
            make_sortie(
                "S000017", 0, "06:15", "missionC-1", "AC34", (("P01", "教员"), ("P07", "学员"))
            )
        ]
    )
    result = check_c04(broken, ctx)
    assert not result.passed
    assert "时间重叠" in details(result)


def test_c04_detects_instructor_as_trainee(ctx: ValidationContext) -> None:
    """S-09：教员不作为受训人排课目。"""
    broken = variant(
        replace={"S000001": {"crew": [CrewMember(person_id="P01", name="孙军", role="单飞")]}}
    )
    assert "教员 孙军(P01) 不作为受训人排课目" in details(check_c04(broken, ctx))


# ─────────────────────────────────────────────────────────────────────
# C05 机型与机组编成
# ─────────────────────────────────────────────────────────────────────
def test_c05_detects_missing_aircraft_type_qualification(ctx: ValidationContext) -> None:
    """学员只持 JL-8：让陈伟上 AC84（JL-9）。"""
    broken = variant(
        add=[
            make_sortie(
                "S000018", 6, "09:00", "missionC-1", "AC84", (("P01", "教员"), ("P07", "学员"))
            )
        ]
    )
    assert "不持 JL-9 机型资质" in details(check_c05(broken, ctx))


def test_c05_detects_crew_over_seats(ctx: ValidationContext) -> None:
    broken = variant(
        replace={
            "S000002": {
                "crew": [
                    CrewMember(person_id="P01", name="孙军", role="教员"),
                    CrewMember(person_id="P02", name="高超", role="教员"),
                    CrewMember(person_id="P06", name="张勇", role="学员"),
                ]
            }
        }
    )
    assert "超过 AC27 座位数 2" in details(check_c05(broken, ctx))


def test_c05_detects_missing_instructor_on_dual_mission(ctx: ValidationContext) -> None:
    broken = variant(
        replace={"S000002": {"crew": [CrewMember(person_id="P06", name="张勇", role="单飞")]}}
    )
    assert "机组编成应为 2 人" in details(check_c05(broken, ctx))


def test_expected_crew_size_matches_311_decision_table() -> None:
    assert expected_crew_size(dual_required=True, trainee_identity="学员") == 2
    assert expected_crew_size(dual_required=False, trainee_identity="学员") == 1  # D-1
    assert expected_crew_size(dual_required=True, trainee_identity="成熟飞行员") == 1  # S-08


# ─────────────────────────────────────────────────────────────────────
# C06 资源有效性与容量
# ─────────────────────────────────────────────────────────────────────
def test_c06_detects_airspace_over_capacity(ctx: ValidationContext) -> None:
    """★ v6 §12.1 注入违规 ①：空域并发超容量必须命中 C06。IFR 容量为 1。"""
    broken = variant(
        add=[
            make_sortie(
                "S000019", 3, "06:20", "missionC-1", "AC34", (("P03", "教员"), ("P05", "学员"))
            )
        ]
    )  # 与 S000007（06:00-06:35，IFR）重叠
    result = check_c06(broken, ctx)
    assert not result.passed
    assert "空域 IFR" in details(result)
    assert "并发 2 架 > 同时段容量 1" in details(result)


def test_c06_same_minute_landing_then_takeoff_is_not_concurrent(ctx: ValidationContext) -> None:
    """★ 同刻口径「先减后加」：06:35 着陆 + 06:35 起飞**不算并发**。

    这是与求解侧的半开区间 `[start, start+dur)` 对齐的关键；两边不一致会在
    容量=1 的空域上直接产出 FTS-3003（CRITICAL）。
    """
    first = make_sortie(
        "S000020", 6, "06:00", "missionC-1", "AC10", (("P01", "教员"), ("P06", "学员"))
    )  # IFR，06:00-06:35
    touching = make_sortie(
        "S000021", 6, "06:35", "missionC-2", "AC27", (("P02", "教员"), ("P05", "学员"))
    )  # IFR，前一架**着陆的同一分钟**起飞
    assert check_c06(make_plan([first, touching], validate=False), ctx).passed

    overlapping = touching.model_copy(update={"takeoff": time(6, 34), "landing": time(7, 30)})
    result = check_c06(make_plan([first, overlapping], validate=False), ctx)
    assert not result.passed  # 提前一分钟就真重叠了
    assert "在 06:34 并发 2 架 > 同时段容量 1" in details(result)


def test_c06_capacity_comes_from_the_airspace_table_not_a_constant(ctx: ValidationContext) -> None:
    """容量一律从 `airspaces` 表读：把 IFR 调成 2，同一份方案就该放行。"""

    def widen_ifr(rows: ContextRows) -> None:
        for a in rows.airspaces:
            if a.airspace_id == "IFR":
                a.capacity = 2

    overlapping = variant(
        add=[
            make_sortie(
                "S000019", 3, "06:20", "missionC-1", "AC34", (("P03", "教员"), ("P05", "学员"))
            )
        ]
    )
    assert not check_c06(overlapping, ctx).passed
    assert check_c06(overlapping, context_with(widen_ifr)).passed


def test_c06_detects_aircraft_type_mismatch(ctx: ValidationContext) -> None:
    broken = variant(replace={"S000001": {"aircraft_id": "AC84"}})  # A-1 只允许 JL-8
    result = check_c06(broken, ctx)
    assert "机型不适配 missionA-1" in details(result)
    assert "适配课目列表不含 missionA-1" in details(result)


def test_c06_detects_unknown_aircraft_and_airspace_mismatch(ctx: ValidationContext) -> None:
    broken = variant(
        replace={"S000001": {"aircraft_id": "AC99"}, "S000003": {"airspace_id": "SAA"}}
    )
    result = check_c06(broken, ctx)
    assert "机号 AC99 不在册" in details(result)
    assert "绑定空域为 RT1" in details(result)


# ─────────────────────────────────────────────────────────────────────
# C07 飞机排期冲突与周转时间
# ─────────────────────────────────────────────────────────────────────
def test_c07_detects_short_turnaround(ctx: ValidationContext) -> None:
    """JL-8 周转 30 分钟，S-06 从**着陆**算到**起飞**。"""
    broken = variant(
        add=[make_sortie("S000021", 0, "06:45", "missionA-2", "AC10", (("P07", "单飞"),))]
    )  # S000001 06:30 着陆 → 间隔 15 分钟
    result = check_c07(broken, ctx)
    assert not result.passed
    assert "间隔 15 分钟 < 周转要求 30 分钟" in details(result)


def test_c07_turnaround_is_per_aircraft_from_the_fleet_table(ctx: ValidationContext) -> None:
    """周转时间取 `aircraft.turnaround_minutes`（JL-9 = 40），不是写死的 30。"""
    sorties = [
        make_sortie("S000022", 6, "09:00", "missionC-1", "AC84", (("P04", "单飞"),)),
        make_sortie(
            "S000023", 6, "10:10", "missionC-1", "AC84", (("P01", "教员"), ("P05", "学员"))
        ),
    ]
    plan_jl9 = make_plan(sorties, validate=False)
    assert not check_c07(plan_jl9, ctx).passed  # 间隔 35 < 40
    assert "周转要求 40 分钟" in details(check_c07(plan_jl9, ctx))


def test_c07_detects_maintenance_overlap(ctx: ValidationContext) -> None:
    """AC73 在 2026-01-09 全天定检（v6 §1.2.3 的真实扰动）。"""
    broken = variant(
        add=[make_sortie("S000024", 4, "09:00", "missionA-1", "AC73", (("P05", "单飞"),))]
    )
    result = check_c07(broken, ctx)
    assert not result.passed
    assert "落入维护时段" in details(result)


def test_c07_touching_maintenance_boundary_is_allowed(ctx: ValidationContext) -> None:
    """维护 08:00 结束、架次 08:00 起飞 —— 规格没要求维护前后另留周转。"""

    def morning_maintenance(rows: ContextRows) -> None:
        rows.maintenance.clear()  # type: ignore[attr-defined]
        rows.maintenance.append(  # type: ignore[attr-defined]
            AircraftMaintenance(
                aircraft_id="AC10",
                snapshot_id=SNAPSHOT,
                start_ts=datetime(2026, 1, 11, 6, 0),
                end_ts=datetime(2026, 1, 11, 8, 0),
                kind="定检维护",
                all_day=False,
            )
        )

    touching = make_plan(
        [make_sortie("S000025", 6, "08:00", "missionA-1", "AC10", (("P05", "单飞"),))],
        validate=False,
    )
    assert check_c07(touching, context_with(morning_maintenance)).passed


# ─────────────────────────────────────────────────────────────────────
# C08 人员冲突与休息
# ─────────────────────────────────────────────────────────────────────
def test_c08_detects_short_gap_between_two_sorties(ctx: ValidationContext) -> None:
    broken = variant(
        add=[make_sortie("S000026", 0, "06:35", "missionA-2", "AC34", (("P05", "单飞"),))]
    )  # 罗磊 S000001 06:30 着陆 → 间隔 5 分钟
    result = check_c08(broken, ctx)
    assert not result.passed
    assert "间隔 5 分钟 < 10 分钟" in details(result)


def test_c08_detects_missing_rest_after_two_sorties(ctx: ValidationContext) -> None:
    """连飞 2 架次后需休息 30 分钟：第 3 架次只隔 15 分钟。"""
    sorties = [
        make_sortie("S000027", 6, "06:00", "missionA-1", "AC10", (("P05", "单飞"),)),
        make_sortie("S000028", 6, "06:45", "missionA-2", "AC27", (("P05", "单飞"),)),
        make_sortie("S000029", 6, "07:27", "missionA-1", "AC34", (("P05", "单飞"),)),
    ]
    result = check_c08(make_plan(sorties, validate=False), ctx)
    assert not result.passed
    assert "连飞 2 架次后需休息 30 分钟" in details(result)
    assert "第 3 架次" in details(result)


def test_c08_does_not_accumulate_across_days(ctx: ValidationContext) -> None:
    """S-07：**仅同日内累计**。跨日的两个架次不构成间隔违规。"""
    sorties = [
        make_sortie("S000030", 0, "17:00", "missionA-1", "AC10", (("P05", "单飞"),)),
        make_sortie("S000031", 1, "06:00", "missionA-2", "AC10", (("P05", "单飞"),)),
    ]
    assert check_c08(make_plan(sorties, validate=False), ctx).passed


# ─────────────────────────────────────────────────────────────────────
# C09 起降密度（D-2 的唯一守门人）
# ─────────────────────────────────────────────────────────────────────
def test_c09_detects_three_takeoffs_in_twenty_minutes_on_one_runway(
    ctx: ValidationContext,
) -> None:
    """★ v6 §12.1 注入违规 ②：同一跑道 20 分钟内 3 次起飞。"""
    sorties = [
        make_sortie(
            "S000032", 6, "06:00", "missionA-1", "AC10", (("P05", "单飞"),), runway_id="RWY-1"
        ),
        make_sortie(
            "S000033", 6, "06:08", "missionA-2", "AC27", (("P06", "单飞"),), runway_id="RWY-1"
        ),
        make_sortie(
            "S000034", 6, "06:16", "missionA-1", "AC34", (("P07", "单飞"),), runway_id="RWY-1"
        ),
    ]
    result = check_c09(make_plan(sorties, validate=False), ctx)
    assert not result.passed
    assert "跑道 RWY-1 在 [06:00, +20) 内起飞 3 次 > 2 次" in details(result)


def test_c09_two_takeoffs_in_twenty_minutes_per_runway_is_fine(ctx: ValidationContext) -> None:
    """20 分钟窗口**按跑道分组**：把第三个架次挪到 RWY-2 就不再超窗。"""
    sorties = [
        make_sortie(
            "S000032", 6, "06:00", "missionA-1", "AC10", (("P05", "单飞"),), runway_id="RWY-1"
        ),
        make_sortie(
            "S000033", 6, "06:08", "missionA-2", "AC27", (("P06", "单飞"),), runway_id="RWY-1"
        ),
        make_sortie(
            "S000034", 6, "06:16", "missionA-1", "AC34", (("P07", "单飞"),), runway_id="RWY-2"
        ),
    ]
    assert check_c09(make_plan(sorties, validate=False), ctx).passed


def test_c09_seven_minute_separation_is_airport_wide_not_per_runway(
    ctx: ValidationContext,
) -> None:
    """★★ v6 §12.1 注入违规 ③（**本窗口最容易写错的一条**）。

    两次起飞相隔 3 分钟、分属两条**不同**跑道 —— 若把 7 分钟间隔误实现成
    「按跑道分组」，这个用例会被漏判。`rules.pdf` 约束9 原文只对前半句（20 分钟
    窗口）限定了「同一跑道」，后半句没有（D-2，v6 §1.1.1）。
    """
    sorties = [
        make_sortie(
            "S000035", 6, "06:00", "missionA-1", "AC10", (("P05", "单飞"),), runway_id="RWY-1"
        ),
        make_sortie(
            "S000036", 6, "06:03", "missionA-2", "AC27", (("P06", "单飞"),), runway_id="RWY-2"
        ),
    ]
    result = check_c09(make_plan(sorties, validate=False), ctx)
    assert not result.passed
    assert len(result.violations) == 1
    only = result.violations[0]
    assert only.rule_id == "C09"
    assert "相邻起飞间隔 3 分钟 < 7 分钟（**全场口径**" in only.detail
    assert "S000035@RWY-1 06:00 → S000036@RWY-2 06:03" in only.detail


def test_c09_detects_jl9_on_runway_two(ctx: ValidationContext) -> None:
    """RWY-2 只服务 JL-8。**不是**「RWY-1=JL-8、RWY-2=JL-9」。"""
    solo = make_sortie(
        "S000037", 6, "09:00", "missionC-1", "AC84", (("P04", "单飞"),), runway_id="RWY-2"
    )
    result = check_c09(make_plan([solo], validate=False), ctx)
    assert not result.passed
    assert "AC84（JL-9）不得使用跑道 RWY-2" in details(result)


def test_c09_window_boundary_is_half_open(ctx: ValidationContext) -> None:
    """S-04：`[t, t+20)` —— 第三次起飞落在 t+20 整点上不算进窗口。"""
    sorties = [
        make_sortie(
            "S000038", 6, "06:00", "missionA-1", "AC10", (("P05", "单飞"),), runway_id="RWY-1"
        ),
        make_sortie(
            "S000039", 6, "06:10", "missionA-2", "AC27", (("P06", "单飞"),), runway_id="RWY-1"
        ),
        make_sortie(
            "S000040", 6, "06:20", "missionA-1", "AC34", (("P07", "单飞"),), runway_id="RWY-1"
        ),
    ]
    assert check_c09(make_plan(sorties, validate=False), ctx).passed


# ─────────────────────────────────────────────────────────────────────
# C10 / C11 / C12 上限
# ─────────────────────────────────────────────────────────────────────
def test_c10_detects_student_daily_minutes_over_limit(ctx: ValidationContext) -> None:
    """学员 240 分钟/日。"""
    sorties = [
        make_sortie(
            "S000041", 6, "06:00", "missionC-2", "AC10", (("P01", "教员"), ("P05", "学员"))
        ),
        make_sortie(
            "S000042", 6, "07:10", "missionC-2", "AC27", (("P02", "教员"), ("P05", "学员"))
        ),
        make_sortie(
            "S000043", 6, "08:20", "missionC-2", "AC34", (("P03", "教员"), ("P05", "学员"))
        ),
        make_sortie(
            "S000044", 6, "09:30", "missionC-2", "AC49", (("P01", "教员"), ("P05", "学员"))
        ),
        make_sortie(
            "S000045", 6, "10:40", "missionC-2", "AC61", (("P02", "教员"), ("P05", "学员"))
        ),
    ]
    result = check_c10(make_plan(sorties, validate=False), ctx)
    assert not result.passed
    assert "飞行时长合计 280 分钟 > 上限 240 分钟" in details(result)
    assert "身份 学员" in details(result)


def test_c10_instructor_limit_is_480(ctx: ValidationContext) -> None:
    sorties = [
        make_sortie(
            "S000046", 6, "06:00", "missionC-2", "AC10", (("P01", "教员"), ("P05", "学员"))
        ),
        make_sortie(
            "S000047", 6, "07:10", "missionC-2", "AC27", (("P01", "教员"), ("P06", "学员"))
        ),
        make_sortie(
            "S000048", 6, "08:20", "missionC-2", "AC34", (("P01", "教员"), ("P07", "学员"))
        ),
    ]
    result = check_c10(make_plan(sorties, validate=False), ctx)
    assert [v.subjects[0] for v in result.violations] == ["P05", "P06", "P07"] or result.passed
    assert not any(v.subjects[0] == "P01" for v in result.violations)  # 168 分钟 < 480


def test_c11_detects_weekly_sortie_cap(ctx: ValidationContext) -> None:
    """学员 10 架次/周。"""
    extra = [
        make_sortie(
            f"S0001{50 + i:02d}",
            i % 7,
            f"{9 + i // 7:02d}:00",
            "missionA-1",
            "AC61",
            (("P05", "单飞"),),
        )
        for i in range(9)
    ]
    result = check_c11(variant(add=extra), ctx)
    assert not result.passed
    assert "本周 12 架次 > 上限 10 架次" in details(result)


def test_c12_detects_person_and_aircraft_daily_caps(ctx: ValidationContext) -> None:
    sorties = [
        make_sortie("S000160", 6, "06:00", "missionA-1", "AC10", (("P05", "单飞"),)),
        make_sortie("S000161", 6, "07:00", "missionA-2", "AC10", (("P05", "单飞"),)),
        make_sortie("S000162", 6, "08:00", "missionA-1", "AC10", (("P05", "单飞"),)),
        make_sortie("S000163", 6, "09:00", "missionA-2", "AC10", (("P05", "单飞"),)),
        make_sortie("S000164", 6, "10:00", "missionA-1", "AC10", (("P06", "单飞"),)),
        make_sortie("S000165", 6, "11:00", "missionA-2", "AC10", (("P06", "单飞"),)),
        make_sortie("S000166", 6, "12:00", "missionA-1", "AC10", (("P06", "单飞"),)),
    ]
    result = check_c12(make_plan(sorties, validate=False), ctx)
    assert not result.passed
    assert "安排 4 架次 > 单人单日上限 3" in details(result)
    assert "AC10 2026-01-11 安排 7 架次 > 单机单日上限 6" in details(result)


# ─────────────────────────────────────────────────────────────────────
# C13 任务完成度
# ─────────────────────────────────────────────────────────────────────
def test_c13_detects_blocked_mission_being_scheduled(ctx: ValidationContext) -> None:
    """先修未满足的课目在方案中出现次数必须 = 0（何超缺 missionA-2）。"""
    broken = variant(
        add=[
            make_sortie(
                "S000170", 6, "09:00", "missionC-1", "AC61", (("P01", "教员"), ("P08", "学员"))
            )
        ]
    )
    result = check_c13(broken, ctx)
    assert not result.passed
    assert "的 missionC-1 先修未满足" in details(result)
    assert ("P08", "missionC-1") in BLOCKED_EXPECTED


def test_c13_detects_missing_weekly_frequency(ctx: ValidationContext) -> None:
    broken = variant(drop={"S000009"})  # 罗磊 missionC-2
    result = check_c13(broken, ctx)
    assert not result.passed
    assert "罗磊(P05) 的 missionC-2" in details(result)
    assert "本周一次都未安排" in details(result)


def test_c13_detects_sliding_window_gap_for_three_day_mission(ctx: ValidationContext) -> None:
    """A 类 freq_days=3：何超两次 A-2 挪到第 5、6 天就漏掉前面的窗口。"""
    broken = variant(replace={"S000005": {"date": date(2026, 1, 11), "weekday": "周日"}})
    result = check_c13(broken, ctx)
    assert not result.passed
    assert "在窗口 [第0天, 第2天] 内没有安排" in details(result)


def test_c13_missing_anchor_starts_from_monday_and_is_not_debt(ctx: ValidationContext) -> None:
    """S-12：`last_done_date` 为 NULL → `deadline = F − 1`，**不计欠账**。

    何超 A-2 首次执行在第 2 天，恰好等于 `3 − 1`；挪到第 3 天即越线。
    绝不能按 `gap=999` 处理（那会让 deadline 变成 0，基准周假性不可行）。
    """
    assert check_c13(compliant_plan(), ctx).passed
    late = variant(
        replace={
            "S000005": {"date": date(2026, 1, 8), "weekday": "周四"},
            "S000011": {"date": date(2026, 1, 11), "weekday": "周日"},
        }
    )
    result = check_c13(late, ctx)
    assert "首次执行在第 3 天，晚于截止日第 2 天" in details(result)


def test_c13_cross_week_anchor_uses_the_d4_formula(ctx: ValidationContext) -> None:
    """D-4 通式 `deadline = max(0, F − gap)`：锚点 01-01、F=7 → 截止日为第 3 天。"""

    def anchor_c2(rows: ContextRows) -> None:
        for p in rows.progress:
            if p.person_id == "P05" and p.mission_id == "missionC-2":
                p.last_done_date = date(2026, 1, 1)

    anchored = context_with(anchor_c2)
    result = check_c13(compliant_plan(), anchored)  # S000009 在第 4 天
    assert not result.passed
    assert "首次执行在第 4 天，晚于截止日第 3 天" in details(result)
    on_time = variant(replace={"S000009": {"date": date(2026, 1, 8), "weekday": "周四"}})
    assert check_c13(on_time, anchored).passed


def test_c13_completed_missions_are_exempt(ctx: ValidationContext) -> None:
    """S-03：已完成课目不受约束13（罗磊的 B-1/B-2/C-1 一次都没排，仍应通过）。"""
    result = check_c13(compliant_plan(), ctx)
    assert result.passed
    assert result.checked_items > 0


def test_c13_s11_recurrent_window_does_not_bind_this_week(ctx: ValidationContext) -> None:
    """刘斌的复训窗口 [01-08, 01-14] 跨出 W02 → 本周不强制（v6 §1.2.4）。"""
    assert check_c13(compliant_plan(), ctx).passed


def test_c13_s11_recurrent_window_binds_when_it_falls_inside_the_week(
    ctx: ValidationContext,
) -> None:
    """把复训起算日提前到 01-01，7 天窗口就整段落进本周 → 必须排 1 次。"""

    def earlier_recurrent(rows: ContextRows) -> None:
        for p in rows.progress:
            if p.person_id == "P04" and p.mission_id == "missionC-1":
                p.recurrent_since = date(2026, 1, 1)

    earlier = context_with(earlier_recurrent)
    assert not check_c13(compliant_plan(), earlier).passed
    # 复训窗口 [01-01, 01-07] 的右端落在本周第 2 天 → 首次执行必须不晚于第 2 天
    with_recurrent = variant(
        add=[
            make_sortie(
                "S000171",
                2,
                "07:30",
                "missionC-1",
                "AC61",
                (("P04", "复训"),),
                is_recurrent=True,
            )
        ]
    )
    assert check_c13(with_recurrent, earlier).passed
    too_late = variant(
        add=[
            make_sortie(
                "S000172",
                3,
                "07:20",
                "missionC-1",
                "AC61",
                (("P04", "复训"),),
                is_recurrent=True,
            )
        ]
    )
    assert "首次执行在第 3 天，晚于截止日第 2 天" in details(check_c13(too_late, earlier))


def test_c13_shortfall_is_soft_when_tier1_and_disclosed(ctx: ValidationContext) -> None:
    broken = variant(
        drop={"S000009"},
        relaxation_tier=1,
        debts=[debt("P05", "missionC-2", required=1, scheduled=0, relaxed_by="TIER1")],
    )
    result = check_c13(broken, ctx)
    assert result.passed
    assert result.violations and all(v.severity == "SOFT" for v in result.violations)


# ─────────────────────────────────────────────────────────────────────
# C14 任务唯一性
# ─────────────────────────────────────────────────────────────────────
def test_c14_detects_exact_duplicate_sortie(ctx: ValidationContext) -> None:
    dup = compliant_sorties()[0].model_copy(update={"sortie_id": "S000180"})
    result = check_c14(variant(add=[dup]), ctx)
    assert not result.passed
    assert "是完全重复的架次记录" in details(result)


def test_c14_detects_req_max_overflow(ctx: ValidationContext) -> None:
    """`req_max = ceil(7 / freq_days)`：F-1 的 freq_days=7 → req_max=1。"""
    extra = make_sortie(
        "S000181", 6, "10:00", "missionF-1", "AC61", (("P02", "教员"), ("P05", "学员"))
    )
    result = check_c14(variant(add=[extra]), ctx)
    assert not result.passed
    assert "共 2 次 > req_max 1 = ceil(7 / freq_days=7)" in details(result)


def test_c14_a_class_allows_three_per_week(ctx: ValidationContext) -> None:
    """A 类 freq_days=3 → `req_max = ceil(7/3) = 3`：何超第 3 次 A-2 仍合规。"""
    third = make_sortie("S000182", 0, "10:00", "missionA-2", "AC61", (("P08", "单飞"),))
    assert check_c14(variant(add=[third]), ctx).passed
    fourth = make_sortie("S000183", 1, "10:00", "missionA-2", "AC61", (("P08", "单飞"),))
    result = check_c14(variant(add=[third, fourth]), ctx)
    assert not result.passed
    assert "共 4 次 > req_max 3" in details(result)


def test_c14_instructor_seat_does_not_count_toward_req_max(ctx: ValidationContext) -> None:
    """教员一周内带同一门课目多次是正常的：约束14 只约束受训人。"""
    result = check_c14(compliant_plan(), ctx)
    assert result.passed  # 孙军带了 3 次不同学员的课目，其中 C-1 一次、B-2 一次…
    per_instructor = [s for s in compliant_sorties() if any(c.person_id == "P01" for c in s.crew)]
    assert len(per_instructor) >= 3


# ─────────────────────────────────────────────────────────────────────
# image 4 的已知违规样例（v6 §1.2.2）
# ─────────────────────────────────────────────────────────────────────
def test_image4_fixture_reproduces_all_four_documented_violations(ctx: ValidationContext) -> None:
    """v6 §1.2.2 点名的四类违规必须被逐条拓出。"""
    report = run_all_checks(image4_plan(), ctx)
    assert not report.all_passed
    violated = rules_violated(report)
    assert {"C03", "C04", "C06", "C07", "C09"} <= violated

    by_rule = {r.rule_id: r for r in report.results}
    # ① AC84（JL-9）飞限 JL-8 的 missionA-1 → C06
    assert any("AC84（JL-9）机型不适配 missionA-1" in v.detail for v in by_rule["C06"].violations)
    # ② AC84 06:29 着陆后 06:39 起飞，10 分钟 < JL-9 周转 40 分钟 → C07
    assert any("间隔 10 分钟 < 周转要求 40 分钟" in v.detail for v in by_rule["C07"].violations)
    # ③ 周二两架 06:00 同刻起飞 → C09（全场 7 分钟口径）
    assert any(
        "2026-01-06 相邻起飞间隔 0 分钟 < 7 分钟" in v.detail for v in by_rule["C09"].violations
    )
    # ④ 刘斌（成熟飞行员）被标为「教员」带飞 → C03 + C04
    assert any("刘斌(P04) 身份为「成熟飞行员」" in v.detail for v in by_rule["C03"].violations)
    assert any("占据了 S000103 的教员岗" in v.detail for v in by_rule["C04"].violations)
    assert any("占据了 S000107 的教员岗" in v.detail for v in by_rule["C04"].violations)


def test_image4_fixture_only_references_existing_entities(ctx: ValidationContext) -> None:
    """裁剪后的 fixture 里不许再出现 missionD-2 / Large Area C 这类不存在的实体。"""
    for s in image4_plan().sorties:
        assert s.mission_id in ctx.missions
        assert s.aircraft_id in ctx.aircraft
        assert s.airspace_id in ctx.airspaces
        assert s.airspace_id == ctx.missions[s.mission_id].airspace_id


# ─────────────────────────────────────────────────────────────────────
# 汇总层
# ─────────────────────────────────────────────────────────────────────
def test_run_all_checks_does_not_short_circuit(ctx: ValidationContext) -> None:
    """第一条就失败也要把 14 条跑完 —— 排班员需要一次看到全部问题。"""
    report = run_all_checks(image4_plan(), ctx)
    assert len(report.results) == 14
    assert report.missing_rules() == []


def test_run_all_checks_is_deterministic(plan: SchedulePlan, ctx: ValidationContext) -> None:
    """同输入 → 同输出（铁律 9）。耗时字段除外。"""
    a = run_all_checks(image4_plan(), ctx)
    b = run_all_checks(image4_plan(), ctx)

    def fingerprint(report: ValidationReport) -> list[tuple[str, int, tuple[str, ...]]]:
        return (
            [(r.rule_id, r.checked_items, tuple(v.detail for v in r.violations)) for r in r_list]
            if (r_list := report.results)
            else []
        )

    assert fingerprint(a) == fingerprint(b)


def test_empty_plan_is_not_silently_compliant(ctx: ValidationContext) -> None:
    """空方案不是「全过」：约束3 与约束13 都会拦下它。"""
    report = run_all_checks(make_plan([], validate=False), ctx)
    assert not report.all_passed
    assert {"C03", "C13"} <= rules_violated(report)


def test_context_rejects_non_monday_week_start() -> None:
    rows = baseline_rows()
    with pytest.raises(Exception, match="week_start 必须是周一"):
        context_from_rows(rows, week_start=date(2026, 1, 6), snapshot_id=SNAPSHOT)


def test_semantics_switches_are_the_frozen_thirteen(ctx: ValidationContext) -> None:
    """校验器读的是同一份 `semantics.yaml`（S-01~S-13 已封闭）。"""
    assert ctx.semantics.version == get_semantics().version
    assert ctx.semantics.s04_half_open and ctx.semantics.s05_dual_runway
    assert ctx.semantics.s11_enabled and "成熟飞行员" in ctx.semantics.s11_identities
    assert ctx.semantics.s12_from_week_monday and not ctx.semantics.s12_count_as_debt
    assert dataclasses.is_dataclass(ctx)
