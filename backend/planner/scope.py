"""影响面探测与自我降档（v6 §7.3.3 第 ① 步）。

```python
radius = estimate_scope(intent, state.prev_plan)
if radius > BLAST_RADIUS_THRESHOLD and intent.freeze_policy == "AGGRESSIVE":
    intent = downgrade_freeze(intent, reason=f"预计影响 {radius} 个架次，超出阈值")
```

## 三档冻结策略各自「牵连」多少

冻结策略决定的是「除了直接相关的架次，还允许动多少」。三档的语义：

| 档 | 允许改动的范围 |
|---|---|
| `CONSERVATIVE` | 只有**直接命中**范围的架次（点名的人 / 飞机 / 课目） |
| `BALANCED` | 直接命中 + **同日同机同人**的关联架次（默认档） |
| `AGGRESSIVE` | 整周全部架次 |

`estimate_scope` 按这三档在**上一版方案**上数架次。没有上一版（首轮排班）时
影响面按定义为 0——首轮不存在「扰动」，那是重排才有的概念。

## 为什么不复用 `solver/reschedule.py` 的冻结选择

那边算的是「哪些架次要真的钉住」，输入是完整的 `ProblemData` 与 `Disruption`，
是求解的一部分。这边算的是「跟用户说这次大概会动多少」，输入只有一个
`SchedulePlan`。**估算不该把求解器的数据装配拖进 Planner**——Planner 是个
LLM 节点，它的每一次调用都要装配上下文，多背一份 `ProblemData` 只会挤爆 8K 窗口。
两者算的不是同一件事，也就不存在「两份实现会漂移」的问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from backend.schemas.intent import FreezePolicy, SolveIntent
from backend.schemas.plan import SchedulePlan, Sortie

#: 自我降档的目标档：AGGRESSIVE 只降一档到 BALANCED，不一步降到 CONSERVATIVE。
#: 一步降到底会把「用户明确要求大改」直接压成「几乎不动」，那是另一种擅自决定。
_DOWNGRADE: dict[FreezePolicy, FreezePolicy] = {
    "AGGRESSIVE": "BALANCED",
    "BALANCED": "CONSERVATIVE",
    "CONSERVATIVE": "CONSERVATIVE",
}


def _direct_hits(plan: SchedulePlan, intent: SolveIntent) -> list[Sortie]:
    """直接命中范围的架次。"""
    persons = intent.scope_persons
    missions = intent.scope_missions
    all_persons = persons == "ALL"
    all_missions = missions == "ALL"
    if all_persons and all_missions:
        return list(plan.sorties)

    person_set = set() if all_persons else set(persons)
    mission_set = set() if all_missions else set(missions)
    hits: list[Sortie] = []
    for sortie in plan.sorties:
        person_hit = all_persons or any(c.person_id in person_set for c in sortie.crew)
        mission_hit = all_missions or sortie.mission_id in mission_set
        if person_hit and mission_hit:
            hits.append(sortie)
    return hits


def _related(plan: SchedulePlan, seed: list[Sortie]) -> list[Sortie]:
    """同日同机 / 同日同人的关联架次（`BALANCED` 档的牵连面）。

    「同日同机」是因为周转时间（约束7）把同一天同一架飞机的架次串成一条链，
    动一个必然挤到后面的；「同日同人」是因为休息与日上限（约束8/12）同理。
    """
    keys = {(s.date, s.aircraft_id) for s in seed}
    person_keys = {(s.date, c.person_id) for s in seed for c in s.crew}
    seed_ids = {s.sortie_id for s in seed}
    out = list(seed)
    for sortie in plan.sorties:
        if sortie.sortie_id in seed_ids:
            continue
        if (sortie.date, sortie.aircraft_id) in keys or any(
            (sortie.date, c.person_id) in person_keys for c in sortie.crew
        ):
            out.append(sortie)
    return out


def estimate_scope(intent: SolveIntent, prev_plan: SchedulePlan | None) -> int:
    """预计受影响的架次数（v6 §7.3.2 的 `estimated_blast_radius`）。

    首轮排班（`prev_plan is None`）恒为 0：没有既有方案就没有扰动。
    """
    if prev_plan is None:
        return 0
    if intent.freeze_policy == "AGGRESSIVE":
        return len(prev_plan.sorties)
    hits = _direct_hits(prev_plan, intent)
    if intent.freeze_policy == "BALANCED":
        return len(_related(prev_plan, hits))
    return len(hits)


def downgrade_freeze(intent: SolveIntent, *, reason: str) -> SolveIntent:
    """自我降档：把冻结策略降一档，并把理由写进 `freeze_reason`。

    `freeze_reason` **原样进 Sheet 4**（v6 §7.3.2），所以这里拼的是给人看的
    句子，不是日志格式。降档这件事必须在报告里留痕：排班员看到「只动了 12 个
    架次」时，得能查到「因为影响面超阈值系统自己降了一档」。
    """
    target = _DOWNGRADE[intent.freeze_policy]
    if target == intent.freeze_policy:
        return intent
    return intent.model_copy(
        update={
            "freeze_policy": target,
            "freeze_reason": f"{intent.freeze_reason}；已自动降档 "
            f"{intent.freeze_policy} → {target}（{reason}）",
        }
    )


@dataclass(frozen=True)
class DisruptionReport:
    """相对基线方案的影响面（v6 §7.7.2 的 `assess_disruption`）。"""

    baseline_plan_id: str
    total_baseline: int
    total_new: int
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    @property
    def touched(self) -> int:
        """被动过的架次总数 —— 这才是「扰动」的口径，不是新旧总数之差。"""
        return len(self.added) + len(self.removed) + len(self.changed)

    def summary(self) -> str:
        return (
            f"相对 {self.baseline_plan_id or '（无基线）'}："
            f"新增 {len(self.added)}、取消 {len(self.removed)}、改动 {len(self.changed)}，"
            f"合计触及 {self.touched} 个架次"
        )


def _fingerprint(sortie: Sortie) -> tuple[str, ...]:
    """判定「改没改」的字段集。

    **只取业务上可感知的字段**：日期、时刻、机号、跑道、空域、课目、机组。
    `is_recurrent` 是派生标记，不单独算改动。
    """
    return (
        sortie.date.isoformat(),
        sortie.takeoff.isoformat(),
        sortie.landing.isoformat(),
        sortie.mission_id,
        sortie.aircraft_id,
        sortie.runway_id,
        sortie.airspace_id,
        "|".join(sorted(f"{c.person_id}:{c.role}" for c in sortie.crew)),
    )


def assess_disruption(baseline: SchedulePlan | None, candidate: SchedulePlan) -> DisruptionReport:
    """比对两版方案，给出扰动明细。"""
    if baseline is None:
        return DisruptionReport(
            baseline_plan_id="",
            total_baseline=0,
            total_new=len(candidate.sorties),
            added=(),
            removed=(),
            changed=(),
        )
    old = {s.sortie_id: s for s in baseline.sorties}
    new = {s.sortie_id: s for s in candidate.sorties}
    added = tuple(sorted(set(new) - set(old)))
    removed = tuple(sorted(set(old) - set(new)))
    changed = tuple(
        sorted(
            sid for sid in set(old) & set(new) if _fingerprint(old[sid]) != _fingerprint(new[sid])
        )
    )
    return DisruptionReport(
        baseline_plan_id=baseline.plan_id,
        total_baseline=len(baseline.sorties),
        total_new=len(candidate.sorties),
        added=added,
        removed=removed,
        changed=changed,
    )


ScopeVerdict = Literal["ok", "downgraded"]


@dataclass(frozen=True)
class ScopeDecision:
    """影响面探测 + 自我降档的合并结果。"""

    intent: SolveIntent
    radius: int
    verdict: ScopeVerdict
    reason: str = ""


def apply_scope_policy(
    intent: SolveIntent,
    prev_plan: SchedulePlan | None,
    *,
    threshold: int,
) -> ScopeDecision:
    """v6 §7.3.3 第 ① 步的完整落地。

    降档后**重算一次影响面**：降档的意义就是缩小它，不重算的话
    `estimated_blast_radius` 记的还是降档前那个数，写进 Sheet 4 就是错的。
    """
    radius = estimate_scope(intent, prev_plan)
    if radius > threshold and intent.freeze_policy == "AGGRESSIVE":
        reason = f"预计影响 {radius} 个架次，超出阈值 {threshold}"
        downgraded = downgrade_freeze(intent, reason=reason)
        new_radius = estimate_scope(downgraded, prev_plan)
        return ScopeDecision(
            intent=downgraded.model_copy(update={"estimated_blast_radius": new_radius}),
            radius=new_radius,
            verdict="downgraded",
            reason=reason,
        )
    return ScopeDecision(
        intent=intent.model_copy(update={"estimated_blast_radius": radius}),
        radius=radius,
        verdict="ok",
    )


__all__ = [
    "DisruptionReport",
    "ScopeDecision",
    "ScopeVerdict",
    "apply_scope_policy",
    "assess_disruption",
    "downgrade_freeze",
    "estimate_scope",
]
