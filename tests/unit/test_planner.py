"""Planner 四件事：`SolveIntent` 生成、影响面、授权、修订翻译（v6 §7.3）。"""

from __future__ import annotations

import json
from datetime import time

import pytest

from backend.core.config import Settings
from backend.planner import (
    RELAX_TIER_AUTHORITY,
    ROLE_RANK,
    RevisionStack,
    apply_scope_policy,
    assess_disruption,
    authorized_tiers,
    check_authority,
    check_runway_feasibility,
    deterministic_intent,
    downgrade_freeze,
    echo_text,
    estimate_scope,
    few_shot_block,
    plan_solve_intent,
    required_role_for,
    rule_translate,
    translate_revision,
)
from backend.planner.authority import normalize_role
from backend.planner.revision import FEW_SHOT, REVISION_KINDS, for_solver, to_solver_params
from backend.schemas.intent import (
    ConstraintSpec,
    IncrementalConstraint,
    ObjectiveWeights,
    SchedulingRequest,
    SolveIntent,
)
from tests.fixtures.graph_fixtures import (
    BASELINE_RUNWAYS,
    BASELINE_WEEK,
    FakeHarness,
    degraded_output,
    directory,
    plan,
    sortie,
    text_output,
    tool_output,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def intent(**kwargs: object) -> SolveIntent:
    base: dict[str, object] = {
        "scope_persons": "ALL",
        "scope_missions": "ALL",
        "freeze_policy": "BALANCED",
        "freeze_reason": "测试",
        "objective_weights": ObjectiveWeights(progress=1.0, disruption=1.0, balance=1.0),
        "pre_authorized_tiers": [0],
        "incremental_constraints": [],
        "estimated_blast_radius": 0,
        "open_questions": [],
    }
    base.update(kwargs)
    return SolveIntent.model_validate(base)


def spec(**kwargs: object) -> ConstraintSpec:
    base: dict[str, object] = {
        "snapshot_id": "snap_test",
        "ruleset_version": "rs_1.3",
        "semantics_version": "sem_1.1",
        "iso_week": "2026W02",
        "week_start": BASELINE_WEEK,
        "week_end": BASELINE_WEEK.fromordinal(BASELINE_WEEK.toordinal() + 6),
        "scope_persons": "ALL",
        "scope_missions": "ALL",
        "relaxation_tier": 0,
        "objective_weights": ObjectiveWeights(progress=1.0, disruption=1.0, balance=1.0),
        "runway_model": "dual_runway",
        "runways": dict(BASELINE_RUNWAYS),
    }
    base.update(kwargs)
    return ConstraintSpec.model_validate(base)


# ─────────────────────────────────────────────────────────────────────
# 授权（v6 §3.10）
# ─────────────────────────────────────────────────────────────────────
def test_role_rank_is_ordered() -> None:
    assert ROLE_RANK["viewer"] < ROLE_RANK["scheduler"] < ROLE_RANK["director"] < ROLE_RANK["admin"]


@pytest.mark.parametrize(
    ("tier", "role"),
    [(0, "viewer"), (1, "scheduler"), (2, "scheduler"), (3, "director")],
)
def test_relax_tier_authority_matches_v6(tier: int, role: str) -> None:
    assert RELAX_TIER_AUTHORITY[tier] == role
    assert required_role_for(tier) == role


def test_scheduler_cannot_authorize_tier3() -> None:
    result = check_authority(3, "scheduler")
    assert not result.granted
    assert "训练主任" in result.reason


def test_director_can_authorize_tier3() -> None:
    assert check_authority(3, "训练主任").granted


def test_denied_tiers_leave_a_reason() -> None:
    kept, reasons = authorized_tiers([0, 1, 3], "scheduler")
    assert kept == [0, 1]
    assert len(reasons) == 1 and "Tier3" in reasons[0]


def test_unknown_role_raises_instead_of_defaulting() -> None:
    with pytest.raises(ValueError, match="未知角色"):
        normalize_role("飞行员")


def test_out_of_range_tier_raises() -> None:
    with pytest.raises(ValueError, match="0~3"):
        required_role_for(4)


# ─────────────────────────────────────────────────────────────────────
# 影响面（v6 §7.3.3 ①）
# ─────────────────────────────────────────────────────────────────────
def test_first_round_has_zero_blast_radius() -> None:
    """首轮排班没有「扰动」这个概念。"""
    assert estimate_scope(intent(freeze_policy="AGGRESSIVE"), None) == 0


def test_aggressive_touches_the_whole_week() -> None:
    p = plan([sortie(f"S00000{i}", day=i, aircraft_id="AC10") for i in range(1, 5)])
    assert estimate_scope(intent(freeze_policy="AGGRESSIVE"), p) == 4


def test_conservative_only_touches_named_persons() -> None:
    p = plan(
        [
            sortie("S000001", day=0, crew=(("P01", "教员"), ("P06", "学员"))),
            sortie("S000002", day=3, aircraft_id="AC27", crew=(("P05", "单飞"),)),
        ]
    )
    narrow = intent(freeze_policy="CONSERVATIVE", scope_persons=["P05"])
    assert estimate_scope(narrow, p) == 1


def test_balanced_pulls_in_same_day_same_aircraft() -> None:
    p = plan(
        [
            sortie("S000001", day=0, aircraft_id="AC10", crew=(("P05", "单飞"),)),
            sortie(
                "S000002",
                day=0,
                takeoff=time(10, 0),
                aircraft_id="AC10",
                crew=(("P06", "单飞"),),
            ),
            sortie("S000003", day=4, aircraft_id="AC27", crew=(("P07", "单飞"),)),
        ]
    )
    balanced = intent(freeze_policy="BALANCED", scope_persons=["P05"])
    assert estimate_scope(balanced, p) == 2


def test_downgrade_freeze_records_the_reason_for_sheet4() -> None:
    downgraded = downgrade_freeze(intent(freeze_policy="AGGRESSIVE"), reason="影响面太大")
    assert downgraded.freeze_policy == "BALANCED"
    assert "影响面太大" in downgraded.freeze_reason
    assert "AGGRESSIVE → BALANCED" in downgraded.freeze_reason


def test_scope_policy_recomputes_radius_after_downgrade() -> None:
    p = plan([sortie(f"S00000{i}", day=i % 5, aircraft_id="AC10") for i in range(1, 6)])
    decision = apply_scope_policy(
        intent(freeze_policy="AGGRESSIVE", scope_persons=["P06"]), p, threshold=2
    )
    assert decision.verdict == "downgraded"
    assert decision.intent.freeze_policy == "BALANCED"
    # ★ 降档后重算：写进 Sheet 4 的 blast radius 必须是降档**之后**那个数
    assert decision.intent.estimated_blast_radius == decision.radius


def test_scope_policy_leaves_non_aggressive_alone() -> None:
    p = plan([sortie(f"S00000{i}", day=i % 5) for i in range(1, 6)])
    decision = apply_scope_policy(intent(freeze_policy="BALANCED"), p, threshold=1)
    assert decision.verdict == "ok"


def test_assess_disruption_counts_touched_not_totals() -> None:
    before = plan([sortie("S000001"), sortie("S000002", day=1, aircraft_id="AC27")])
    after = plan(
        [
            sortie("S000001", aircraft_id="AC49"),  # 改动
            sortie("S000003", day=2, aircraft_id="AC34"),  # 新增
        ]
    )
    report = assess_disruption(before, after)
    assert report.added == ("S000003",)
    assert report.removed == ("S000002",)
    assert report.changed == ("S000001",)
    assert report.touched == 3


def test_assess_disruption_without_baseline() -> None:
    report = assess_disruption(None, plan([sortie("S000001")]))
    assert report.touched == 0 and report.total_new == 1


# ─────────────────────────────────────────────────────────────────────
# SolveIntent 生成（v6 §7.3.3）
# ─────────────────────────────────────────────────────────────────────
def test_deterministic_intent_takes_scope_from_request() -> None:
    request = SchedulingRequest(kind="schedule", raw_text="给何超排班", persons=["P08"])
    result = deterministic_intent(request)
    assert result.scope_persons == ["P08"]
    assert result.scope_missions == "ALL"
    assert "未经 LLM" in result.freeze_reason


def test_plan_solve_intent_without_llm_is_complete(settings: Settings) -> None:
    """FTS-4001 降级路径：没有 LLM 也产出一个完整可用的 SolveIntent。"""
    request = SchedulingRequest(kind="schedule", raw_text="排班")
    decision = plan_solve_intent(request, user_role="scheduler", settings=settings)
    assert decision.intent.scope_persons == "ALL"
    assert decision.next_node == "compile_spec"
    assert decision.llm_calls == 0


def test_plan_solve_intent_consumes_propose_tool(settings: Settings) -> None:
    proposed = intent(scope_persons=["P05", "P06"], freeze_policy="CONSERVATIVE")
    harness = FakeHarness(
        responses=[
            tool_output(
                "planner",
                [("propose_solve_intent", {"intent": proposed.model_dump(mode="json")})],
            )
        ]
    )
    request = SchedulingRequest(kind="schedule", raw_text="只排罗磊和张勇")
    decision = plan_solve_intent(request, user_role="scheduler", harness=harness, settings=settings)
    assert decision.intent.scope_persons == ["P05", "P06"]
    assert decision.next_node == "compile_spec"


def test_ask_user_becomes_an_open_question_and_routes_back(settings: Settings) -> None:
    harness = FakeHarness(
        responses=[
            tool_output(
                "planner",
                [("ask_user", {"question": "要连带调整教员的架次吗？", "resolution": "answer"})],
            )
        ]
    )
    decision = plan_solve_intent(
        SchedulingRequest(kind="schedule", raw_text="排班"),
        user_role="scheduler",
        harness=harness,
        settings=settings,
    )
    assert decision.needs_clarification
    assert decision.next_node == "route"
    assert "要连带调整教员的架次吗？" in decision.open_questions


def test_unauthorized_tier_is_removed_and_explained(settings: Settings) -> None:
    proposed = intent(pre_authorized_tiers=[0, 3])
    harness = FakeHarness(
        responses=[
            tool_output(
                "planner",
                [("propose_solve_intent", {"intent": proposed.model_dump(mode="json")})],
            )
        ]
    )
    decision = plan_solve_intent(
        SchedulingRequest(kind="schedule", raw_text="排班"),
        user_role="scheduler",
        harness=harness,
        settings=settings,
    )
    assert decision.intent.pre_authorized_tiers == [0]
    assert any("Tier3" in q for q in decision.open_questions)
    assert decision.needs_clarification


def test_degraded_planner_falls_back_without_raising(settings: Settings) -> None:
    harness = FakeHarness(responses=[degraded_output("planner")])
    decision = plan_solve_intent(
        SchedulingRequest(kind="schedule", raw_text="排班"),
        user_role="scheduler",
        harness=harness,
        settings=settings,
    )
    assert decision.degraded
    assert decision.intent.scope_persons == "ALL"


# ─────────────────────────────────────────────────────────────────────
# 修订翻译（v6 §7.3.4）
# ─────────────────────────────────────────────────────────────────────
def test_few_shot_covers_all_six_kinds_and_the_negative_case() -> None:
    """业务方 2026-08-13 确认的六条示例，一条都不能少。"""
    assert len(FEW_SHOT) == 6
    assert FEW_SHOT[-1].utterance == "AC84 那班也走 2 号跑道"
    block = few_shot_block()
    for example in FEW_SHOT:
        assert example.utterance in block
    assert "RWY-2 只服务 JL-8" in block


def test_six_revision_kinds_are_exactly_v6s() -> None:
    assert set(REVISION_KINDS) == {
        "FORBID",
        "PIN_TIME",
        "PIN_RESOURCE",
        "SHIFT_WINDOW",
        "REDUCE_DENSITY",
        "PIN_RUNWAY",
    }


@pytest.mark.parametrize(
    ("utterance", "kind"),
    [
        ("刘斌周五别排了", "FORBID"),
        ("何超那个换成 AC49", "PIN_RESOURCE"),
        ("这几个都走 2 号跑道", "PIN_RUNWAY"),
        ("早点飞", "SHIFT_WINDOW"),
        ("周三上午太挤了，挪两个到下午", "REDUCE_DENSITY"),
        ("何超那班固定在 08:30", "PIN_TIME"),
    ],
)
def test_rule_translate_handles_the_canonical_phrasings(utterance: str, kind: str) -> None:
    result = rule_translate(utterance, round_no=1, directory=directory())
    assert result is not None
    assert result.kind == kind
    assert result.origin_utterance == utterance
    assert result.round_no == 1


def test_rule_translate_returns_none_instead_of_guessing() -> None:
    assert rule_translate("嗯，就这样吧", round_no=1, directory=directory()) is None


def test_rule_translate_resolves_names_to_ids() -> None:
    result = rule_translate("刘斌周五别排了", round_no=1, directory=directory())
    assert result is not None
    assert result.targets == ["P04"]
    assert result.params["day"] == "周五"


def test_translate_revision_raises_when_untranslatable() -> None:
    from backend.core.errors import FTSError

    with pytest.raises(FTSError, match="没能翻译成增量约束"):
        translate_revision("嗯", round_no=1, directory=directory())


def test_echo_text_is_the_confirmation_line() -> None:
    """v6 §7.3.4 第 4 条：回显确认，句式固定为「我理解为：……」。"""
    c = IncrementalConstraint(
        kind="REDUCE_DENSITY",
        targets=["周三"],
        params={"day": "周三", "window": "06:00-12:00", "delta": -2},
        origin_utterance="周三上午太挤了，挪两个到下午",
        round_no=1,
    )
    assert echo_text(c) == "我理解为：周三 06:00-12:00 减少 2 个起飞"


def test_echo_shows_names_not_just_ids() -> None:
    c = IncrementalConstraint(
        kind="PIN_RESOURCE",
        targets=["P08"],
        params={"aircraft": "AC49"},
        origin_utterance="何超那个换成 AC49",
        round_no=1,
    )
    assert "何超(P08)" in echo_text(c, directory=directory())


# ── PIN_RUNWAY × JL-9 的预检 ────────────────────────────────────────
def test_pin_runway_on_jl9_is_flagged_before_solving() -> None:
    p = plan([sortie("S000001", aircraft_id="AC84", crew=(("P04", "复训"),))])
    c = IncrementalConstraint(
        kind="PIN_RUNWAY",
        targets=["AC84"],
        params={"runway": "RWY-2"},
        origin_utterance="AC84 那班也走 2 号跑道",
        round_no=1,
    )
    warnings = check_runway_feasibility(c, plan=p, spec=spec(), directory=directory())
    assert warnings and "AC84" in warnings[0] and "JL-9" in warnings[0]
    assert "FTS-3005" in warnings[0]


def test_pin_runway_on_jl8_is_clean() -> None:
    p = plan([sortie("S000001", aircraft_id="AC10")])
    c = IncrementalConstraint(
        kind="PIN_RUNWAY",
        targets=["AC10"],
        params={"runway": "RWY-2"},
        origin_utterance="这个走 2 号跑道",
        round_no=1,
    )
    assert check_runway_feasibility(c, plan=p, spec=spec(), directory=directory()) == []


def test_unknown_runway_is_flagged() -> None:
    c = IncrementalConstraint(
        kind="PIN_RUNWAY",
        targets=["ALL"],
        params={"runway": "RWY-9"},
        origin_utterance="走 9 号跑道",
        round_no=1,
    )
    warnings = check_runway_feasibility(c, plan=None, spec=spec(), directory=directory())
    assert warnings and "不在本次规格的跑道表里" in warnings[0]


def test_prechecked_constraint_is_still_produced() -> None:
    """预检**不改变翻译结果** —— 约束照常产出，解释只是提前。"""
    p = plan([sortie("S000001", aircraft_id="AC84", crew=(("P04", "复训"),))])
    result = translate_revision(
        "AC84 那班也走 2 号跑道",
        round_no=1,
        plan=p,
        directory=directory(),
        spec=spec(),
    )
    assert result.constraint.kind == "PIN_RUNWAY"
    assert result.constraint.params["runway"] == "RWY-2"
    assert result.infeasible_hint


# ── 人话形状 → 求解器线格式 ──────────────────────────────────────────
def test_solver_params_translate_day_to_index() -> None:
    c = IncrementalConstraint(
        kind="FORBID",
        targets=["P04"],
        params={"day": "周五"},
        origin_utterance="刘斌周五别排了",
        round_no=1,
    )
    wire = to_solver_params(c, window_start=time(6, 0), horizon_minutes=720)
    assert wire == {"day_index": 4}


def test_solver_params_translate_clock_to_minutes() -> None:
    c = IncrementalConstraint(
        kind="SHIFT_WINDOW",
        targets=["ALL"],
        params={"latest": "09:00"},
        origin_utterance="早点飞",
        round_no=1,
    )
    wire = to_solver_params(c, window_start=time(6, 0), horizon_minutes=720)
    assert wire == {"latest_minute": 180}


def test_solver_params_use_the_key_names_the_model_layer_expects() -> None:
    """键名必须与 `solver/model.py::post_incremental` 一致 —— 否则静默失效。"""
    runway = IncrementalConstraint(
        kind="PIN_RUNWAY",
        targets=["ALL"],
        params={"runway": "RWY-2"},
        origin_utterance="都走 2 号跑道",
        round_no=1,
    )
    resource = IncrementalConstraint(
        kind="PIN_RESOURCE",
        targets=["P08"],
        params={"aircraft": "AC49"},
        origin_utterance="换 AC49",
        round_no=1,
    )
    assert "runway_id" in to_solver_params(runway, window_start=time(6, 0), horizon_minutes=720)
    assert "aircraft_id" in to_solver_params(resource, window_start=time(6, 0), horizon_minutes=720)


def test_reduce_density_converts_delta_to_absolute_cap() -> None:
    """求解器认的是**上限**，不是增量。"""
    p = plan(
        [
            sortie("S000001", day=2, takeoff=time(7, 0)),
            sortie("S000002", day=2, takeoff=time(9, 0), aircraft_id="AC27"),
            sortie("S000003", day=2, takeoff=time(11, 0), aircraft_id="AC34"),
        ]
    )
    c = IncrementalConstraint(
        kind="REDUCE_DENSITY",
        targets=["周三"],
        params={"day": "周三", "window": "06:00-12:00", "delta": -2},
        origin_utterance="周三上午太挤了，挪两个到下午",
        round_no=1,
    )
    wire = for_solver(c, window_start=time(6, 0), plan=p, horizon_minutes=720)
    assert wire.params == {"day_index": 2, "max_takeoffs_per_day": 1}
    # 原话与轮次原样保留 —— 撤销与审计靠它们
    assert wire.origin_utterance == c.origin_utterance
    assert wire.round_no == 1


# ── 修订栈 ───────────────────────────────────────────────────────────
def test_revision_stack_round_numbers_start_at_one() -> None:
    stack = RevisionStack()
    assert stack.round_no == 1


def test_revision_stack_undo_pops_last() -> None:
    stack = RevisionStack()
    first = IncrementalConstraint(
        kind="FORBID", targets=["P04"], origin_utterance="第一条", round_no=1
    )
    second = IncrementalConstraint(
        kind="FORBID", targets=["P05"], origin_utterance="第二条", round_no=2
    )
    stack.push(first).push(second)
    assert stack.round_no == 3
    assert stack.undo() is second
    assert stack.utterances() == ["第一条"]
    assert stack.undo() is first
    assert stack.undo() is None  # 空栈不是错误


def test_llm_translation_path(settings: Settings) -> None:
    payload = json.dumps(
        {"kind": "PIN_RESOURCE", "targets": ["何超"], "params": {"aircraft": "AC49"}}
    )
    harness = FakeHarness(responses=[text_output("planner", payload)])
    result = translate_revision(
        "把小何那趟换一架", round_no=2, harness=harness, directory=directory()
    )
    assert result.source == "llm"
    assert result.constraint.targets == ["P08"]
    assert result.constraint.round_no == 2


def test_llm_degradation_falls_back_to_rules(settings: Settings) -> None:
    harness = FakeHarness(responses=[degraded_output("planner")])
    result = translate_revision(
        "刘斌周五别排了", round_no=1, harness=harness, directory=directory()
    )
    assert result.source == "rule"
    assert result.constraint.kind == "FORBID"
    assert any("降级" in w for w in result.warnings)
