"""`trajectory_100` 图夹具编译回归。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from backend.datasets.loader import load_eval_dataset
from backend.experiments import trajectory_fixtures as fixtures
from backend.experiments.run_trajectory import (
    TODAY,
    stop_before_invalid_commit,
    tool_calls_from_trace_events,
)
from backend.routing.entities import resolve_week
from backend.schemas.validation import CheckResult, ValidationReport

ENTITIES = fixtures.SnapshotEntities(
    instructors=("P01", "P02", "P03"),
    student_types=("JL-8",),
    student_fleet=("AC10", "AC27", "AC34", "AC49", "AC61", "AC73"),
    all_fleet=("AC10", "AC27", "AC34", "AC49", "AC61", "AC73", "AC84", "AC95"),
    student_runways=("RWY-1", "RWY-2"),
    airspaces=("IFR", "RT2", "SAA", "SAB", "SAC", "SAD"),
)


class _Spec:
    def model_copy(self, *, update: dict[str, Any]) -> SimpleNamespace:
        return SimpleNamespace(**update)


def test_all_graph_fixture_setups_compile(monkeypatch: Any) -> None:
    monkeypatch.setattr(fixtures, "_entities", lambda _session, _snapshot: ENTITIES)
    monkeypatch.setattr(
        fixtures,
        "baseline_updates",
        lambda *_args, **_kwargs: {
            "constraint_spec": _Spec(),
            "solution": object(),
            "solver_stats": object(),
            "blocked_items": [],
            "solve_attempts": 1,
        },
    )
    _manifest, models = load_eval_dataset("trajectory_100", require_approved=True)
    rows = [m.model_dump() for m in models if m.flow not in {"query", "ingest"}]  # type: ignore[attr-defined]
    cache: dict[Any, dict[str, Any]] = {}

    compiled = [
        fixtures.graph_fixture(
            row,
            session=object(),  # type: ignore[arg-type]
            snapshot_id="snap_test",
            baseline_cache=cache,
        )
        for row in rows
    ]

    assert len(compiled) == 60
    assert all(f.overrides is not None for f in compiled)
    assert sum(f.stop_at_interrupt for f in compiled) == 26
    assert sum(f.force_validation_retry for f in compiled) == 1
    assert sum(f.disable_harness for f in compiled) == 1
    assert {f.entry_node for f in compiled} == {"route", "human_gate"}


def test_probe_budget_resumes_from_fixture_usage() -> None:
    cfg = SimpleNamespace(PROBE_TIME_LIMIT_S=5, PROBE_MAX_CALLS=5, PROBE_TOTAL_BUDGET_S=20)
    budget = fixtures.probe_budget(fixtures.GraphFixture(probe_calls_used=4), cfg)

    assert budget is not None
    assert budget.calls == 4
    assert budget.spent_s == 4.0
    assert fixtures.probe_budget(fixtures.GraphFixture(), cfg) is None


def test_runner_clock_matches_trajectory_week_labels() -> None:
    assert resolve_week("本周", today=TODAY).entity_id == "2026W02"
    assert resolve_week("下周", today=TODAY).entity_id == "2026W03"


def test_revision_009_fixture_runs_as_scheduler(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        fixtures,
        "baseline_updates",
        lambda *_args, **_kwargs: {
            "constraint_spec": _Spec(),
            "solution": object(),
            "solver_stats": object(),
            "blocked_items": [],
            "solve_attempts": 1,
        },
    )
    _manifest, models = load_eval_dataset("trajectory_100", require_approved=True)
    item = next(model.model_dump() for model in models if model.item_id == "TRJ-REV-009")  # type: ignore[attr-defined]

    fixture = fixtures.graph_fixture(
        item,
        session=object(),  # type: ignore[arg-type]
        snapshot_id="snap_test",
        baseline_cache={},
    )

    assert fixture.user_role == "scheduler"
    assert fixture.gate_actions[0].decision == "REVISE"
    assert fixture.stop_at_interrupt is True


def test_replay_fixture_uses_recorded_baseline_without_invoking_solver(monkeypatch: Any) -> None:
    """重放只消费 checkpoint，不能为构造夹具额外求解一次。"""
    _manifest, models = load_eval_dataset("trajectory_100", require_approved=True)
    item = next(model.model_dump() for model in models if model.item_id == "TRJ-RSC-001")  # type: ignore[attr-defined]
    baseline = {
        "constraint_spec": _Spec(),
        "solution": object(),
        "solver_stats": object(),
        "blocked_items": [],
        "solve_attempts": 1,
    }

    def fail_if_called(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("replay fixture 不应重新构造基线方案")

    monkeypatch.setattr(fixtures, "baseline_updates", fail_if_called)
    fixture = fixtures.graph_fixture(
        item,
        session=object(),  # type: ignore[arg-type]
        snapshot_id="snap_test",
        baseline_cache={},
        recorded_baseline=baseline,
    )

    assert fixture.initial_updates["solution"] is baseline["solution"]


def test_trace_tool_measurement_uses_actual_event_arguments() -> None:
    actual = {
        "actor_role": "排班员",
        "requested_tier": 3,
    }
    update = {
        "trace_events": [
            {
                "seq": 8,
                "agent": "planner",
                "kind": "tool_call",
                "payload": {
                    "tool": "check_authority",
                    "arguments": actual,
                    "result": {"granted": False},
                },
            },
            {
                "seq": 9,
                "agent": "planner",
                "kind": "negotiation",
                "payload": {"requested_tier": 999},
            },
        ]
    }

    calls = tool_calls_from_trace_events(update)

    assert calls == [("planner", "check_authority", actual)]
    actual["requested_tier"] = 1
    assert calls[0][2]["requested_tier"] == 3, "量具保存事件快照，不持有可变参数引用"


def test_approve_does_not_push_an_incomplete_gate_to_commit() -> None:
    item = {"flow": "schedule"}
    action = fixtures.GateAction("APPROVE")
    passed = ValidationReport(
        plan_id="plan-test",
        ruleset_version="1.3.0",
        semantics_version="1.0.0",
        results=[
            CheckResult(
                rule_id="C01",
                rule_title="test",
                passed=True,
                checked_items=1,
                duration_ms=0.0,
            )
        ],
    )
    assert stop_before_invalid_commit(item, action, {"needs_clarification": True})
    assert stop_before_invalid_commit(item, action, {"solution": None})
    assert stop_before_invalid_commit(
        item,
        action,
        {"solution": object(), "validation": passed, "needs_clarification": False},
    )
    assert not stop_before_invalid_commit(
        item,
        action,
        {
            "solution": object(),
            "validation": passed,
            "solver_stats": object(),
            "needs_clarification": False,
        },
    )
    assert not stop_before_invalid_commit(
        {"flow": "revision"},
        action,
        {"pending_revision": True},
    )
