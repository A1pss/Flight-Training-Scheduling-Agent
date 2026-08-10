"""不可行诊断：最小冲突集 → 归因 → 松弛提案 → **实证验证**（v6 §3.9 / §3.10）。

## 三条不能松的规矩

1. **未经 `probe_solve` 实际验证过的提案，不得呈现给用户**（v6 §3.9.1）。
   Diagnosis 的产物会引导训练主任去放宽约束11 这类管理刚性规则，一个编错的归因
   （把「缺 JL-9」说成「缺教员」）会导致错误授权。与 Explain 不同，Diagnosis 的
   事实基础**完全可以自动核验** —— 每条提案都能真的跑一遍求解看结果。
   `UNKNOWN` 的标注「探针超时，未能确认此方案可行」；`INFEASIBLE` 的**直接丢弃**。
2. **R0 恒不可松弛**（v6 §3.10）。`RelaxationProposal` 的契约层已经堵死
   （`rule_tier == "R0"` 直接抛），本模块另有一层：候选动作表里根本不存在
   针对 R0 的条目。空域容量与跑道密度都归 R0；**空域关闭是外部扰动输入，
   不是松弛动作**，两者不要混。
3. **探针有独立预算池**（v6 §3.9.2）：单次 30s / 单请求 5 次 / 累计 120s。
   超限即停止探测，已验证的提案照常呈现，未验证的标注「预算耗尽，未验证」。
   这个池子独立于 Harness 的 LLM 预算，两者互不挤占。

## 松弛阶梯（v6 §3.10）

```
Tier 0  全硬约束
Tier 1  约束13 的频率窗口降级为软目标（允许欠账，最大化完成度）
Tier 2  Tier1 + 约束3「A 类每周必飞」整体降级为软目标   ← D-6 重定义
Tier 3  Tier2 + 经授权放宽 R1（约束10/11/12，需人工审批后执行）
```

> **Tier 2 为何重定义**：v5.2 的 Tier 2 是「A 类每周必飞次数降至每人 1 次」，
> 而 S-02 裁定之后**基线本来就是每人 1 次**，该档位成了空操作。D-6 裁定改为
> 「约束3 整体降级为软目标」，即允许某些学员本周完全不飞 A 类，欠账显式披露。

## Tier 3 的「放宽多少」是**探出来的**，不是代码拍的

v6 §3.9 第 2 步「量化：跑探针求解，逐个放宽单个约束组，**测出最小放宽量**」。
所以 R1 提案不带任何预设增量，而是从 +1 起按倍增探测（1, 2, 4, 8…），
第一个让问题可行的增量就是提案里的数字。代码不替训练主任拍「临时提至 16」。
"""

from __future__ import annotations

import time as _time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Final

from ortools.sat.python import cp_model
from sqlalchemy.exc import SQLAlchemyError

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.nodes.compile_spec import SpecBundle
from backend.retrieval.prereq_cte import fetch_prereq_chain
from backend.schemas.solver import ConflictItem, ProbeResult, RelaxationProposal, SolveStatus
from backend.solver.candidates import (
    DROP_AIRCRAFT_MAINTENANCE,
    DROP_AIRSPACE_CLOSED,
    DROP_NO_CAPABLE_AIRCRAFT,
    DROP_NO_INSTRUCTOR,
    DROP_NO_RUNWAY,
    CandidateSet,
    enumerate_candidates,
)
from backend.solver.model import ConstraintGroup, RelaxationSettings, build_model
from backend.solver.objective import make_solver, map_status
from backend.solver.solve import SolveOutcome, solve

#: 预筛原因码 → 它真正指向的规则编号（§3.9 第 1 步的归因链）。
#:
#: 「全天维护」同时归**约束6 资源有效性**与**约束7 维护时段**：v6 §12.3 的 I2
#: 把「机队全部维护」标为约束6 的冲突，而建模落点在约束7 的固定区间 ——
#: 两个编号都报出来，召回率优先（§12.3 要求冲突源召回 100%、精确率仅 ≥60%）。
DROP_TO_RULE: Final[dict[str, tuple[int, ...]]] = {
    DROP_NO_CAPABLE_AIRCRAFT: (6,),
    DROP_AIRCRAFT_MAINTENANCE: (6, 7),
    DROP_AIRSPACE_CLOSED: (6,),
    DROP_NO_RUNWAY: (9,),
    DROP_NO_INSTRUCTOR: (3, 4),
}

#: R1 上限的倍增探测序列（v6 §3.9 第 2 步「测出最小放宽量」）
R1_PROBE_STEPS: Final[tuple[int, ...]] = (1, 2, 4, 8)


# ─────────────────────────────────────────────────────────────────────
# 探针预算池（v6 §3.9.2）
# ─────────────────────────────────────────────────────────────────────
@dataclass
class ProbeBudget:
    """独立预算池。**超限不是异常，是一种要如实标注的结果。**"""

    per_call_s: float
    max_calls: int
    total_s: float
    calls: int = 0
    spent_s: float = 0.0

    @classmethod
    def from_settings(cls) -> ProbeBudget:
        s = get_settings()
        return cls(
            per_call_s=s.PROBE_TIME_LIMIT_S,
            max_calls=s.PROBE_MAX_CALLS,
            total_s=s.PROBE_TOTAL_BUDGET_S,
        )

    def is_exhausted(self) -> bool:
        """是否已超限。

        **刻意做成方法而不是 property**：mypy 会把 property 的真假值一路窄化下去
        （`if budget.is_exhausted(): continue` 之后，同一函数里后续的
        `if budget.is_exhausted()` 被判成 unreachable），而这个值会被 `record()` 改。
        方法调用不参与窄化。
        """
        return self.calls >= self.max_calls or self.spent_s >= self.total_s

    def next_limit(self) -> float:
        """本次探针可用的时限（单次上限与累计余额的较小值）。"""
        return max(0.0, min(self.per_call_s, self.total_s - self.spent_s))

    def record(self, seconds: float) -> None:
        self.calls += 1
        self.spent_s += seconds

    def snapshot(self) -> dict[str, float]:
        return {
            "calls": float(self.calls),
            "max_calls": float(self.max_calls),
            "spent_s": round(self.spent_s, 3),
            "total_s": self.total_s,
            "per_call_s": self.per_call_s,
        }


BUDGET_EXHAUSTED_NOTE: Final[str] = "⚠ 预算耗尽，未验证"
PROBE_TIMEOUT_NOTE: Final[str] = "探针超时，未能确认此方案可行"


# ─────────────────────────────────────────────────────────────────────
# 探针
# ─────────────────────────────────────────────────────────────────────
def probe_solve(
    bundle: SpecBundle,
    *,
    relaxation: RelaxationSettings,
    budget: ProbeBudget,
) -> tuple[ProbeResult | None, SolveOutcome | None]:
    """只读探针（v6 §3.9.1）。返回 `(None, None)` 表示预算已耗尽、没跑。

    `probe_solve` 是 `CLAUDE.md` 铁律 4 中确定性边界的**唯一例外** —— 它是只读
    探针，不产出交付方案，结果必须经 `validate_node` 才能进入输出。所以这里
    **绝不落库、绝不物化 `training_progress`**：`bundle` 是调用方已经编译好的规格，
    本函数只换一个时限重解。
    """
    if budget.is_exhausted():
        return None, None
    limit = budget.next_limit()
    if limit <= 0:
        return None, None

    probe_spec = bundle.spec.model_copy(
        update={"solver_time_limit_s": limit, "relaxation_tier": relaxation.tier}
    )
    probe_bundle = replace(bundle, spec=probe_spec)
    started = _time.monotonic()
    outcome = solve(probe_bundle, relaxation=relaxation)
    budget.record(_time.monotonic() - started)
    return (
        ProbeResult(
            status=outcome.status,  # type: ignore[arg-type]
            sorties=len(outcome.plan.sorties) if outcome.plan else 0,
            debts=list(outcome.debts),
            wall_time_ms=outcome.stats.wall_time_ms,
        ),
        outcome,
    )


# ─────────────────────────────────────────────────────────────────────
# 最小冲突集（v6 §3.9）
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ConflictCore:
    """最小冲突集：CP-SAT 的 assumption core + 结构性不可满足组。

    `sat_core_ids` 是 `sufficient_assumptions_for_infeasibility()` 原样给出的那一组；
    `structural_ids` 是另外补上的**结构性不可满足**组。

    ## 为什么要补

    CP-SAT 给的 core 是**极小**的：只要一组自相矛盾就够了，它不会把「另一组其实
    也同样矛盾」一并列出。I2（机队全部整周维护）实测就是这样 —— core 只有
    `C13_frequency`，而约束3「A 类每周必飞」在同一场景下同样一个候选都没有，
    v6 §12.3 恰恰把它列为预期冲突源。§12.3 对冲突集的要求是
    **「必须包含人工标注的真实冲突源 → 召回率 100%」**，极小 core 满足不了这条。

    所以在 core 之外再做一次**确定性的结构判定**：某个要求的候选范围为空集时，
    它所属的约束组必然不可满足（`Σ over ∅ ≥ 1`），直接并入冲突集。
    这不是猜，是逻辑上的必然，且不额外花一次求解。
    """

    status: SolveStatus
    group_ids: tuple[str, ...]
    groups: tuple[ConstraintGroup, ...]
    wall_time_s: float
    num_candidates: int
    sat_core_ids: tuple[str, ...] = ()
    structural_ids: tuple[str, ...] = ()


def find_conflict_core(
    bundle: SpecBundle,
    cset: CandidateSet,
    *,
    time_limit_s: float,
    relaxation: RelaxationSettings | None = None,
) -> ConflictCore:
    """用 assumption literals 让 CP-SAT 直接指出哪些约束组互斥（v6 §3.9）。

    只求可行性、不带目标 —— 诊断要的是「哪些约束打架」，不是「最优解长什么样」。
    """
    built = build_model(
        bundle.data,
        bundle.spec,
        cset,
        ruleset=bundle.ruleset,
        semantics=bundle.semantics,
        relaxation=relaxation or RelaxationSettings(),
        diagnose=True,
    )
    solver, _ = make_solver(
        seed=bundle.spec.solver_seed,
        workers=bundle.spec.solver_workers,
        time_limit_s=time_limit_s,
    )
    raw = solver.solve(built.model)
    status = map_status(raw)
    sat_core: tuple[str, ...] = ()
    structural: tuple[str, ...] = ()
    if raw == cp_model.INFEASIBLE:
        core = set(solver.sufficient_assumptions_for_infeasibility())
        sat_core = tuple(sorted(gid for gid, lit in built.assumptions.items() if lit.index in core))
        structural = structurally_unsatisfiable(bundle, cset)
    group_ids = tuple(sorted(set(sat_core) | set(structural)))
    return ConflictCore(
        status=status,
        group_ids=group_ids,
        groups=tuple(built.groups[gid] for gid in group_ids),
        wall_time_s=solver.wall_time,
        num_candidates=len(cset.candidates),
        sat_core_ids=sat_core,
        structural_ids=structural,
    )


def structurally_unsatisfiable(bundle: SpecBundle, cset: CandidateSet) -> tuple[str, ...]:
    """候选范围为空集的要求所属的约束组 —— 必然不可满足（见 :class:`ConflictCore`）。"""
    mission_class_of = {mid: m.mission_class for mid, m in bundle.data.missions.items()}
    hit: set[str] = set()
    for req in cset.requirements:
        if any(req.matches(c, mission_class_of) for c in cset.candidates):
            continue
        hit.add("C03_weekly" if req.rule_id == 3 else "C13_frequency")
    return tuple(sorted(hit))


# ─────────────────────────────────────────────────────────────────────
# 归因（v6 §3.9 第 1 步）
# ─────────────────────────────────────────────────────────────────────
def attribute(
    bundle: SpecBundle,
    cset: CandidateSet,
    core: ConflictCore,
    *,
    session: object | None = None,
) -> tuple[ConflictItem, ...]:
    """沿「人员→资质→课目→先修→机型→飞机→空域→跑道」把冲突组落到具体实体。

    最关键的一类归因是「**某个要求一个候选都没有**」：约束3 在 I2 那类场景下
    （6 架 JL-8 全部维护整周）会以 `Σ over ∅ ≥ 1` 的形式进冲突集，可**真正的根因
    是约束6 的机队**。静态预筛当时记下的 `DropReason` 就是为这一刻准备的 ——
    事后从一个空集合是反推不出原因的。

    `session` 给了就顺带把先修链的递归 CTE 查出来放进 `subjects`（v6 §6.1）。
    """
    data = bundle.data
    items: list[ConflictItem] = []
    for group in core.groups:
        rule_ids = set(group.rule_ids)
        subjects: list[str] = []

        if group.group_id in ("C03_weekly", "C13_frequency"):
            subjects, extra_rules = _attribute_requirements(bundle, cset, group.group_id, session)
            rule_ids |= extra_rules
        elif group.group_id == "C06_airspace":
            subjects = [
                f"空域 {aid}（容量 {data.capacity_of(aid)}）"
                for aid in sorted(data.airspaces)
                if data.capacity_of(aid) < data.airspaces[aid].capacity
                or data.capacity_of(aid) <= 1
            ]
        elif group.group_id == "C07_aircraft":
            subjects = [
                f"{ac.aircraft_id}（{ac.aircraft_type}，周转 {ac.turnaround_minutes} 分"
                + (f"，{len(ac.maintenance)} 段维护）" if ac.maintenance else "）")
                for _aid, ac in sorted(data.aircraft.items())
            ]
        elif group.group_id in ("C09_window", "C09_separation"):
            subjects = [
                f"跑道 {rid}（服务机型 {sorted(data.runways[rid].aircraft_types)}）"
                for rid in sorted(bundle.spec.runways)
            ] + [f"训练窗 {data.window_start}-{data.window_end}"]
        elif group.group_id in ("C10_daily_minutes", "C11_weekly_sorties", "C12_person_daily"):
            subjects = [
                f"{p.name}({pid})：{p.identity}，"
                f"周上限 {bundle.ruleset.weekly_sortie_cap(p.identity)} 架次、"
                f"日上限 {bundle.ruleset.daily_minute_cap(p.identity)} 分钟"
                for pid, p in sorted(data.persons.items())
            ]
        elif group.group_id == "C12_aircraft_daily":
            subjects = [f"单机单日上限 {bundle.ruleset.daily_sorties_per_aircraft} 架次"]
        elif group.group_id == "C01_window":
            subjects = [
                f"训练窗 {data.window_start}-{data.window_end}（{data.horizon_minutes} 分钟）"
            ]

        items.append(
            ConflictItem(
                group_id=group.group_id,
                rule_ids=[f"C{rid:02d}" for rid in sorted(rule_ids)],
                tier=group.tier,  # type: ignore[arg-type]
                description=group.description,
                subjects=subjects,
            )
        )
    return tuple(items)


def _attribute_requirements(
    bundle: SpecBundle,
    cset: CandidateSet,
    group_id: str,
    session: object | None,
) -> tuple[list[str], set[int]]:
    """约束3/13 的归因：哪些 (人, 课目/类别) 的候选被谁掐死了。"""
    data = bundle.data
    mission_class_of = {mid: m.mission_class for mid, m in data.missions.items()}
    target_rule = 3 if group_id == "C03_weekly" else 13
    subjects: list[str] = []
    extra_rules: set[int] = set()

    for req in cset.requirements:
        if req.rule_id != target_rule:
            continue
        scope = [c for c in cset.candidates if req.matches(c, mission_class_of)]
        if scope:
            continue  # 有候选 → 不是「一个都没有」这类根因
        person = data.persons.get(req.person_id)
        who = f"{person.name}({req.person_id})" if person else req.person_id
        what = req.mission_id or f"{req.mission_class} 类"
        reasons: list[str] = []
        for drop in cset.drops_for(req.person_id):
            if req.mission_id and drop.mission_id != req.mission_id:
                continue
            if req.mission_class and mission_class_of.get(drop.mission_id) != req.mission_class:
                continue
            reasons.append(f"{drop.mission_id}: {drop.label}（{drop.detail}）")
            extra_rules.update(DROP_TO_RULE.get(drop.code, ()))
        detail = "；".join(reasons[:6]) if reasons else "无可用候选"
        subjects.append(f"{who} 的 {what} 本周无任何可行候选 —— {detail}")

    if session is not None:
        for basis in cset.debt_basis[:3]:
            try:
                chain = fetch_prereq_chain(
                    session,  # type: ignore[arg-type]
                    basis.mission_id,
                    bundle.spec.snapshot_id,
                )
            except SQLAlchemyError as exc:
                # 归因是辅助信息：先修链查不出来不该拖垮整个诊断，但要留痕
                get_logger(__name__).warning(
                    "先修链归因查询失败",
                    extra={"mission_id": basis.mission_id, "error": str(exc)},
                )
                continue
            if chain:
                subjects.append(
                    f"{basis.mission_id} 先修链："
                    + "；".join(f"{e.mission_id}→{e.prereq_ref}({e.ref_kind})" for e in chain)
                )
    return subjects, extra_rules


# ─────────────────────────────────────────────────────────────────────
# 松弛提案（v6 §3.9.3 + §3.10）
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ProposalDraft:
    """未验证的提案草案 + 它对应的松弛设置。"""

    proposal_id: str
    tier: int
    action: str
    cost: str
    affected_rules: tuple[str, ...]
    rule_tier: str
    authority: str
    settings: RelaxationSettings
    escalating_field: str | None = None


def draft_proposals(bundle: SpecBundle, core: ConflictCore) -> tuple[ProposalDraft, ...]:
    """按松弛阶梯生成候选动作。**只针对冲突集里出现的可松弛规则。**

    R0 组在这里根本不生成条目 —— 这是「代码层硬编码禁止」的落点之一。
    """
    ruleset = bundle.ruleset
    hit_rules = {rid for g in core.groups for rid in g.rule_ids}
    drafts: list[ProposalDraft] = []

    for step in ruleset.ladder:
        if step.tier == 0:
            continue
        relaxes = [rid for rid in step.relaxes if ruleset.is_relaxable(rid)]
        if not relaxes or not (set(relaxes) & hit_rules):
            continue
        if step.tier <= 2:
            drafts.append(
                ProposalDraft(
                    proposal_id=f"TIER{step.tier}",
                    tier=step.tier,
                    action=step.note or step.name,
                    cost=(
                        "本周进度顺延，欠账记入下周并在 Sheet 4 显式披露"
                        if step.tier == 1
                        else "部分学员本周不飞每周必飞课目，熟练度下降，欠账显式披露"
                    ),
                    affected_rules=tuple(f"C{rid:02d}" for rid in sorted(relaxes)),
                    rule_tier=max((ruleset.tier_of(rid) for rid in relaxes), default="R2"),
                    authority="排班员",
                    settings=RelaxationSettings(tier=step.tier),
                )
            )
        else:
            # Tier 3：逐条 R1 单独提案，放宽量由探针测出（见模块文档）
            for rid in sorted(set(relaxes) & hit_rules):
                if ruleset.tier_of(rid) != "R1":
                    continue
                field_name = {
                    10: "daily_minutes_bonus",
                    11: "weekly_sorties_bonus",
                    12: "daily_sorties_bonus",
                }.get(rid)
                if field_name is None:
                    continue
                drafts.append(
                    ProposalDraft(
                        proposal_id=f"TIER3-C{rid:02d}",
                        tier=3,
                        action=f"经授权临时放宽约束{rid}（{ruleset.rules[rid].title}）",
                        cost="人员疲劳风险上升，需训练主任二次确认并记审计",
                        affected_rules=(f"C{rid:02d}",),
                        rule_tier="R1",
                        authority="训练主任",
                        settings=RelaxationSettings(tier=3),
                        escalating_field=field_name,
                    )
                )
    return tuple(drafts)


def verify_proposals(
    drafts: Sequence[ProposalDraft],
    bundle: SpecBundle,
    *,
    budget: ProbeBudget,
) -> tuple[RelaxationProposal, ...]:
    """v6 §3.9.1：**未经 `probe_solve` 实证验证过的提案一律不呈现。**

    - `OPTIMAL` / `FEASIBLE` → `verified=True`，带上可量化的代价（架次数 + 欠账）
    - `UNKNOWN` → `verified=False`，标注「探针超时，未能确认此方案可行」，仍呈现
    - `INFEASIBLE` → **直接丢弃，不呈现**
    - 预算耗尽 → `verified=False`，标注「预算耗尽，未验证」
    """
    out: list[RelaxationProposal] = []
    for draft in drafts:
        if budget.is_exhausted():
            out.append(_as_proposal(draft, verified=False, note=BUDGET_EXHAUSTED_NOTE))
            continue

        if draft.escalating_field is None:
            result, _ = probe_solve(bundle, relaxation=draft.settings, budget=budget)
            if result is None:
                out.append(_as_proposal(draft, verified=False, note=BUDGET_EXHAUSTED_NOTE))
            elif result.status in ("OPTIMAL", "FEASIBLE"):
                out.append(_as_proposal(draft, verified=True, result=result))
            elif result.status == "UNKNOWN":
                out.append(_as_proposal(draft, verified=False, note=PROBE_TIMEOUT_NOTE))
            # INFEASIBLE：丢弃
            continue

        # R1：倍增探测最小放宽量
        found: bool = False
        for bonus in R1_PROBE_STEPS:
            if budget.is_exhausted():
                break
            settings = replace(draft.settings, **{draft.escalating_field: bonus})
            r1_result, _ = probe_solve(bundle, relaxation=settings, budget=budget)
            if r1_result is None:
                break
            if r1_result.status in ("OPTIMAL", "FEASIBLE"):
                out.append(
                    _as_proposal(
                        draft,
                        verified=True,
                        result=r1_result,
                        action_suffix=f"：上限 +{bonus}（探针测出的最小放宽量）",
                    )
                )
                found = True
                break
        if not found and not budget.is_exhausted():
            continue  # 全部 INFEASIBLE → 丢弃
        if not found:
            out.append(_as_proposal(draft, verified=False, note=BUDGET_EXHAUSTED_NOTE))

    if out:
        # 推荐档位取最低的已验证提案（松弛越少越优先）
        verified = [p for p in out if p.verified]
        if verified:
            best = min(verified, key=lambda p: (p.tier, p.proposal_id))
            out = [
                p.model_copy(update={"recommended": p.proposal_id == best.proposal_id}) for p in out
            ]
    return tuple(out)


def _as_proposal(
    draft: ProposalDraft,
    *,
    verified: bool,
    result: ProbeResult | None = None,
    note: str | None = None,
    action_suffix: str = "",
) -> RelaxationProposal:
    return RelaxationProposal(
        proposal_id=draft.proposal_id,
        tier=draft.tier,
        action=draft.action + action_suffix,
        cost=draft.cost
        + (
            f"（探针实测：{result.sorties} 个架次、{len(result.debts)} 项欠账）"
            if result and verified
            else ""
        ),
        affected_rules=list(draft.affected_rules),
        rule_tier=draft.rule_tier,  # type: ignore[arg-type]
        authority=draft.authority,  # type: ignore[arg-type]
        verified=verified,
        verified_result=result if verified else None,
        note=note,
    )


# ─────────────────────────────────────────────────────────────────────
# 编排
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Diagnosis:
    """一次完整诊断的产物（v6 §3.9.3 的结构化形态）。"""

    status: SolveStatus
    core: ConflictCore
    conflicts: tuple[ConflictItem, ...]
    proposals: tuple[RelaxationProposal, ...]
    escalate: bool
    escalation_reason: str
    budget: dict[str, float] = field(default_factory=dict)

    @property
    def verified_proposals(self) -> tuple[RelaxationProposal, ...]:
        return tuple(p for p in self.proposals if p.verified)

    @property
    def useful_proposals(self) -> tuple[RelaxationProposal, ...]:
        """已验证**且真的排出了架次**的提案（见 :func:`diagnose` 的升级判定）。"""
        return tuple(
            p
            for p in self.verified_proposals
            if p.verified_result and p.verified_result.sorties > 0
        )


def diagnose(
    bundle: SpecBundle,
    *,
    time_limit_s: float | None = None,
    budget: ProbeBudget | None = None,
    session: object | None = None,
    cset: CandidateSet | None = None,
) -> Diagnosis:
    """完整诊断：冲突集 → 归因 → 提案 → 实证验证（v6 §3.9 四步）。

    `time_limit_s` 缺省取 `Settings.SOLVER_DIAGNOSE_TIME_LIMIT_S`（§3.11 的 300s）——
    不可行性**证明**比找解贵得多，v6 §12.3 的 I4/I5 专门要求用这个时限，
    好让判定落在 `INFEASIBLE` 而不是 `UNKNOWN`。
    """
    limit = time_limit_s or get_settings().SOLVER_DIAGNOSE_TIME_LIMIT_S
    pool = budget or ProbeBudget.from_settings()
    candidates = cset or enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    core = find_conflict_core(bundle, candidates, time_limit_s=limit)

    if core.status != "INFEASIBLE":
        return Diagnosis(
            status=core.status,
            core=core,
            conflicts=(),
            proposals=(),
            escalate=False,
            escalation_reason=(
                "本次求解并未判定 INFEASIBLE，无需诊断"
                if core.status != "UNKNOWN"
                else "诊断求解本身超时：既未证明可行也未证明不可行，不得当作 INFEASIBLE 处理"
            ),
            budget=pool.snapshot(),
        )

    conflicts = attribute(bundle, candidates, core, session=session)
    drafts = draft_proposals(bundle, core)
    proposals = verify_proposals(drafts, bundle, budget=pool)
    verified = [p for p in proposals if p.verified]
    # **「一个架次都不排」不算解决方案。** 资源被抹平的场景（机队全维护、跑道全关）
    # 下，松弛 R1/R2 能「可行」只是因为把要求本身撤掉了，探针给回来的是个空方案。
    # 那不是排班，是取消本周 —— 照 v6 §12.3 I2 的口径，这种情形的合格输出是
    # **升级人工，提示需调配资源**。提案照常呈现（它如实写着代价），但要标升级。
    useful = [p for p in verified if p.verified_result and p.verified_result.sorties > 0]
    escalate = not useful
    reason = ""
    if escalate:
        # R0 判定要看**归因后**的规则编号，不能只看冲突组自己的等级。
        # 「跑道全关」实测就是这个形状：候选被预筛清空，SAT core 只剩 R2 的
        # C03/C13 两组，真正的根因约束9 是归因阶段从 `DropReason` 补回来的。
        r0_rules = sorted(
            {
                rid
                for item in conflicts
                for rid in item.rule_ids
                if rid.startswith("C") and bundle.ruleset.tier_of(int(rid[1:])) == "R0"
            }
        )
        if r0_rules:
            reason = (
                f"冲突集含 R0 安全刚性约束（{', '.join(r0_rules)}），"
                "松弛 R1/R2 撤不掉它 —— 能验证通过的方案都是 0 架次（等于取消本周）。"
                "**升级人工，需调配资源。**"
            )
        elif not drafts:
            reason = "冲突集里没有任何可松弛规则 → 升级人工"
        else:
            reason = "已生成的松弛提案没有一条通过探针验证 → 升级人工"
    return Diagnosis(
        status=core.status,
        core=core,
        conflicts=conflicts,
        proposals=proposals,
        escalate=escalate,
        escalation_reason=reason,
        budget=pool.snapshot(),
    )


__all__ = [
    "BUDGET_EXHAUSTED_NOTE",
    "DROP_TO_RULE",
    "PROBE_TIMEOUT_NOTE",
    "R1_PROBE_STEPS",
    "ConflictCore",
    "Diagnosis",
    "ProbeBudget",
    "ProposalDraft",
    "attribute",
    "diagnose",
    "draft_proposals",
    "find_conflict_core",
    "probe_solve",
    "verify_proposals",
]
