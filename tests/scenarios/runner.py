"""200 场景的执行器：求解 → 三重校验 → 冲突集度量（v6 §12.3）。

每个场景跑完记录一条 :class:`ScenarioResult`，度量口径全部写在字段文档里，
汇总口径见 :func:`summarize`。

## 三重独立验证在这里合流（v6 §12.3 度量方式）

1. `backend/validator/checks.py` 的 14 条 —— 主校验器
2. `tests/naive_checker.py` 的 14 条 —— 第三方 pandas 暴力实现
3. 人工抽检 —— 由 `reports/M2_交叉验收报告.md` 的抽样清单承载（每类 5 个）

前两条在这里逐条对拍，**任何一条不一致即视为该轮测试失败**（v6 §12.3 原文）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.entities import (
    Aircraft,
    Airspace,
    Mission,
    Person,
    PersonAircraftType,
    PersonQualification,
    Runway,
    RunwayAircraftType,
)
from backend.nodes.compile_spec import compile_spec
from backend.schemas.plan import SchedulePlan
from backend.solver.diagnose import ProbeBudget, diagnose
from backend.solver.reschedule import Disruption, local_reschedule
from backend.solver.solve import SolveOutcome, solve
from backend.validator import load_context, run_all_checks, verify_format
from backend.validator.context import ValidationContext
from tests.naive_checker import blocked_disclosure_gaps, naive_check_all
from tests.scenarios.catalog import Entities, ScenarioCase

SOLVED_STATUSES: tuple[str, ...] = ("OPTIMAL", "FEASIBLE")


# ─────────────────────────────────────────────────────────────────────
# 实体加载（从快照读，不写死任何编号）
# ─────────────────────────────────────────────────────────────────────
def load_entities(session: Session, *, snapshot_id: str, week_start: date) -> Entities:
    def rows(model: type) -> list[object]:
        return list(
            session.execute(select(model).where(model.snapshot_id == snapshot_id)).scalars()
        )  # type: ignore[attr-defined]

    persons = tuple(
        sorted((p.person_id, p.name, p.identity) for p in rows(Person))  # type: ignore[attr-defined]
    )
    aircraft = tuple(sorted((a.aircraft_id, a.aircraft_type) for a in rows(Aircraft)))  # type: ignore[attr-defined]
    airspaces = tuple(sorted((s.airspace_id, s.capacity) for s in rows(Airspace)))  # type: ignore[attr-defined]
    runway_types: dict[str, set[str]] = {}
    for row in rows(RunwayAircraftType):
        runway_types.setdefault(row.runway_id, set()).add(row.aircraft_type)  # type: ignore[attr-defined]
    runways = tuple(
        sorted(
            (r.runway_id, tuple(sorted(runway_types.get(r.runway_id, set()))))  # type: ignore[attr-defined]
            for r in rows(Runway)
        )
    )
    missions = tuple(
        sorted((m.mission_id, m.mission_class, m.freq_days) for m in rows(Mission))  # type: ignore[attr-defined]
    )
    quals = tuple(
        sorted((q.person_id, q.mission_class) for q in rows(PersonQualification))  # type: ignore[attr-defined]
    )
    identity_of = {pid: identity for pid, _n, identity in persons}
    student_types = tuple(
        sorted(
            {
                r.aircraft_type  # type: ignore[attr-defined]
                for r in rows(PersonAircraftType)
                if identity_of.get(r.person_id) == "学员"  # type: ignore[attr-defined]
            }
        )
    )
    return Entities(
        snapshot_id=snapshot_id,
        week_start=week_start,
        persons=persons,
        aircraft=aircraft,
        airspaces=airspaces,
        runways=runways,
        missions=missions,
        qualifications=quals,
        student_types=student_types,
    )


def c_class_airspaces(session: Session, *, snapshot_id: str, ents: Entities) -> tuple[str, ...]:
    """I3 要关掉的空域：**学员可及、非「每周必飞」类、且承载课目最多的那个空域**。

    基准数据下解出来就是 `IFR`（承载 C-1 与 C-2）—— 但这是**算出来的**，不是把
    `IFR` 写死（CLAUDE.md §11）。
    """
    student_classes = {cls for pid, cls in ents.qualifications if ents.identity_of(pid) == "学员"}
    by_airspace: dict[str, int] = {}
    for mission in session.execute(
        select(Mission).where(Mission.snapshot_id == snapshot_id)
    ).scalars():
        if mission.mission_class not in student_classes or mission.weekly_required:
            continue
        by_airspace[mission.airspace_id] = by_airspace.get(mission.airspace_id, 0) + 1
    if not by_airspace:
        return ()
    best = max(sorted(by_airspace), key=lambda aid: (by_airspace[aid], aid))
    return (best,)


# ─────────────────────────────────────────────────────────────────────
# 单场景结果
# ─────────────────────────────────────────────────────────────────────
@dataclass
class ScenarioResult:
    scenario_id: str
    category: str
    family: str
    title: str
    expected_status: str
    status: str
    wall_time_s: float
    num_candidates: int = 0
    num_sorties: int = 0
    num_blocked: int = 0
    #: 主校验器：14 条是否全过 / HARD 违规的规则集合
    main_passed: bool | None = None
    main_rules: tuple[str, ...] = ()
    #: 第三方 naive checker
    naive_passed: bool | None = None
    naive_rules: tuple[str, ...] = ()
    #: 闸门2 格式校验
    format_passed: bool | None = None
    format_errors: tuple[str, ...] = ()
    #: 阻塞项披露缺口（应恒为空）
    disclosure_gaps: tuple[str, ...] = ()
    #: 诊断（仅 INFEASIBLE）
    conflict_rules: tuple[str, ...] = ()
    sat_core_ids: tuple[str, ...] = ()
    structural_ids: tuple[str, ...] = ()
    escalate: bool | None = None
    useful_proposals: int = 0
    verified_proposals: int = 0
    #: 局部重排
    frozen_count: int | None = None
    error: str | None = None

    @property
    def agrees(self) -> bool:
        """主校验器与 naive checker 判定一致（未求解的场景视为一致）。"""
        if self.main_passed is None and self.naive_passed is None:
            return True
        return (self.main_passed == self.naive_passed) and (
            set(self.main_rules) == set(self.naive_rules)
        )

    @property
    def status_ok(self) -> bool:
        if self.expected_status == "SOLVED":
            return self.status in SOLVED_STATUSES
        if self.expected_status == "INFEASIBLE":
            return self.status == "INFEASIBLE"
        return self.status in (*SOLVED_STATUSES, "INFEASIBLE")

    def to_json(self) -> dict[str, object]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# 执行
# ─────────────────────────────────────────────────────────────────────
def _validate(plan: SchedulePlan, ctx: ValidationContext, result: ScenarioResult) -> None:
    report = run_all_checks(plan, ctx)
    hard = [v for v in report.all_violations() if v.severity == "HARD"]
    result.main_passed = bool(report.all_passed) and not hard
    result.main_rules = tuple(sorted({v.rule_id for v in hard}))
    naive = naive_check_all(plan, ctx)
    result.naive_passed = naive.passed
    result.naive_rules = tuple(sorted(naive.violated_rules()))
    fmt = verify_format(plan, ctx)
    result.format_passed = fmt.passed
    result.format_errors = tuple(fmt.all_errors())
    result.disclosure_gaps = blocked_disclosure_gaps(plan, ctx)
    assert report.missing_rules() == [], "14 条没跑全"


def run_case(
    session: Session,
    case: ScenarioCase,
    ents: Entities,
    *,
    baseline_plan: SchedulePlan | None = None,
    diagnose_infeasible: bool = True,
) -> ScenarioResult:
    """跑一个场景：求解 → 三重校验 →（不可行则）诊断。"""
    started = time.perf_counter()
    result = ScenarioResult(
        scenario_id=case.scenario_id,
        category=case.category,
        family=case.family,
        title=case.title,
        expected_status=case.expected_status,
        status="ERROR",
        wall_time_s=0.0,
    )
    try:
        bundle = compile_spec(
            session,
            snapshot_id=ents.snapshot_id,
            week_start=ents.week_start,
            overrides=case.overrides.to_overrides(),
            time_limit_s=case.time_limit_s,
        )
        if case.reschedule is not None:
            if baseline_plan is None:
                raise RuntimeError("局部重排场景需要一份已批准的基准计划")
            disruption = Disruption(
                persons=frozenset(case.reschedule.get("persons", ())),  # type: ignore[arg-type]
                aircraft=frozenset(case.reschedule.get("aircraft", ())),  # type: ignore[arg-type]
                airspaces=frozenset(case.reschedule.get("airspaces", ())),  # type: ignore[arg-type]
                runways=frozenset(case.reschedule.get("runways", ())),  # type: ignore[arg-type]
                days=frozenset(case.reschedule.get("days", ())),  # type: ignore[arg-type]
                reason=str(case.reschedule.get("reason", "")),
            )
            outcome, decision = local_reschedule(
                bundle,
                baseline_plan,
                disruption,
                policy=str(case.reschedule.get("policy", "BALANCED")),  # type: ignore[arg-type]
            )
            result.frozen_count = len(decision.frozen_ids)
        else:
            outcome = solve(bundle)

        result.status = outcome.status
        result.num_candidates = outcome.stats.num_candidates
        result.num_blocked = len(outcome.blocked_items)
        if outcome.plan is not None:
            result.num_sorties = len(outcome.plan.sorties)
            ctx = load_context(session, snapshot_id=ents.snapshot_id, week_start=ents.week_start)
            _validate(outcome.plan, ctx, result)
        elif outcome.status == "INFEASIBLE" and diagnose_infeasible:
            _diagnose(bundle, outcome, case, result)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    result.wall_time_s = round(time.perf_counter() - started, 2)
    return result


def _diagnose(
    bundle: object, outcome: SolveOutcome, case: ScenarioCase, result: ScenarioResult
) -> None:
    diagnosis = diagnose(
        bundle,  # type: ignore[arg-type]
        time_limit_s=case.time_limit_s,
        budget=ProbeBudget.from_settings(),
        cset=outcome.cset,
    )
    result.conflict_rules = tuple(
        sorted({rid for item in diagnosis.conflicts for rid in item.rule_ids})
    )
    result.sat_core_ids = diagnosis.core.sat_core_ids
    result.structural_ids = diagnosis.core.structural_ids
    result.escalate = diagnosis.escalate
    result.useful_proposals = len(diagnosis.useful_proposals)
    result.verified_proposals = len(diagnosis.verified_proposals)


# ─────────────────────────────────────────────────────────────────────
# 汇总
# ─────────────────────────────────────────────────────────────────────
@dataclass
class SuiteSummary:
    total: int = 0
    by_category: Mapping[str, int] = field(default_factory=dict)
    solved: int = 0
    infeasible: int = 0
    unknown: int = 0
    errors: int = 0
    #: 硬约束满足率 = 出解且主校验器 0 条 HARD 违规 / 出解总数
    hard_pass_rate: float = 0.0
    #: 格式校验通过率
    format_pass_rate: float = 0.0
    #: 对拍一致率（主校验器 vs naive checker）
    crosscheck_agreement: float = 0.0
    #: 阻塞项披露率
    disclosure_rate: float = 0.0
    #: 不可行族：INFEASIBLE 判定率、UNKNOWN 个数
    infeasible_family_correct: int = 0
    infeasible_family_total: int = 0
    infeasible_family_unknown: int = 0
    #: 冲突源召回率 / 精确率（micro：按并集计；macro：按场景平均）
    conflict_recall_micro: float = 0.0
    conflict_precision_micro: float = 0.0
    conflict_recall_macro: float = 0.0
    conflict_precision_macro: float = 0.0
    #: 边界对（enough 可解 ∧ short 不可解）
    boundary_pairs_ok: int = 0
    boundary_pairs_total: int = 0
    disagreements: tuple[str, ...] = ()


def summarize(cases: Sequence[ScenarioCase], results: Sequence[ScenarioResult]) -> SuiteSummary:
    by_id = {c.scenario_id: c for c in cases}
    summary = SuiteSummary(total=len(results))
    counts: dict[str, int] = {}
    solved = [r for r in results if r.status in SOLVED_STATUSES]
    disagreements: list[str] = []
    recalls: list[float] = []
    precisions: list[float] = []
    hit_total = 0
    annotated_total = 0
    reported_total = 0

    for result in results:
        counts[result.category] = counts.get(result.category, 0) + 1
        if result.error:
            summary.errors += 1
        if result.status == "INFEASIBLE":
            summary.infeasible += 1
        if result.status == "UNKNOWN":
            summary.unknown += 1
        if not result.agrees:
            disagreements.append(
                f"{result.scenario_id}: main={list(result.main_rules)} naive={list(result.naive_rules)}"
            )
        case = by_id[result.scenario_id]
        if case.category == "infeasible":
            summary.infeasible_family_total += 1
            if result.status == "INFEASIBLE":
                summary.infeasible_family_correct += 1
            if result.status == "UNKNOWN":
                summary.infeasible_family_unknown += 1
            annotated = set(case.annotated_conflict_rules)
            reported = set(result.conflict_rules)
            if annotated:
                hit = annotated & reported
                hit_total += len(hit)
                annotated_total += len(annotated)
                reported_total += len(reported)
                recalls.append(len(hit) / len(annotated))
                precisions.append(len(hit) / len(reported) if reported else 0.0)

    summary.solved = len(solved)
    summary.by_category = counts
    if solved:
        summary.hard_pass_rate = sum(1 for r in solved if r.main_passed) / len(solved)
        summary.format_pass_rate = sum(1 for r in solved if r.format_passed) / len(solved)
        summary.disclosure_rate = sum(1 for r in solved if not r.disclosure_gaps) / len(solved)
    summary.crosscheck_agreement = (
        sum(1 for r in results if r.agrees) / len(results) if results else 0.0
    )
    summary.conflict_recall_micro = hit_total / annotated_total if annotated_total else 0.0
    summary.conflict_precision_micro = hit_total / reported_total if reported_total else 0.0
    summary.conflict_recall_macro = sum(recalls) / len(recalls) if recalls else 0.0
    summary.conflict_precision_macro = sum(precisions) / len(precisions) if precisions else 0.0

    pairs: dict[str, dict[str, ScenarioResult]] = {}
    for result in results:
        case = by_id[result.scenario_id]
        if case.pair_id and case.pair_role:
            pairs.setdefault(case.pair_id, {})[case.pair_role] = result
    for members in pairs.values():
        summary.boundary_pairs_total += 1
        enough = members.get("enough")
        short = members.get("short")
        if enough and short and enough.status in SOLVED_STATUSES and short.status == "INFEASIBLE":
            summary.boundary_pairs_ok += 1
    summary.disagreements = tuple(disagreements)
    return summary


__all__ = [
    "SOLVED_STATUSES",
    "ScenarioResult",
    "SuiteSummary",
    "c_class_airspaces",
    "load_entities",
    "run_case",
    "summarize",
]
