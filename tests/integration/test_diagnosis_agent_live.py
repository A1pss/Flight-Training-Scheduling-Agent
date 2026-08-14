"""`DiagnosisAgent` 受控自治的实测（v6 §7.2.2 / §3.9 / §7.1.5）。

构造一个真不可行的场景（**机队全部整周维护**，v6 §12.3 的 I2），跑完整诊断，
逐条验它的**边界**：

| 边界 | 断言 |
|---|---|
| 独立预算池（单次 30s / 5 次 / 累计 120s） | 探针次数不超上限，且与 Harness 的 LLM 预算互不挤占 |
| 每条提案必经 `probe_solve` 实证验证 | 呈现出来的提案要么 `verified=True`，要么带明确的 `note` |
| R0 恒不可松弛 | 提案里不出现 R0 规则 |
| 没有 LLM 也能诊断 | `harness=None` 时照常给冲突集与提案，`autonomous=False` 如实标着 |

**为什么用 I2 而不是随便造一个**：I2 是 v6 §12.3 已经标注过预期冲突源的场景，
「冲突集必须包含人工标注的真实冲突源」这条召回率要求在它身上有基准可比。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy.orm import Session

from backend.agents.diagnosis import DIAGNOSIS_TOOLS, run_diagnosis
from backend.core.config import Settings
from backend.core.db import get_session_factory, session_scope
from backend.nodes.compile_spec import compile_spec
from backend.solver.data import ScenarioOverrides
from backend.solver.diagnose import ProbeBudget
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot
from tests.fixtures.graph_fixtures import FakeHarness, FakeRegistry, degraded_output, tool_output

pytestmark = pytest.mark.integration

BASELINE_WEEK = date(2026, 1, 5)
WEEK_END = BASELINE_WEEK + timedelta(days=6)

#: v6 §1.3.2 的 8 架机。这里把它们**全部**整周送修 —— I2 的构造。
FLEET = ("AC10", "AC27", "AC34", "AC49", "AC61", "AC73", "AC84", "AC95")


@contextmanager
def shared_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(scope="module")
def snapshot() -> str:
    """一个 ACTIVE 快照。**库里没有就现建一份**（CLAUDE.md §6）。

    不写成 `assert 库里应当已经有` —— 那是在断言「有人在测试之外先跑过某个
    命令」，本地绿、CI 红。本文件按字母序排在 `test_ingestion_pipeline_live.py`
    **之前**，全新的 CI 库跑到这里时还没有任何快照。
    """
    with session_scope() as session:
        return ensure_baseline_snapshot(session)


@pytest.fixture(scope="module")
def settings() -> Settings:
    """诊断求解给 60s、探针给 5s —— 集成测试不该跑满 §3.11 的 300s。

    **这不是放宽判据**：I2 的不可行性是结构性的（候选集为空），秒级就能证完。
    """
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        SOLVER_DIAGNOSE_TIME_LIMIT_S=60.0,
        PROBE_TIME_LIMIT_S=5.0,
        PROBE_MAX_CALLS=3,
        PROBE_TOTAL_BUDGET_S=20.0,
        DIAGNOSIS_MAX_ROUNDS=2,
    )


@pytest.fixture(scope="module")
def outcome_without_llm(snapshot: str, settings: Settings) -> object:
    overrides = ScenarioOverrides(
        maintenance_all_day=tuple((ac, BASELINE_WEEK, WEEK_END) for ac in FLEET)
    )
    with shared_session() as session:
        bundle = compile_spec(
            session, snapshot_id=snapshot, week_start=BASELINE_WEEK, overrides=overrides
        )
        return run_diagnosis(bundle, budget=ProbeBudget.from_settings(), settings=settings)


def test_diagnosis_works_without_an_llm(outcome_without_llm) -> None:  # type: ignore[no-untyped-def]
    """`harness=None` 时诊断照常给结果，`autonomous=False` 如实标着。"""
    outcome = outcome_without_llm
    assert outcome.autonomous is False
    assert outcome.llm_calls == 0
    assert outcome.rounds == 0
    assert "未配置 Harness" in outcome.notes[0]
    assert outcome.conflicts, "机队全部整周维护竟然没有冲突集"


def test_conflict_set_names_the_expected_rules(outcome_without_llm) -> None:  # type: ignore[no-untyped-def]
    """v6 §12.3 的 I2 预期冲突源：约束3 与约束13 都该出现。

    极小 sat core 只会给出其中一个；另一个靠**结构性不可满足判定**补进来
    （`ConflictCore.structural_ids`）。这条正是那段补充逻辑的回归。
    """
    rule_ids = {rid for c in outcome_without_llm.conflicts for rid in c.rule_ids}
    assert {"C03", "C13"} <= rule_ids, f"冲突集漏了预期规则：{sorted(rule_ids)}"


def test_no_proposal_touches_r0(outcome_without_llm) -> None:  # type: ignore[no-untyped-def]
    """R0 安全刚性绝不可松弛 —— 契约层已堵死，这里再确认一次呈现面。"""
    assert all(p.rule_tier != "R0" for p in outcome_without_llm.proposals)


def test_every_presented_proposal_is_verified_or_explicitly_flagged(  # type: ignore[no-untyped-def]
    outcome_without_llm,
) -> None:
    """**未经探针验证的提案不得含糊呈现**（v6 §3.9.1）。"""
    for proposal in outcome_without_llm.proposals:
        if proposal.verified:
            assert proposal.verified_result is not None
        else:
            assert proposal.note, f"{proposal.proposal_id} 既没验证也没说明原因"


def test_probe_budget_is_respected(outcome_without_llm) -> None:  # type: ignore[no-untyped-def]
    """独立预算池：次数与秒数都不超（v6 §3.9.2）。"""
    budget = outcome_without_llm.probe_budget
    assert budget["calls"] <= budget["max_calls"]
    assert budget["spent_s"] <= budget["total_s"] + budget["per_call_s"]


def test_escalates_when_no_relaxation_actually_schedules_anything(  # type: ignore[no-untyped-def]
    outcome_without_llm,
) -> None:
    """「一个架次都不排」不算解决方案 —— 资源被抹平时应升级人工（§12.3 I2）。"""
    assert outcome_without_llm.escalate is True
    assert outcome_without_llm.escalation_reason


def test_autonomous_round_uses_only_the_four_tools(snapshot: str, settings: Settings) -> None:
    """自主探测那一轮暴露的工具，就是 v6 §7.2.2 给 Diagnosis 的那四个。"""
    overrides = ScenarioOverrides(
        maintenance_all_day=tuple((ac, BASELINE_WEEK, WEEK_END) for ac in FLEET)
    )
    harness = FakeHarness(
        responses=[
            tool_output("diagnosis", [("min_conflict_set", {"iso_week": "2026W02"})], [{}]),
            tool_output("diagnosis", [], []),  # 模型自己决定停 —— 这就是它的自治
        ]
    )
    harness.registry = FakeRegistry()
    with shared_session() as session:
        bundle = compile_spec(
            session, snapshot_id=snapshot, week_start=BASELINE_WEEK, overrides=overrides
        )
        outcome = run_diagnosis(
            bundle,
            harness=harness,  # type: ignore[arg-type]
            budget=ProbeBudget.from_settings(),
            settings=settings,
        )

    assert outcome.autonomous is True
    assert outcome.rounds >= 1
    assert harness.calls, "自主探测一次都没调模型"
    exposed = harness.calls[0][0].tools
    assert set(exposed) == set(DIAGNOSIS_TOOLS)
    assert set(harness.registry.handlers) == set(DIAGNOSIS_TOOLS)


def test_degraded_llm_falls_back_to_the_deterministic_result(
    snapshot: str, settings: Settings
) -> None:
    """LLM 降级不影响诊断能力，只是少了那层自主探测。"""
    overrides = ScenarioOverrides(
        maintenance_all_day=tuple((ac, BASELINE_WEEK, WEEK_END) for ac in FLEET)
    )
    harness = FakeHarness(responses=[degraded_output("diagnosis")])
    harness.registry = FakeRegistry()
    with shared_session() as session:
        bundle = compile_spec(
            session, snapshot_id=snapshot, week_start=BASELINE_WEEK, overrides=overrides
        )
        outcome = run_diagnosis(
            bundle,
            harness=harness,  # type: ignore[arg-type]
            budget=ProbeBudget.from_settings(),
            settings=settings,
        )
    assert outcome.autonomous is False
    assert outcome.conflicts
    assert any("降级" in note for note in outcome.notes)


def test_summary_says_which_mode_it_ran_in(outcome_without_llm) -> None:  # type: ignore[no-untyped-def]
    assert "确定性诊断（无 LLM）" in outcome_without_llm.summary()
