"""确定性节点 ④：`resume_guard`（v6 §9.2）。

> **批准一个基于过期数据的排班方案，是这类系统最典型的生产事故**——
> 方案本身合规，但合的是昨天的规。

HITL 可以隔天再来（`interrupt()` + PostgresSaver 让状态在 PG 里等着）。等待期间
数据可能变：有人请了假、飞机进了厂、空域临时关闭。恢复时必须先问一句
「我睡着的时候，天变了吗」。

```python
def resume_guard(state) -> Command:
    current = get_active_snapshot(state.tenant_id)
    if current != state.snapshot_id:
        diff = snapshot_diff(state.snapshot_id, current)
        if diff.affects(state.solution):        # 触及本方案涉及的人/机/日期/空域
            return Command(goto="planner", update={"errors": [FTS-3004], "snapshot_id": current})
        # 不触及则放行，但在 Sheet 4 记录快照已变更
    return Command(goto="human_gate")
```

## 「触及」的判据：实体 ∩ 方案，且日期 ∩ 排班周

只要 `snapshot_id` 变了就强制重解，会把「隔壁周的人员表补了个电话号码」也
算成陈旧，HITL 从此天天重解；反过来只看 `snapshot_id` 相等则等于没做检查。
所以判据是两层交集：

1. **实体交集** —— 变更的实体（人 / 机 / 课目 / **空域** / 跑道）是否出现在本方案里；
2. **日期交集** —— 变更字段带日期语义时（不可用日期、维护窗），那些日期是否
   落在本方案的排班周内。

**规则条文的变更一律触及**：它不挂在任何具体架次上，但它改的是判定本身。

## 不触及也不是「什么都没发生」

放行时会在黑板上记一条 `INFO` 与一条轨迹事件，`commit_plan` 把它带进 Sheet 4
区块 1 的备注。评审时能查到「批准这版时快照已经从 X 变成 Y，但变更与本方案无关」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from langgraph.types import Command
from sqlalchemy.orm import Session

from backend.core.errors import ErrorCode
from backend.graph.events import emit, error
from backend.graph.state import FTSState, model_get
from backend.graph.state import get as state_get
from backend.ingestion.diff import Change, diff_normalized
from backend.ingestion.loader import active_snapshot_id, load_snapshot_normalized
from backend.schemas.plan import SchedulePlan

#: 带日期语义的字段。值可能是单个 ISO 日期串，也可能是它们的列表/字典。
_DATE_FIELDS: frozenset[str] = frozenset(
    {
        "unavailable_dates",
        "maintenance",
        "maintenance_windows",
        "expiry_date",
        "qualifications",
        "closures",
        "unavailable",
        "cycle_start",
        "last_done_date",
    }
)

#: 实体类别 → 从方案里取出该类实体编号的办法。
_PLAN_ENTITIES: dict[str, str] = {
    "person": "person",
    "aircraft": "aircraft",
    "mission": "mission",
    "airspace": "airspace",
    "runway": "runway",
}


def plan_entity_ids(plan: SchedulePlan) -> dict[str, frozenset[str]]:
    """方案里实际涉及的实体。**空域与跑道一个都不能漏**（v6 §9.2 点名了空域）。"""
    persons: set[str] = set()
    aircraft: set[str] = set()
    missions: set[str] = set()
    airspaces: set[str] = set()
    runways: set[str] = set()
    for sortie in plan.sorties:
        aircraft.add(sortie.aircraft_id)
        missions.add(sortie.mission_id)
        airspaces.add(sortie.airspace_id)
        runways.add(sortie.runway_id)
        persons.update(c.person_id for c in sortie.crew)
    # 阻塞项与欠账里的人/课目同样属于「本方案涉及」——它们会出现在 Sheet 4 上，
    # 数据变了那几行的结论就变了
    persons.update(b.person_id for b in plan.blocked_items)
    missions.update(b.mission_id for b in plan.blocked_items)
    persons.update(d.person_id for d in plan.debts)
    missions.update(d.mission_id for d in plan.debts)
    return {
        "person": frozenset(persons),
        "aircraft": frozenset(aircraft),
        "mission": frozenset(missions),
        "airspace": frozenset(airspaces),
        "runway": frozenset(runways),
    }


def _collect_dates(value: Any) -> set[date]:
    """从任意嵌套结构里捞出 ISO 日期。捞不到就返回空集（视为「无日期语义」）。"""
    found: set[date] = set()
    if isinstance(value, str):
        try:
            found.add(date.fromisoformat(value))
        except ValueError:
            return found
        return found
    if isinstance(value, Mapping):
        for item in value.values():
            found |= _collect_dates(item)
        return found
    if isinstance(value, Sequence):
        for item in value:
            found |= _collect_dates(item)
    return found


def change_dates(change: Change) -> set[date]:
    """一条变更所涉及的日期。空集表示这条变更没有日期语义（如改了姓名）。"""
    fields = change.changed_fields or tuple(
        sorted(set(change.before or {}) | set(change.after or {}))
    )
    found: set[date] = set()
    for field in fields:
        if field not in _DATE_FIELDS:
            continue
        for payload in (change.before, change.after):
            if payload is not None and field in payload:
                found |= _collect_dates(payload[field])
    return found


@dataclass(frozen=True)
class StalenessVerdict:
    """陈旧性检查的结论。"""

    old_snapshot_id: str
    new_snapshot_id: str
    changed: bool
    affecting: tuple[Change, ...] = ()
    unrelated: tuple[Change, ...] = ()

    @property
    def affects_plan(self) -> bool:
        return bool(self.affecting)

    def summary(self) -> str:
        if not self.changed:
            return f"快照未变更（{self.old_snapshot_id}）"
        if not self.affecting:
            return (
                f"快照已由 {self.old_snapshot_id} 变更为 {self.new_snapshot_id}，"
                f"共 {len(self.unrelated)} 处变更，均**不触及**本方案"
            )
        touched = "、".join(f"{c.entity_type}:{c.entity_id}" for c in self.affecting[:5])
        more = "…" if len(self.affecting) > 5 else ""
        return (
            f"快照已由 {self.old_snapshot_id} 变更为 {self.new_snapshot_id}，"
            f"其中 {len(self.affecting)} 处触及本方案（{touched}{more}）"
        )

    def as_details(self) -> dict[str, Any]:
        return {
            "old_snapshot_id": self.old_snapshot_id,
            "new_snapshot_id": self.new_snapshot_id,
            "affecting": [
                {
                    "entity_type": c.entity_type,
                    "entity_id": c.entity_id,
                    "kind": c.kind,
                    "changed_fields": list(c.changed_fields),
                }
                for c in self.affecting
            ],
            "unrelated_count": len(self.unrelated),
        }


def check_staleness(
    session: Session,
    *,
    plan: SchedulePlan,
    snapshot_id: str,
    current_snapshot_id: str | None = None,
) -> StalenessVerdict:
    """比对方案所依据的快照与当前 ACTIVE 快照。"""
    current = current_snapshot_id or active_snapshot_id(session) or snapshot_id
    if current == snapshot_id:
        return StalenessVerdict(old_snapshot_id=snapshot_id, new_snapshot_id=current, changed=False)

    before = load_snapshot_normalized(session, snapshot_id)
    after = load_snapshot_normalized(session, current)
    entities = plan_entity_ids(plan)
    week = {plan.week_start + _days(i) for i in range(7)}

    affecting: list[Change] = []
    unrelated: list[Change] = []
    for change in diff_normalized(before, after):
        if _touches(change, entities=entities, week=week):
            affecting.append(change)
        else:
            unrelated.append(change)
    return StalenessVerdict(
        old_snapshot_id=snapshot_id,
        new_snapshot_id=current,
        changed=True,
        affecting=tuple(affecting),
        unrelated=tuple(unrelated),
    )


def _days(n: int) -> Any:
    from datetime import timedelta

    return timedelta(days=n)


def _touches(change: Change, *, entities: Mapping[str, frozenset[str]], week: set[date]) -> bool:
    # 规则条文变了就是判定变了，与具体架次无关 —— 一律触及
    if change.entity_type not in _PLAN_ENTITIES:
        return True
    if change.entity_id not in entities.get(change.entity_type, frozenset()):
        return False
    dates = change_dates(change)
    if not dates:
        # 没有日期语义的变更（改了机型、改了资质、改了空域容量…）一律触及：
        # 它们对整周都成立
        return True
    return bool(dates & week)


def resume_guard(
    state: FTSState,
    session: Session,
    *,
    current_snapshot_id: str | None = None,
) -> Command[str]:
    """确定性节点 ④。**纯比对逻辑，没有推理空间**（v6 §7.2.4）。"""
    plan = model_get(state, "solution", SchedulePlan)
    snapshot_id = state_get(state, "snapshot_id", "")
    if plan is None or not snapshot_id:
        # 没有方案就没有「基于过期数据批准」这回事，直接放行到人工门禁
        return Command(goto="human_gate", update={"needs_human": True})

    verdict = check_staleness(
        session,
        plan=plan,
        snapshot_id=snapshot_id,
        current_snapshot_id=current_snapshot_id,
    )
    events = emit(
        state,
        "resume_guard",
        "decision",
        {
            "changed": verdict.changed,
            "affects_plan": verdict.affects_plan,
            "summary": verdict.summary(),
        },
    )

    if verdict.affects_plan:
        return Command(
            goto="planner",
            update={
                "snapshot_id": verdict.new_snapshot_id,
                "solution": None,
                "validation": None,
                "needs_human": False,
                "trace_events": events,
                "errors": error(
                    ErrorCode.SNAPSHOT_STALE_ON_RESUME,
                    f"数据快照已变更且影响本方案，拒绝直接批准，将基于新快照重解。"
                    f"{verdict.summary()}",
                    severity="ERROR",
                    stage="constraint",
                    details=verdict.as_details(),
                    suggestions=["查看前后差异后重新排班", "如需保留原方案请先冻结快照"],
                    retryable=True,
                ),
            },
        )

    if verdict.changed:
        # 放行，但留痕：Sheet 4 区块 1 会写「快照已变更，变更与本方案无关」
        return Command(
            goto="human_gate",
            update={
                "needs_human": True,
                "trace_events": events,
                "errors": error(
                    ErrorCode.SNAPSHOT_STALE_ON_RESUME,
                    verdict.summary(),
                    severity="INFO",
                    stage="constraint",
                    details=verdict.as_details(),
                    suggestions=["本方案可继续审批"],
                    retryable=False,
                ),
            },
        )

    return Command(goto="human_gate", update={"needs_human": True, "trace_events": events})


__all__ = [
    "StalenessVerdict",
    "change_dates",
    "check_staleness",
    "plan_entity_ids",
    "resume_guard",
]
