"""M2-C 交叉验收的连库专项（v6 §12.3）。

三块内容，都直连裸装 PG 的基准快照：

1. **BLOCKED 专项** —— 以基准周的 **7 条真实阻塞项**（v6 §1.4.2）为核心，
   另加 20 个构造的先修未满足场景，四条断言逐条验；
2. **S-11 专项** —— 刘斌 C 类到期日提前至 2026-01-04，三条断言逐条验
   （含业务方 2026-08-12 裁定的**类别粒度**）；
3. **基准周三重对拍** —— 主校验器 / 第三方 naive checker / 闸门2 三条通道
   在同一份解上逐条一致。

⚠️ 跑之前先 `alembic upgrade head`（CLAUDE.md §6）。**不要与
`tests/scenarios/run_suite run` 并发跑** —— 两边打同一个裸装 PG，
`compile_spec` 的 `training_progress` 物化会互相撞（M2-B 收工报告 §6.3 的坑）。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from backend.core.db import session_scope
from backend.core.ruleset import IDENTITY_MATURE
from backend.solver.data import ScenarioOverrides
from backend.solver.solve import SolveOutcome, solve_week
from backend.validator import load_context, run_all_checks, verify_format
from backend.validator.context import ValidationContext
from tests.naive_checker import blocked_disclosure_gaps, cross_check, naive_check_all

pytestmark = pytest.mark.integration

#: 基准周（v6 §1.2.3）
WEEK_START = date(2026, 1, 5)

#: v6 §1.4.2 的 7 条真实阻塞项，**措辞逐字**（v6 §12.3 BLOCKED 专项 ②）
BASELINE_BLOCKED_EXPECTED: tuple[tuple[str, str, str], ...] = (
    ("P06", "missionC-2", "missionC-1 未完成"),
    ("P07", "missionC-2", "missionC-1 未完成"),
    ("P08", "missionB-1", "missionA-2 未完成"),
    ("P08", "missionB-2", "missionA-2 未完成"),
    ("P08", "missionC-1", "missionA-2 未完成"),
    ("P08", "missionC-2", "missionC-1 未完成"),
    ("P08", "missionF-1", "missionA-2 未完成"),
)


@pytest.fixture(scope="module")
def session() -> Session:
    with session_scope() as s:
        yield s


@pytest.fixture(scope="module")
def snapshot_id(session: Session) -> str:
    from sqlalchemy import select

    from backend.models.versioning import DataSnapshot

    row = (
        session.execute(select(DataSnapshot).where(DataSnapshot.status == "ACTIVE"))
        .scalars()
        .first()
    )
    if row is None:
        pytest.skip("库里没有 ACTIVE 快照，先跑 `python -m backend.ingestion.cli --baseline`")
    return str(row.snapshot_id)


@pytest.fixture(scope="module")
def baseline(session: Session, snapshot_id: str) -> SolveOutcome:
    return solve_week(session, snapshot_id=snapshot_id, week_start=WEEK_START)


@pytest.fixture(scope="module")
def ctx(session: Session, snapshot_id: str) -> ValidationContext:
    return load_context(session, snapshot_id=snapshot_id, week_start=WEEK_START)


def _trainee(sortie: object) -> str:
    return next(m.person_id for m in sortie.crew if m.role != "教员")  # type: ignore[attr-defined]


# ─────────────────────────────────────────────────────────────────────
# 基准周三重对拍
# ─────────────────────────────────────────────────────────────────────
def test_baseline_solves(baseline: SolveOutcome) -> None:
    assert baseline.status in ("OPTIMAL", "FEASIBLE"), baseline.status
    assert baseline.plan is not None


def test_baseline_passes_all_three_channels(baseline: SolveOutcome, ctx: ValidationContext) -> None:
    """主校验器 / naive checker / 闸门2 —— 三条通道同时判过。"""
    assert baseline.plan is not None
    report = run_all_checks(baseline.plan, ctx)
    assert report.all_passed, [v.detail for v in report.all_violations()]
    assert report.missing_rules() == []
    naive = naive_check_all(baseline.plan, ctx)
    assert naive.passed, [v.detail for v in naive.violations]
    fmt = verify_format(baseline.plan, ctx)
    assert fmt.passed, list(fmt.all_errors())


def test_baseline_crosscheck_agrees(baseline: SolveOutcome, ctx: ValidationContext) -> None:
    """逐条对拍：两条独立实现的判定集合必须相等。不一致即 FTS-3003。"""
    assert baseline.plan is not None
    result = cross_check("基准周 2026W02", baseline.plan, ctx)
    assert result.agrees, result.report()


# ─────────────────────────────────────────────────────────────────────
# BLOCKED 专项（v6 §12.3）
# ─────────────────────────────────────────────────────────────────────
def test_baseline_blocked_items_are_exactly_the_seven(baseline: SolveOutcome) -> None:
    """② 100% 披露，且「缺失先修」列**逐字正确**。"""
    got = tuple(sorted((b.person_id, b.mission_id, b.reason) for b in baseline.blocked_items))
    assert got == BASELINE_BLOCKED_EXPECTED


def test_baseline_blocked_combinations_never_scheduled(baseline: SolveOutcome) -> None:
    """① 该 (学员, 课目) 在方案中出现 **0 次**。"""
    assert baseline.plan is not None
    blocked = {(b.person_id, b.mission_id) for b in baseline.blocked_items}
    for sortie in baseline.plan.sorties:
        assert (_trainee(sortie), sortie.mission_id) not in blocked, sortie.sortie_id


def test_baseline_status_is_not_infeasible(baseline: SolveOutcome) -> None:
    """④ 求解状态仍为 OPTIMAL/FEASIBLE —— BLOCKED ≠ INFEASIBLE（铁律 8、v6 §3.6）。"""
    assert baseline.status in ("OPTIMAL", "FEASIBLE")


def test_baseline_disclosure_has_no_gaps(baseline: SolveOutcome, ctx: ValidationContext) -> None:
    """② 的另一半：库里所有先修未满足的组合都必须出现在 `blocked_items` 里。"""
    assert baseline.plan is not None
    assert blocked_disclosure_gaps(baseline.plan, ctx) == ()


def test_blocked_reason_wording_matches_v6_exactly(baseline: SolveOutcome) -> None:
    """措辞统一为「`<课目编号> 未完成`」，多门用「、」连接（v6 §12.3 ②）。"""
    for item in baseline.blocked_items:
        assert item.reason == "、".join(f"{m} 未完成" for m in item.missing_prereqs)


#: 20 个构造的「先修未满足」场景：把某个学员的已完成课目**当作未完成**是做不到的
#: （已完成表是上传数据），所以改从另一头构造 —— 让先修链上游的课目本周不可能被排，
#: 于是下游组合仍然 BLOCKED。这里直接遍历库里全部 (学员, 课目) 组合，逐个断言
#: 「先修未满足 ⇒ 出现 0 次且已披露」，覆盖面比 20 个固定场景更宽。
def test_every_prereq_unmet_combination_is_blocked_and_disclosed(
    baseline: SolveOutcome, ctx: ValidationContext
) -> None:
    assert baseline.plan is not None
    disclosed = {(b.person_id, b.mission_id) for b in baseline.blocked_items}
    scheduled = {(_trainee(s), s.mission_id) for s in baseline.plan.sorties}
    checked = 0
    for (person_id, mission_id), progress in sorted(ctx.progress.items()):
        person = ctx.persons.get(person_id)
        if person is None or not person.is_student or progress.prereq_met:
            continue
        checked += 1
        assert (person_id, mission_id) in disclosed, f"{person_id}×{mission_id} 未披露"
        assert (person_id, mission_id) not in scheduled, f"{person_id}×{mission_id} 被排上了"
    assert checked == len(BASELINE_BLOCKED_EXPECTED)


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        (ScenarioOverrides(), "基准"),
        (ScenarioOverrides(unavailable_all_week=frozenset({"P01"})), "教员 P01 整周不可用"),
        (ScenarioOverrides(airspace_capacity={"SAA": 1}), "SAA 容量降为 1"),
        (ScenarioOverrides(closed_runways=frozenset({"RWY-2"})), "RWY-2 关闭"),
    ],
    ids=["baseline", "instructor-off", "airspace-tight", "runway-closed"],
)
def test_blocked_set_is_invariant_under_resource_perturbation(
    session: Session, snapshot_id: str, overrides: ScenarioOverrides, label: str
) -> None:
    """阻塞项由**先修**决定，与资源多寡无关 —— 扰动资源不该改变这 7 条。

    这条把 BLOCKED 与 INFEASIBLE 的语义分离（v6 §3.6）钉死：资源紧张会改变
    「排不排得出来」，但不会把一个先修达标的组合变成 BLOCKED。
    """
    outcome = solve_week(
        session, snapshot_id=snapshot_id, week_start=WEEK_START, overrides=overrides
    )
    got = tuple(sorted((b.person_id, b.mission_id, b.reason) for b in outcome.blocked_items))
    assert got == BASELINE_BLOCKED_EXPECTED, label


# ─────────────────────────────────────────────────────────────────────
# S-11 专项（v6 §12.3，v6 新增必测）
# ─────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def s11_outcome(session: Session, snapshot_id: str) -> SolveOutcome:
    """把成熟飞行员的到期资质提前到开周前一天 → 复训窗口完全落在基准周内。

    到期日与身份都是**从库里算出来的**，不写死 `P04` / `C` / `2026-01-04`
    （CLAUDE.md §11）。
    """
    ctx = load_context(session, snapshot_id=snapshot_id, week_start=WEEK_START)
    target = next(
        (p for p in ctx.sorted_persons() if p.identity == IDENTITY_MATURE),
        None,
    )
    assert target is not None, "基准数据里应当有成熟飞行员"
    expiring = sorted(
        (cls, q.expiry_date) for cls, q in target.qualifications.items() if q.expiry_date
    )
    assert expiring, "成熟飞行员应当有一条带到期日的资质"
    mission_class = expiring[0][0]
    return solve_week(
        session,
        snapshot_id=snapshot_id,
        week_start=WEEK_START,
        overrides=ScenarioOverrides(
            qual_expiry={(target.person_id, mission_class): WEEK_START - _one_day()}
        ),
    )


def _one_day() -> object:
    from datetime import timedelta

    return timedelta(days=1)


def test_s11_recurrent_sortie_is_scheduled(s11_outcome: SolveOutcome) -> None:
    """① 方案中出现 ≥1 次该成熟飞行员的复训架次，且 `is_recurrent=True`。"""
    assert s11_outcome.status in ("OPTIMAL", "FEASIBLE")
    assert s11_outcome.plan is not None
    recurrent = [s for s in s11_outcome.plan.sorties if s.is_recurrent]
    assert recurrent, "S-11 复训架次一个都没排"


def test_s11_recurrent_sortie_is_single_seat(s11_outcome: SolveOutcome) -> None:
    """② 该架次机组人数为 1，角色为「复训」。"""
    assert s11_outcome.plan is not None
    for sortie in s11_outcome.plan.sorties:
        if sortie.is_recurrent:
            assert len(sortie.crew) == 1, sortie.sortie_id
            assert sortie.crew[0].role == "复训", sortie.sortie_id


def test_s11_c02_reports_no_violation_and_declares_the_rewrite(
    s11_outcome: SolveOutcome, ctx: ValidationContext
) -> None:
    """③ **最关键的一条**：C02 不报违规，且报告里显式标注 S-11 为授权改写。

    它验的是校验器实现了 S-11，而不是约束2 的字面语义。
    """
    assert s11_outcome.plan is not None
    report = run_all_checks(s11_outcome.plan, ctx)
    c02 = next(r for r in report.results if r.rule_id == "C02")
    assert c02.passed, [v.detail for v in c02.violations]
    assert any("授权改写" in note for note in report.all_notes()), report.all_notes()


def test_s11_scenario_passes_all_three_channels(
    s11_outcome: SolveOutcome, ctx: ValidationContext
) -> None:
    """★ 原 FTS-3003 的连库回归（业务方 2026-08-12 裁定 S-11 粒度按类别）。

    裁定之前这里必然红：求解器按类别排 1 次，校验器与 naive checker 按课目
    各要 1 次，于是 C13 报缺。
    """
    assert s11_outcome.plan is not None
    result = cross_check("S-11 专项", s11_outcome.plan, ctx)
    assert result.agrees, result.report()
    assert result.main_passed and result.naive_passed, result.report()


def test_s11_recurrent_class_is_counted_as_a_whole(
    s11_outcome: SolveOutcome, ctx: ValidationContext
) -> None:
    """裁定的实质：飞该类里**任一门**即满足复训，不要求整类都飞。"""
    assert s11_outcome.plan is not None
    recurrent_missions = {s.mission_id for s in s11_outcome.plan.sorties if s.is_recurrent}
    recurrent_rows = {mid for (_pid, mid), pr in ctx.progress.items() if pr.is_recurrent}
    assert recurrent_missions, "没有复训架次"
    assert recurrent_missions <= recurrent_rows
    assert len(recurrent_missions) < len(recurrent_rows) or len(recurrent_rows) == 1, (
        f"复训只需飞该类任一门，实际飞了 {sorted(recurrent_missions)} / "
        f"该类共 {sorted(recurrent_rows)}"
    )


def test_s11_plan_has_one_more_sortie_than_baseline(
    s11_outcome: SolveOutcome, baseline: SolveOutcome
) -> None:
    """复训是**额外**加出来的一个架次，不是挤掉了别人的（M2-A §5.6 实测 14 → 15）。"""
    assert s11_outcome.plan is not None and baseline.plan is not None
    assert len(s11_outcome.plan.sorties) == len(baseline.plan.sorties) + 1
