"""DiagnosisAgent 的必需工具阶段与确定性停止。"""

from types import SimpleNamespace
from typing import Any

import pytest

import backend.agents.diagnosis as diagnosis_agent
from backend.core.config import Settings
from backend.llm.mock import tool_response
from backend.schemas.solver import ProbeResult, RelaxationProposal
from backend.solver.diagnose import ConflictCore, Diagnosis, ProbeBudget
from tests.fixtures.graph_fixtures import FakeHarness, FakeRegistry, tool_output
from tests.fixtures.harness_fixtures import build_harness


def _proposal(*, verified: bool) -> RelaxationProposal:
    return RelaxationProposal(
        proposal_id="TIER1",
        tier=1,
        action="顺延一项频率要求",
        cost="形成一项欠账",
        affected_rules=["C13"],
        rule_tier="R2",
        authority="排班员",
        verified=verified,
        verified_result=(
            ProbeResult(status="OPTIMAL", sorties=1, wall_time_ms=1.0) if verified else None
        ),
        note=None if verified else "预算耗尽，未验证",
    )


def _base(*, verified: bool) -> Diagnosis:
    core = ConflictCore(
        status="INFEASIBLE",
        group_ids=("C13_frequency",),
        groups=(),
        wall_time_s=1.0,
        num_candidates=0,
    )
    return Diagnosis(
        status="INFEASIBLE",
        core=core,
        conflicts=(),
        proposals=(_proposal(verified=verified),),
        escalate=not verified,
        escalation_reason="" if verified else "没有已验证提案",
    )


def _patch_bottom(monkeypatch: Any, base: Diagnosis) -> None:
    def fake_diagnose(*_args: Any, **_kwargs: Any) -> Diagnosis:
        return base

    def fake_candidates(*_args: Any, **_kwargs: Any) -> object:
        return object()

    def fake_handlers(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {name: (lambda _arguments: {}) for name in diagnosis_agent.DIAGNOSIS_TOOLS}

    def fake_drafts(*_args: Any, **_kwargs: Any) -> tuple[()]:
        return ()

    def fake_verify(*_args: Any, **_kwargs: Any) -> tuple[RelaxationProposal, ...]:
        return base.proposals

    monkeypatch.setattr(diagnosis_agent, "diagnose", fake_diagnose)
    monkeypatch.setattr(diagnosis_agent, "enumerate_candidates", fake_candidates)
    monkeypatch.setattr(
        diagnosis_agent,
        "diagnosis_tool_handlers",
        fake_handlers,
    )
    monkeypatch.setattr(diagnosis_agent, "draft_proposals", fake_drafts)
    monkeypatch.setattr(diagnosis_agent, "verify_proposals", fake_verify)


def _bundle() -> Any:
    spec = SimpleNamespace(
        iso_week="2026W02",
        scope_persons="ALL",
        scope_missions=["missionA-1"],
    )
    return SimpleNamespace(data=object(), spec=spec, ruleset=object(), semantics=object())


def test_required_tools_progress_min_conflict_probe_then_rank(monkeypatch: Any) -> None:
    """模型不能在关键证据未取得时用空响应提前结束。"""
    base = _base(verified=True)
    _patch_bottom(monkeypatch, base)
    harness = FakeHarness(
        responses=[
            tool_output("diagnosis", [("min_conflict_set", {})], [{}]),
            tool_output("diagnosis", [("probe_solve", {"relaxations": ["TIER_1"]})], [{}]),
            tool_output("diagnosis", [("rank_relaxations", {"proposals": ["TIER1"]})], [{}]),
        ]
    )
    harness.registry = FakeRegistry()

    outcome = diagnosis_agent.run_diagnosis(
        _bundle(),
        harness=harness,  # type: ignore[arg-type]
        budget=ProbeBudget(per_call_s=30.0, max_calls=5, total_s=120.0),
        settings=Settings(_env_file=None, DIAGNOSIS_MAX_ROUNDS=3),
    )

    assert [call[0].required_tools for call in harness.calls] == [
        ("min_conflict_set",),
        ("probe_solve",),
        ("rank_relaxations",),
    ]
    assert all(call[0].tools == diagnosis_agent.DIAGNOSIS_TOOLS for call in harness.calls)
    assert outcome.rounds == 3
    assert outcome.autonomous is True
    assert set(harness.registry.handlers) == set(diagnosis_agent.DIAGNOSIS_TOOLS)
    assert "本轮必需工具: probe_solve" in harness.calls[1][1][0].content
    assert "目标 ISO 周: 2026W02" in harness.calls[0][1][0].content
    assert '"scope_persons": ["ALL"]' in harness.calls[0][1][0].content
    assert (
        "probe_solve.relaxations 合法档位: 3 项（TIER_1、TIER_2、TIER_3）"
        in harness.calls[0][1][0].content
    )
    assert "rank_relaxations.proposals 合法 ID: 1 项（TIER1）" in harness.calls[0][1][0].content


def test_exhausted_budget_stops_after_conflict_set_when_no_verified_proposal(
    monkeypatch: Any,
) -> None:
    """预算耗尽不能伪造 probe，也不应给未经验证的提案排序。"""
    base = _base(verified=False)
    _patch_bottom(monkeypatch, base)
    harness = FakeHarness(responses=[tool_output("diagnosis", [("min_conflict_set", {})], [{}])])
    harness.registry = FakeRegistry()

    outcome = diagnosis_agent.run_diagnosis(
        _bundle(),
        harness=harness,  # type: ignore[arg-type]
        budget=ProbeBudget(
            per_call_s=30.0,
            max_calls=5,
            total_s=120.0,
            calls=5,
            spent_s=5.0,
        ),
        settings=Settings(_env_file=None, DIAGNOSIS_MAX_ROUNDS=3),
    )

    assert [call[0].required_tools for call in harness.calls] == [("min_conflict_set",)]
    assert outcome.rounds == 1


def test_escalation_collects_two_probe_results_and_does_not_rank(monkeypatch: Any) -> None:
    """0 架次类资源死锁要验证两个松弛方向后升级，不能给空方案排序。"""
    base = _base(verified=False)
    _patch_bottom(monkeypatch, base)
    harness = FakeHarness(
        responses=[
            tool_output("diagnosis", [("min_conflict_set", {})], [{}]),
            tool_output("diagnosis", [("probe_solve", {"relaxations": ["TIER_1"]})], [{}]),
            tool_output("diagnosis", [("probe_solve", {"relaxations": ["TIER_2"]})], [{}]),
        ]
    )
    harness.registry = FakeRegistry()

    outcome = diagnosis_agent.run_diagnosis(
        _bundle(),
        harness=harness,  # type: ignore[arg-type]
        budget=ProbeBudget(per_call_s=30.0, max_calls=5, total_s=120.0),
        settings=Settings(_env_file=None, DIAGNOSIS_MAX_ROUNDS=3),
    )

    assert [call[0].required_tools for call in harness.calls] == [
        ("min_conflict_set",),
        ("probe_solve",),
        ("probe_solve",),
    ]
    assert all(call[0].tools == diagnosis_agent.DIAGNOSIS_TOOLS for call in harness.calls)
    assert all("rank_relaxations" not in call[0].required_tools for call in harness.calls)
    assert outcome.rounds == 3


def test_second_probe_result_is_visible_before_fourth_round_rank(monkeypatch: Any) -> None:
    """两次不同方向探针后必须还有一轮收口，且历史结果进入下一轮上下文。"""
    base = _base(verified=False)
    _patch_bottom(monkeypatch, base)
    harness = FakeHarness(
        responses=[
            tool_output("diagnosis", [("min_conflict_set", {})], [{}]),
            tool_output(
                "diagnosis",
                [("probe_solve", {"relaxations": ["TIER_1"]})],
                [{"status": "INFEASIBLE", "sorties": 0}],
            ),
            tool_output(
                "diagnosis",
                [("probe_solve", {"relaxations": ["TIER_2"]})],
                [{"status": "OPTIMAL", "sorties": 1}],
            ),
            tool_output("diagnosis", [("rank_relaxations", {"proposals": ["TIER1"]})], [{}]),
        ]
    )
    harness.registry = FakeRegistry()

    outcome = diagnosis_agent.run_diagnosis(
        _bundle(),
        harness=harness,  # type: ignore[arg-type]
        budget=ProbeBudget(per_call_s=30.0, max_calls=5, total_s=120.0),
        settings=Settings(_env_file=None, DIAGNOSIS_MAX_ROUNDS=4),
    )

    assert [call[0].required_tools for call in harness.calls] == [
        ("min_conflict_set",),
        ("probe_solve",),
        ("probe_solve",),
        ("rank_relaxations",),
    ]
    fourth_summary = harness.calls[3][1][0].content
    assert "TIER_1" in fourth_summary and "INFEASIBLE" in fourth_summary
    assert "TIER_2" in fourth_summary and "OPTIMAL" in fourth_summary
    assert outcome.rounds == 4


def test_presentable_probe_forces_rank_before_more_probe_rounds(monkeypatch: Any) -> None:
    """探针已经找到可排架次时，必须先排序收口，不能继续探测或静默停止。"""
    base = _base(verified=False)
    _patch_bottom(monkeypatch, base)
    harness = FakeHarness(
        responses=[
            tool_output("diagnosis", [("min_conflict_set", {})], [{}]),
            tool_output(
                "diagnosis",
                [("probe_solve", {"relaxations": ["TIER_1"]})],
                [{"status": "OPTIMAL", "sorties": 1}],
            ),
            tool_output("diagnosis", [("rank_relaxations", {"proposals": ["TIER1"]})], [{}]),
        ]
    )
    harness.registry = FakeRegistry()

    outcome = diagnosis_agent.run_diagnosis(
        _bundle(),
        harness=harness,  # type: ignore[arg-type]
        budget=ProbeBudget(per_call_s=30.0, max_calls=5, total_s=120.0),
        settings=Settings(_env_file=None, DIAGNOSIS_MAX_ROUNDS=3),
    )

    assert [call[0].required_tools for call in harness.calls] == [
        ("min_conflict_set",),
        ("probe_solve",),
        ("rank_relaxations",),
    ]
    assert outcome.escalate is True  # 底座提案仍未验证；不能把 rank 当成验证结果


def test_probe_rejects_unknown_relaxation_id() -> None:
    with pytest.raises(ValueError, match="未知松弛档位 ID"):
        diagnosis_agent._tier_from_relaxations(["C13_frequency"])

    assert diagnosis_agent._tier_from_relaxations(["TIER_2"]) == 2
    with pytest.raises(ValueError, match="只能指定一个"):
        diagnosis_agent._tier_from_relaxations(["TIER_1", "TIER_2"])


def test_rank_rejects_unknown_proposal_id() -> None:
    handlers = diagnosis_agent.diagnosis_tool_handlers(
        _bundle(),
        ProbeBudget(per_call_s=30.0, max_calls=5, total_s=120.0),
        core=_base(verified=True).core,
        cset=object(),  # type: ignore[arg-type]
        allowed_proposal_ids=("TIER1",),
    )

    with pytest.raises(ValueError, match="未知 ID"):
        handlers["rank_relaxations"]({"proposals": ["hallucinated"], "prefer": "least_debt"})


def test_harness_request_contains_exact_target_week() -> None:
    """目标周必须进入 Provider 实际收到的请求，而不只是留在 Python 状态里。"""
    harness, _, _ = build_harness(
        [
            tool_response(
                "min_conflict_set",
                {"iso_week": "2026W02", "scope_persons": ["ALL"]},
            )
        ]
    )
    agent = diagnosis_agent.DIAGNOSIS_AGENT.model_copy(
        update={"required_tools": ("min_conflict_set",)}
    )
    blocks = diagnosis_agent._blocks(
        _base(verified=True),
        ProbeBudget(per_call_s=30.0, max_calls=5, total_s=120.0),
        1,
        spec=_bundle().spec,
        required_tool="min_conflict_set",
    )

    out = harness.call(agent, blocks)
    request = harness.recorder.events[0].request  # type: ignore[union-attr]
    prompt = "\n".join(message["content"] for message in request.messages)

    assert out.degraded is False
    assert "目标 ISO 周: 2026W02" in prompt
    assert "iso_week 必须逐字复制" in prompt
    assert '"scope_persons": ["ALL"]' in prompt


def test_diagnosis_handlers_reject_a_week_outside_the_compiled_bundle() -> None:
    """错误周次不得拿当前 bundle 的结果冒充成功工具结果。"""
    bundle = _bundle()
    handlers = diagnosis_agent.diagnosis_tool_handlers(
        bundle,
        ProbeBudget(per_call_s=30.0, max_calls=5, total_s=120.0),
        core=_base(verified=True).core,
        cset=object(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="2026W02"):
        handlers["min_conflict_set"]({"iso_week": "2023W15", "scope_persons": ["ALL"]})
    with pytest.raises(ValueError, match="scope_persons"):
        handlers["min_conflict_set"]({"iso_week": "2026W02", "scope_persons": ["P08"]})
    with pytest.raises(ValueError, match="2026W02"):
        handlers["probe_solve"]({"iso_week": "2023W15", "relaxations": ["TIER_1"]})


def test_diagnosis_checkpoint_replays_agent_control_without_rerunning_base(
    monkeypatch: Any,
) -> None:
    """严格重放恢复底座，但仍真实执行三轮 Agent 控制流。"""
    base = _base(verified=True)
    _patch_bottom(monkeypatch, base)

    def harness() -> FakeHarness:
        value = FakeHarness(
            responses=[
                tool_output("diagnosis", [("min_conflict_set", {})], [{}]),
                tool_output("diagnosis", [("probe_solve", {"relaxations": ["TIER_1"]})], [{}]),
                tool_output("diagnosis", [("rank_relaxations", {"proposals": ["TIER1"]})], [{}]),
            ]
        )
        value.registry = FakeRegistry()
        return value

    checkpoints: dict[str, Any] = {}
    recorded = diagnosis_agent.run_diagnosis(
        _bundle(),
        harness=harness(),  # type: ignore[arg-type]
        budget=ProbeBudget(per_call_s=30.0, max_calls=5, total_s=120.0),
        settings=Settings(_env_file=None, DIAGNOSIS_MAX_ROUNDS=3),
        checkpoint_observer=lambda name, value: checkpoints.__setitem__(name, value),
    )

    monkeypatch.setattr(
        diagnosis_agent,
        "diagnose",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不得重跑底座")),
    )
    replayed = diagnosis_agent.run_diagnosis(
        _bundle(),
        harness=harness(),  # type: ignore[arg-type]
        budget=ProbeBudget(per_call_s=1.0, max_calls=1, total_s=1.0),
        settings=Settings(_env_file=None, DIAGNOSIS_MAX_ROUNDS=3),
        base_checkpoint=diagnosis_agent.DiagnosisBaseCheckpoint.model_validate(
            checkpoints["diagnosis_base"]
        ),
        outcome_checkpoint=diagnosis_agent.DiagnosisOutcomeCheckpoint.model_validate(
            checkpoints["diagnosis_outcome"]
        ),
    )

    assert replayed == recorded
