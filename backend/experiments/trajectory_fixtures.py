"""`trajectory_100` 的可执行前置夹具。

数据集的 ``setup`` 是实验输入，不是说明文字。本模块把其中可复现的世界状态
编译成真实 ``ScenarioOverrides``、既有方案与人工门禁动作；实体集合始终从当前
快照读取，基准编号只出现在 approved 数据点名的回归用例里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.entities import (
    Aircraft,
    Airspace,
    Person,
    PersonAircraftType,
    RunwayAircraftType,
)
from backend.nodes.compile_spec import compile_spec
from backend.schemas.intent import IncrementalConstraint, UserRole
from backend.solver.data import NO_OVERRIDES, ScenarioOverrides
from backend.solver.diagnose import ProbeBudget
from backend.solver.solve import solve


@dataclass(frozen=True)
class GateAction:
    decision: str
    comment: str = ""


@dataclass
class GraphFixture:
    overrides: ScenarioOverrides = NO_OVERRIDES
    entry_node: str = "route"
    initial_updates: dict[str, Any] = field(default_factory=dict)
    gate_actions: tuple[GateAction, ...] = ()
    stop_at_interrupt: bool = False
    disable_harness: bool = False
    probe_calls_used: int = 0
    force_validation_retry: bool = False
    user_role: UserRole = "director"


@dataclass(frozen=True)
class SnapshotEntities:
    instructors: tuple[str, ...]
    student_types: tuple[str, ...]
    student_fleet: tuple[str, ...]
    all_fleet: tuple[str, ...]
    student_runways: tuple[str, ...]
    airspaces: tuple[str, ...]


def _entities(session: Session, snapshot_id: str) -> SnapshotEntities:
    instructors = tuple(
        session.scalars(
            select(Person.person_id)
            .where(Person.snapshot_id == snapshot_id, Person.identity == "教员")
            .order_by(Person.person_id)
        ).all()
    )
    student_types = tuple(
        session.scalars(
            select(PersonAircraftType.aircraft_type)
            .join(
                Person,
                (Person.person_id == PersonAircraftType.person_id)
                & (Person.snapshot_id == PersonAircraftType.snapshot_id),
            )
            .where(Person.snapshot_id == snapshot_id, Person.identity == "学员")
            .distinct()
            .order_by(PersonAircraftType.aircraft_type)
        ).all()
    )
    all_fleet = tuple(
        session.scalars(
            select(Aircraft.aircraft_id)
            .where(Aircraft.snapshot_id == snapshot_id)
            .order_by(Aircraft.aircraft_id)
        ).all()
    )
    student_fleet = tuple(
        session.scalars(
            select(Aircraft.aircraft_id)
            .where(
                Aircraft.snapshot_id == snapshot_id,
                Aircraft.aircraft_type.in_(student_types),
            )
            .order_by(Aircraft.aircraft_id)
        ).all()
    )
    student_runways = tuple(
        session.scalars(
            select(RunwayAircraftType.runway_id)
            .where(
                RunwayAircraftType.snapshot_id == snapshot_id,
                RunwayAircraftType.aircraft_type.in_(student_types),
            )
            .distinct()
            .order_by(RunwayAircraftType.runway_id)
        ).all()
    )
    airspaces = tuple(
        session.scalars(
            select(Airspace.airspace_id)
            .where(Airspace.snapshot_id == snapshot_id)
            .order_by(Airspace.airspace_id)
        ).all()
    )
    return SnapshotEntities(
        instructors=instructors,
        student_types=student_types,
        student_fleet=student_fleet,
        all_fleet=all_fleet,
        student_runways=student_runways,
        airspaces=airspaces,
    )


def _target_week(item: dict[str, Any]) -> date:
    for step in item.get("steps") or []:
        iso_week = (step.get("params") or {}).get("iso_week")
        if isinstance(iso_week, str) and len(iso_week) == 7:
            return date.fromisocalendar(int(iso_week[:4]), int(iso_week[-2:]), 1)
    utterance = str(item.get("utterance", ""))
    if "下周" in utterance:
        return date(2026, 1, 12)
    return date(2026, 1, 5)


def _maintenance(ids: tuple[str, ...], week: date) -> tuple[tuple[str, date, date], ...]:
    end = date.fromordinal(week.toordinal() + 6)
    return tuple((aircraft_id, week, end) for aircraft_id in ids)


def _diagnosis_fixture(
    item_id: str,
    *,
    entities: SnapshotEntities,
    week: date,
) -> GraphFixture:
    i1 = ScenarioOverrides(unavailable_all_week=frozenset(entities.instructors))
    i2 = ScenarioOverrides(maintenance_all_day=_maintenance(entities.student_fleet, week))
    i5 = ScenarioOverrides(closed_runways=frozenset(entities.student_runways))
    mapping: dict[str, ScenarioOverrides] = {
        "TRJ-DIA-001": i1,
        "TRJ-DIA-002": i2,
        "TRJ-DIA-003": i5,
        "TRJ-DIA-004": ScenarioOverrides(airspace_capacity={"IFR": 0}),
        "TRJ-DIA-005": i1,
        "TRJ-DIA-006": ScenarioOverrides(
            window_start=time(8, 0),
            window_end=time(16, 0),
            unavailable_all_week=i1.unavailable_all_week,
        ),
        "TRJ-DIA-007": ScenarioOverrides(
            window_end=time(9, 0), unavailable_all_week=i1.unavailable_all_week
        ),
        "TRJ-DIA-008": i2,
        "TRJ-DIA-009": ScenarioOverrides(
            maintenance_all_day=_maintenance(entities.all_fleet, week)
        ),
        "TRJ-DIA-010": ScenarioOverrides(
            unavailable_all_week=frozenset(entities.instructors[:1]),
            maintenance_all_day=i2.maintenance_all_day,
        ),
        "TRJ-DIA-011": ScenarioOverrides(airspace_capacity={"IFR": 0}),
        "TRJ-DIA-012": ScenarioOverrides(airspace_capacity={"IFR": 0, "RT2": 0}),
        "TRJ-DIA-013": ScenarioOverrides(airspace_capacity=dict.fromkeys(entities.airspaces, 0)),
        "TRJ-DIA-014": ScenarioOverrides(airspace_capacity={"SAB": 0}),
        "TRJ-DIA-015": ScenarioOverrides(window_end=time(6, 30)),
        "TRJ-DIA-016": ScenarioOverrides(window_end=time(6, 25)),
        "TRJ-DIA-017": ScenarioOverrides(
            window_end=time(6, 20), maintenance_all_day=_maintenance(("AC73",), week)
        ),
        "TRJ-DIA-018": ScenarioOverrides(window_end=time(6, 5)),
        "TRJ-DIA-019": i5,
        "TRJ-DIA-020": ScenarioOverrides(window_end=time(9, 0), closed_runways=i5.closed_runways),
        "TRJ-DIA-021": ScenarioOverrides(
            unavailable_all_week=frozenset(entities.instructors[:1]),
            closed_runways=i5.closed_runways,
        ),
        "TRJ-DIA-022": ScenarioOverrides(
            closed_runways=i5.closed_runways,
            maintenance_all_day=_maintenance(entities.student_fleet[:1], week),
        ),
        "TRJ-DIA-023": i1,
        "TRJ-DIA-024": ScenarioOverrides(
            maintenance_all_day=_maintenance(entities.all_fleet, week)
        ),
        "TRJ-DIA-025": ScenarioOverrides(airspace_capacity={"IFR": 0}),
    }
    return GraphFixture(
        overrides=mapping[item_id],
        stop_at_interrupt=True,
        disable_harness=item_id == "TRJ-DIA-025",
        # 确定性 diagnose() 会在 Agent 循环前消耗底座 probe。DIA-023 需要给它
        # 留出一次预算，随后 Agent 才能观察并复现“先 probe、再 rank”的收口路径。
        probe_calls_used=2 if item_id == "TRJ-DIA-023" else (5 if item_id == "TRJ-DIA-024" else 0),
    )


def baseline_updates(
    session: Session,
    *,
    snapshot_id: str,
    week_start: date,
) -> dict[str, Any]:
    """构造一版真实可行方案，供重排与修订夹具作为既有方案。"""
    bundle = compile_spec(
        session,
        snapshot_id=snapshot_id,
        week_start=week_start,
        materialize=False,
    )
    outcome = solve(bundle)
    if outcome.plan is None or outcome.status not in {"OPTIMAL", "FEASIBLE"}:
        raise RuntimeError(f"夹具基线未得到方案：{week_start} status={outcome.status}")
    return {
        "constraint_spec": bundle.spec,
        "solution": outcome.plan,
        "solver_stats": outcome.stats,
        "blocked_items": list(outcome.blocked_items),
        "solve_attempts": 1,
    }


def graph_fixture(
    item: dict[str, Any],
    *,
    session: Session,
    snapshot_id: str,
    baseline_cache: dict[date, dict[str, Any]],
    recorded_baseline: dict[str, Any] | None = None,
) -> GraphFixture:
    item_id = str(item["item_id"])
    flow = str(item["flow"])
    week = _target_week(item)
    if flow == "diagnosis":
        return _diagnosis_fixture(item_id, entities=_entities(session, snapshot_id), week=week)
    if flow == "schedule":
        return GraphFixture(
            gate_actions=(GateAction("REJECT" if item_id == "TRJ-SCH-015" else "APPROVE"),),
            force_validation_retry=item_id == "TRJ-SCH-014",
        )
    if flow in {"reschedule", "revision"}:
        # replay 必须从录制的 fixture checkpoint 恢复既有方案。先重新求一次基线
        # 既浪费时间，也会把“零求解重放”的边界悄悄打穿；record 路径才构造真实
        # 基线并写入 checkpoint。
        if recorded_baseline is not None:
            updates = dict(recorded_baseline)
        elif week not in baseline_cache:
            baseline_cache[week] = baseline_updates(
                session, snapshot_id=snapshot_id, week_start=week
            )
            updates = dict(baseline_cache[week])
        else:
            updates = dict(baseline_cache[week])
        if flow == "reschedule":
            return GraphFixture(
                initial_updates=updates,
                gate_actions=(GateAction("APPROVE"),),
                # IFR Route 整周关闭是该条夹具的真实扰动；若只依赖模型把
                # 自然语言翻成 FORBID ALL，会把“空域容量为 0”的业务事实
                # 与 Planner 语义错误混在一起。
                overrides=(
                    ScenarioOverrides(airspace_capacity={"IFR": 0})
                    if item_id == "TRJ-RSC-005"
                    else NO_OVERRIDES
                ),
            )
        fixture = GraphFixture(entry_node="human_gate", initial_updates=updates)
        if item_id == "TRJ-REV-010":
            fixture.gate_actions = (GateAction("REJECT"),)
            return fixture
        if item_id == "TRJ-REV-002":
            previous = IncrementalConstraint(
                kind="SHIFT_WINDOW",
                targets=["ALL"],
                params={"latest_minute": 180},
                origin_utterance="早点飞",
                round_no=1,
            )
            fixture.initial_updates["revision_stack"] = [previous]
            spec = fixture.initial_updates["constraint_spec"]
            fixture.initial_updates["constraint_spec"] = spec.model_copy(
                update={"incremental_constraints": [previous]}
            )
        first = GateAction("REVISE", str(item["utterance"]))
        if item_id == "TRJ-REV-009":
            fixture.gate_actions = (first,)
            fixture.stop_at_interrupt = True
            fixture.user_role = "scheduler"
        else:
            fixture.gate_actions = (first, GateAction("APPROVE"), GateAction("APPROVE"))
        return fixture
    return GraphFixture()


def probe_budget(fixture: GraphFixture, cfg: Any) -> ProbeBudget | None:
    if not fixture.probe_calls_used:
        return None
    return ProbeBudget(
        per_call_s=float(cfg.PROBE_TIME_LIMIT_S),
        max_calls=int(cfg.PROBE_MAX_CALLS),
        total_s=float(cfg.PROBE_TOTAL_BUDGET_S),
        calls=fixture.probe_calls_used,
        spent_s=float(fixture.probe_calls_used),
    )


__all__ = [
    "GateAction",
    "GraphFixture",
    "baseline_updates",
    "graph_fixture",
    "probe_budget",
]
