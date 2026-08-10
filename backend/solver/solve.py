"""求解入口：候选枚举 → CP-SAT 建模 → 分阶段求解 → `SchedulePlan`。

## 三态严格分离（铁律 8 / v6 §3.6）

| 概念 | 含义 | 是否输出方案 |
|---|---|---|
| `OPTIMAL` / `FEASIBLE` | 求出解（后者为超时但有解，标注非最优） | 是 |
| `INFEASIBLE` | 约束集合在数学上不可满足 | 否，进诊断 |
| `UNKNOWN` | 超时，**既未证明可行也未证明不可行** | 有解则输出并标注 |

`UNKNOWN` 与 `INFEASIBLE` 混为一谈是排班系统最伤信任的 bug，所以这里
`SolveOutcome.status` 直接透传 `SolveRun.status`，中途没有任何「兜底成
INFEASIBLE」的分支。BLOCKED 是**第四个**概念：先修未满足的组合被正常排除，
不影响求解状态，但必须 100% 披露（`SchedulePlan.blocked_items`）。

## 可复现性（铁律 9）

同 `snapshot_id` + 同 `ruleset_version` + 同 `semantics_version` + `seed=42`
+ 同 `num_workers` → 逐字节可复现。落点：

- 候选顺序由 `Candidate.sort_key` 固定，变量名由 `Candidate.key` 固定；
- 架次编号按 (日期, 起飞时刻, 课目, 机号) 排序后顺序发号，不依赖字典遍历序；
- `content_sha256` 只对方案内容取哈希，**时间戳不进哈希**；
- `num_workers` 与 `random_seed` 进 `SolverStats`，也进 manifest（§10.6）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from backend.core.ruleset import IDENTITY_INSTRUCTOR
from backend.nodes.compile_spec import SpecBundle, compile_spec
from backend.schemas.plan import (
    BlockedItem,
    CrewMember,
    SchedulePlan,
    Sortie,
    TrainingDebt,
    Weekday,
)
from backend.schemas.solver import SolverStats
from backend.solver.candidates import Candidate, CandidateSet, enumerate_candidates
from backend.solver.data import NO_OVERRIDES, ProblemData, ScenarioOverrides
from backend.solver.model import BuiltModel, FrozenSortie, RelaxationSettings, build_model
from backend.solver.objective import SolveRun, solve_staged

#: `Sortie.weekday` 的取值（附录 B），按 `date.weekday()` 下标取
WEEKDAYS: tuple[Weekday, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


@dataclass(frozen=True)
class SolveOutcome:
    """一次求解的完整产物。`plan is None` 当且仅当没有可行解。"""

    status: str
    stats: SolverStats
    plan: SchedulePlan | None
    blocked_items: tuple[BlockedItem, ...]
    debts: tuple[TrainingDebt, ...]
    run: SolveRun
    cset: CandidateSet
    built: BuiltModel
    bundle: SpecBundle

    @property
    def sorties(self) -> tuple[Sortie, ...]:
        return tuple(self.plan.sorties) if self.plan else ()


def model_size(built: BuiltModel) -> tuple[int, int]:
    """(变量数, 约束数) —— 直接数 CP-SAT 的 proto，不估算。"""
    proto = built.model.proto
    return len(proto.variables), len(proto.constraints)


def build_stats(built: BuiltModel, run: SolveRun, *, relaxation_tier: int) -> SolverStats:
    """把求解统计装成契约对象（v6 §8.2 求解面板 + §3.11 可复现性）。"""
    num_vars, num_cts = model_size(built)
    return SolverStats(
        status=run.status,
        num_candidates=len(built.cset.candidates),
        num_variables=num_vars,
        num_constraints=num_cts,
        objective_value=run.objective_value if run.has_solution else None,
        best_bound=run.best_bound,
        gap=run.gap,
        wall_time_ms=run.wall_time_s * 1000.0,
        num_branches=run.num_branches,
        num_conflicts=run.num_conflicts,
        random_seed=built.spec.solver_seed,
        num_workers=built.spec.solver_workers,
        relaxation_tier=relaxation_tier,
    )


# ─────────────────────────────────────────────────────────────────────
# 解 → SchedulePlan
# ─────────────────────────────────────────────────────────────────────
def _crew_of(cand: Candidate, data: ProblemData) -> list[CrewMember]:
    """§3.1.1 机组编成：带飞 = 1 教员 + 1 学员；单飞/复训 = 1 人。"""
    trainee = data.persons[cand.trainee_id]
    if cand.instructor_id is None:
        return [
            CrewMember(
                person_id=trainee.person_id,
                name=trainee.name,
                role="复训" if cand.is_recurrent else "单飞",
            )
        ]
    instructor = data.persons[cand.instructor_id]
    return [
        CrewMember(person_id=instructor.person_id, name=instructor.name, role="教员"),
        CrewMember(person_id=trainee.person_id, name=trainee.name, role="学员"),
    ]


def build_plan(built: BuiltModel, run: SolveRun, *, relaxation_tier: int) -> SchedulePlan:
    """把求解结果装成 `SchedulePlan`（附录 B），含 `runway_id` 与 `is_recurrent`。"""
    data = built.data
    spec = built.spec
    cset = built.cset

    rows: list[tuple[date, int, str, str, int]] = []
    for idx in run.selected:
        cand = cset.candidates[idx]
        rows.append(
            (
                data.date_of(cand.day),
                run.starts[idx],
                cand.mission_id,
                cand.aircraft_id,
                idx,
            )
        )
    rows.sort()

    sorties: list[Sortie] = []
    for seq, (when, minute, _mission_id, _aircraft_id, idx) in enumerate(rows, start=1):
        cand = cset.candidates[idx]
        mission = data.missions[cand.mission_id]
        sorties.append(
            Sortie(
                sortie_id=f"S{seq:06d}",
                date=when,
                weekday=WEEKDAYS[when.weekday()],
                takeoff=data.clock_of(minute),
                landing=data.clock_of(minute + mission.duration_minutes),
                mission_id=cand.mission_id,
                mission_name=mission.name,
                airspace_id=mission.airspace_id,  # type: ignore[arg-type]
                aircraft_id=cand.aircraft_id,
                runway_id=run.runways[idx],  # type: ignore[arg-type]
                is_recurrent=cand.is_recurrent,
                crew=_crew_of(cand, data),
            )
        )

    debts = compute_debts(built, run, relaxation_tier=relaxation_tier)
    payload = {
        "iso_week": data.iso_week,
        "week_start": data.week_start.isoformat(),
        "week_end": data.week_end.isoformat(),
        "snapshot_id": spec.snapshot_id,
        "ruleset_version": spec.ruleset_version,
        "semantics_version": spec.semantics_version,
        "semantics_switches": dict(sorted(spec.semantics_switches.items())),
        "runway_model": spec.runway_model,
        "relaxation_tier": relaxation_tier,
        "sorties": [s.model_dump(mode="json") for s in sorties],
        "debts": [d.model_dump(mode="json") for d in debts],
        "blocked_items": [b.model_dump(mode="json") for b in cset.blocked_items],
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()

    return SchedulePlan(
        plan_id=f"{data.iso_week}-{digest[:12]}",
        iso_week=data.iso_week,
        week_start=data.week_start,
        week_end=data.week_end,
        snapshot_id=spec.snapshot_id,
        ruleset_version=spec.ruleset_version,
        semantics_version=spec.semantics_version,
        semantics_switches=dict(sorted(spec.semantics_switches.items())),
        runway_model=spec.runway_model,
        relaxation_tier=relaxation_tier,
        sorties=sorties,
        debts=list(debts),
        blocked_items=list(cset.blocked_items),
        content_sha256=digest,
    )


def compute_debts(
    built: BuiltModel, run: SolveRun, *, relaxation_tier: int
) -> tuple[TrainingDebt, ...]:
    """结算训练欠账（附录 B `TrainingDebt`）。

    `required` 取 `DebtBasis.required`（按 freq_days 滑窗推出的本周应排次数），
    `scheduled` 数实际排上的架次。**Tier 0 下所有要求都是硬约束，欠账必然为 0**；
    非 0 只可能出现在松弛档下，且 100% 显式披露（v6 §0.3）。
    """
    counts: dict[tuple[str, str], int] = {}
    for idx in run.selected:
        cand = built.cset.candidates[idx]
        key = (cand.trainee_id, cand.mission_id)
        counts[key] = counts.get(key, 0) + 1

    debts: list[TrainingDebt] = []
    for basis in built.cset.debt_basis:
        scheduled = counts.get((basis.person_id, basis.mission_id), 0)
        debt = max(0, basis.required - scheduled)
        if debt == 0:
            continue
        tier = max(1, min(3, relaxation_tier))
        debts.append(
            TrainingDebt(
                person_id=basis.person_id,
                mission_id=basis.mission_id,
                required=basis.required,
                scheduled=scheduled,
                debt=debt,
                relaxed_by=f"TIER{tier}",  # type: ignore[arg-type]
            )
        )
    return tuple(sorted(debts, key=lambda d: (d.person_id, d.mission_id)))


def plan_to_selection(plan: SchedulePlan, cset: CandidateSet, data: ProblemData) -> tuple[int, ...]:
    """把一版方案反解回候选下标（阶段2 汉明距离与 warm start 要用）。"""
    index: dict[tuple[str, str, int, str, str | None], int] = {
        (c.trainee_id, c.mission_id, c.day, c.aircraft_id, c.instructor_id): i
        for i, c in enumerate(cset.candidates)
    }
    out: list[int] = []
    for sortie in plan.sorties:
        instructor = next((m.person_id for m in sortie.crew if m.role == "教员"), None)
        trainee = next(
            (m.person_id for m in sortie.crew if m.role != "教员"),
            sortie.crew[0].person_id,
        )
        key = (
            trainee,
            sortie.mission_id,
            data.day_index(sortie.date),
            sortie.aircraft_id,
            instructor,
        )
        if key in index:
            out.append(index[key])
    return tuple(sorted(out))


def frozen_from_plan(
    plan: SchedulePlan, data: ProblemData, sortie_ids: Sequence[str]
) -> tuple[FrozenSortie, ...]:
    """把方案里指定的架次转成冻结项（含起飞分钟与跑道）。"""
    wanted = set(sortie_ids)
    out: list[FrozenSortie] = []
    for sortie in plan.sorties:
        if sortie.sortie_id not in wanted:
            continue
        instructor = next((m.person_id for m in sortie.crew if m.role == "教员"), None)
        trainee = next(
            (m.person_id for m in sortie.crew if m.role != "教员"),
            sortie.crew[0].person_id,
        )
        out.append(
            FrozenSortie(
                trainee_id=trainee,
                mission_id=sortie.mission_id,
                day=data.day_index(sortie.date),
                aircraft_id=sortie.aircraft_id,
                instructor_id=instructor,
                takeoff_minute=data.minutes_of(sortie.takeoff),
                runway_id=sortie.runway_id,
            )
        )
    return tuple(out)


# ─────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────
def solve(
    bundle: SpecBundle,
    *,
    relaxation: RelaxationSettings | None = None,
    prev_plan: SchedulePlan | None = None,
    frozen: Sequence[FrozenSortie] = (),
    warm_start: bool = True,
    capture_log: bool = False,
) -> SolveOutcome:
    """在已编译的规格上求解一周排班。"""
    relax = relaxation or RelaxationSettings(tier=bundle.spec.relaxation_tier)
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    prev_selected: tuple[int, ...] | None = None
    hints: Mapping[int, int] | None = None
    if prev_plan is not None:
        prev_selected = plan_to_selection(prev_plan, cset, bundle.data)
        if warm_start:
            hints = {i: (1 if i in set(prev_selected) else 0) for i in range(len(cset.candidates))}

    built = build_model(
        bundle.data,
        bundle.spec,
        cset,
        ruleset=bundle.ruleset,
        semantics=bundle.semantics,
        relaxation=relax,
        frozen=frozen,
        hints=hints,
    )
    run = solve_staged(built, prev_selected=prev_selected, capture_log=capture_log)
    plan = build_plan(built, run, relaxation_tier=relax.tier) if run.has_solution else None
    debts = tuple(plan.debts) if plan else ()
    return SolveOutcome(
        status=run.status,
        stats=build_stats(built, run, relaxation_tier=relax.tier),
        plan=plan,
        blocked_items=cset.blocked_items,
        debts=debts,
        run=run,
        cset=cset,
        built=built,
        bundle=bundle,
    )


def solve_week(
    session: Session,
    *,
    snapshot_id: str,
    week_start: date,
    overrides: ScenarioOverrides = NO_OVERRIDES,
    relaxation: RelaxationSettings | None = None,
    time_limit_s: float | None = None,
    workers: int | None = None,
    seed: int | None = None,
    capture_log: bool = False,
    materialize: bool = True,
) -> SolveOutcome:
    """便捷入口：编译规格（含 `training_progress` 物化）后直接求解。"""
    bundle = compile_spec(
        session,
        snapshot_id=snapshot_id,
        week_start=week_start,
        relaxation_tier=relaxation.tier if relaxation else 0,
        overrides=overrides,
        time_limit_s=time_limit_s,
        workers=workers,
        seed=seed,
        materialize=materialize,
    )
    return solve(bundle, relaxation=relaxation, capture_log=capture_log)


def instructor_ids(data: ProblemData) -> tuple[str, ...]:
    return tuple(
        pid for pid, p in sorted(data.persons.items()) if p.identity == IDENTITY_INSTRUCTOR
    )


__all__ = [
    "WEEKDAYS",
    "SolveOutcome",
    "build_plan",
    "build_stats",
    "compute_debts",
    "frozen_from_plan",
    "instructor_ids",
    "model_size",
    "plan_to_selection",
    "solve",
    "solve_week",
]
