"""局部重排：三档冻结策略 + 上一版解 warm start（v6 §3.8）。

场景原话：「高超一周都参加不了训练，AC84 本周维修，重新排班」。

| 档位 | 范围 | 特点 |
|---|---|---|
| `CONSERVATIVE` 保守 | 仅重排直接受影响架次 | 扰动最小，可能不可行 |
| `BALANCED` 平衡（默认） | 受影响架次 + 同日同机同人的关联架次 | 扰动与可行性平衡 |
| `AGGRESSIVE` 激进 | 全周重排，仅保留已实际执行的历史架次 | 最优但扰动大 |

**冻结是硬固定**：`x[c] == 1`、`start == 原起飞时刻`、**`rwy[c][原跑道] == 1`**。
跑道必须一并冻结 —— 只冻结时刻不冻结跑道，重排后同一个架次可能换到另一条跑道，
对塔台来说那已经是一次变更了，却不会出现在「扰动架次」清单里。

目标函数侧由 `objective.stage2_hamming` 承担「最小扰动」：上一版解既作
`AddHint` warm start，也进阶段2 的汉明距离目标（v6 §3.7）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, time

from backend.nodes.compile_spec import SpecBundle
from backend.schemas.intent import FreezePolicy
from backend.schemas.plan import SchedulePlan, Sortie
from backend.solver.data import ProblemData, ScenarioOverrides
from backend.solver.model import FrozenSortie, RelaxationSettings
from backend.solver.solve import SolveOutcome, solve

#: v6 §3.8 的三档。默认平衡档。
FREEZE_POLICIES: tuple[FreezePolicy, ...] = ("CONSERVATIVE", "BALANCED", "AGGRESSIVE")


@dataclass(frozen=True)
class Disruption:
    """一次扰动的描述（v6 §3.8 的 `disruptions` 参数）。

    与 :class:`~backend.solver.data.ScenarioOverrides` 的分工：本类描述
    「**哪些既有架次被打到了**」，用于挑受影响集合；`ScenarioOverrides` 描述
    「**世界变成什么样**」，用于重新枚举候选。一次重排通常两者都要给，
    且内容一致 —— :meth:`to_overrides` 就是把前者翻成后者，避免手写两遍写歪。
    """

    persons: frozenset[str] = frozenset()
    aircraft: frozenset[str] = frozenset()
    airspaces: frozenset[str] = frozenset()
    runways: frozenset[str] = frozenset()
    #: 受影响的天（相对排班周周一的下标）。空集 = 整周
    days: frozenset[int] = frozenset()
    reason: str = ""

    def affects_day(self, day: int) -> bool:
        return not self.days or day in self.days

    def to_overrides(
        self, data: ProblemData, *, window: tuple[time, time] | None = None
    ) -> ScenarioOverrides:
        """翻成候选枚举侧的扰动输入。"""
        days = sorted(self.days) if self.days else list(data.days)
        dates = frozenset(data.date_of(d) for d in days)
        return ScenarioOverrides(
            window_start=window[0] if window else None,
            window_end=window[1] if window else None,
            airspace_capacity=dict.fromkeys(sorted(self.airspaces), 0),
            closed_runways=self.runways,
            unavailable=dict.fromkeys(sorted(self.persons), dates),
            maintenance_all_day=tuple(
                (aid, data.date_of(min(days)), data.date_of(max(days)))
                for aid in sorted(self.aircraft)
            ),
        )


@dataclass(frozen=True)
class FreezeDecision:
    """冻结决策的完整交代：谁被重排、谁被冻结、为什么。"""

    policy: FreezePolicy
    frozen: tuple[FrozenSortie, ...]
    frozen_ids: tuple[str, ...]
    affected_ids: tuple[str, ...]
    released_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def blast_radius(self) -> int:
        """预计受影响架次数（进 `SolveIntent.estimated_blast_radius`）。"""
        return len(self.affected_ids) + len(self.released_ids)


def sortie_touches(sortie: Sortie, disruption: Disruption, data: ProblemData) -> bool:
    """该架次是否被扰动直接命中。"""
    day = data.day_index(sortie.date)
    if not disruption.affects_day(day):
        return False
    if disruption.aircraft and sortie.aircraft_id in disruption.aircraft:
        return True
    if disruption.runways and sortie.runway_id in disruption.runways:
        return True
    if disruption.airspaces and sortie.airspace_id in disruption.airspaces:
        return True
    return bool(disruption.persons) and any(m.person_id in disruption.persons for m in sortie.crew)


def select_frozen(
    plan: SchedulePlan,
    disruption: Disruption,
    policy: FreezePolicy,
    data: ProblemData,
    *,
    executed_ids: Sequence[str] = (),
) -> FreezeDecision:
    """按档位挑出要硬固定的架次（v6 §3.8）。

    `executed_ids` 只在激进档有意义 —— 那一档「仅保留已实际执行的历史架次」，
    而「已执行」是外部事实（谁真的飞过了），求解器无从知道，必须显式传进来。
    """
    affected = [s for s in plan.sorties if sortie_touches(s, disruption, data)]
    affected_ids = tuple(s.sortie_id for s in affected)

    if policy == "AGGRESSIVE":
        keep = {s.sortie_id for s in plan.sorties if s.sortie_id in set(executed_ids)}
        released = tuple(
            s.sortie_id
            for s in plan.sorties
            if s.sortie_id not in keep and s.sortie_id not in set(affected_ids)
        )
    elif policy == "CONSERVATIVE":
        keep = {s.sortie_id for s in plan.sorties if s.sortie_id not in set(affected_ids)}
        released = ()
    else:  # BALANCED
        linked_days = {data.day_index(s.date) for s in affected}
        linked_aircraft = {s.aircraft_id for s in affected}
        linked_persons = {m.person_id for s in affected for m in s.crew}
        released_list: list[str] = []
        keep = set()
        for s in plan.sorties:
            if s.sortie_id in set(affected_ids):
                continue
            same_day = data.day_index(s.date) in linked_days
            linked = same_day and (
                s.aircraft_id in linked_aircraft
                or any(m.person_id in linked_persons for m in s.crew)
            )
            if linked:
                released_list.append(s.sortie_id)
            else:
                keep.add(s.sortie_id)
        released = tuple(released_list)

    frozen: list[FrozenSortie] = []
    for sortie in plan.sorties:
        if sortie.sortie_id not in keep:
            continue
        instructor = next((m.person_id for m in sortie.crew if m.role == "教员"), None)
        trainee = next(
            (m.person_id for m in sortie.crew if m.role != "教员"), sortie.crew[0].person_id
        )
        frozen.append(
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
    return FreezeDecision(
        policy=policy,
        frozen=tuple(frozen),
        frozen_ids=tuple(sorted(keep)),
        affected_ids=affected_ids,
        released_ids=released,
    )


def local_reschedule(
    bundle: SpecBundle,
    prev_plan: SchedulePlan,
    disruption: Disruption,
    *,
    policy: FreezePolicy = "BALANCED",
    executed_ids: Sequence[str] = (),
    relaxation: RelaxationSettings | None = None,
    capture_log: bool = False,
) -> tuple[SolveOutcome, FreezeDecision]:
    """在扰动后的实体上重排，冻结未受影响的架次，上一版解作 warm start。

    `bundle` 必须是**已经把扰动编译进去**的规格（`ScenarioOverrides`），否则
    冻结的架次会连同扰动一起被当成仍然可行的。
    """
    decision = select_frozen(prev_plan, disruption, policy, bundle.data, executed_ids=executed_ids)
    outcome = solve(
        bundle,
        relaxation=relaxation,
        prev_plan=prev_plan,
        frozen=decision.frozen,
        capture_log=capture_log,
    )
    return outcome, decision


def week_start_of(plan: SchedulePlan) -> date:
    return plan.week_start


__all__ = [
    "FREEZE_POLICIES",
    "Disruption",
    "FreezeDecision",
    "local_reschedule",
    "select_frozen",
    "sortie_touches",
    "week_start_of",
]
