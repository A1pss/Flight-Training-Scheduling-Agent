"""M3 报告层的测试夹具：一份**不连库**的 `ReportBundle`。

实体与方案直接复用 `tests/fixtures/validator_facts.py` 的手工样本（14 架次合规样本
＋ v6 §1.4.2 的 7 条阻塞项），因此本文件不需要 PG、也不需要跑求解器。

`SolverStats` 是**构造出来的假统计**，只用于验证渲染与回读 —— 它不会出现在任何
实测指标里（铁律 6：真实数字一律来自 `tests/integration/test_report_baseline_live.py`
与 200 场景实跑）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.report.bundle import ApprovalInfo, ProvenanceInfo, RelaxationRecord, ReportBundle
from backend.schemas.plan import SchedulePlan, Sortie
from backend.schemas.solver import SolverStats
from backend.validator import run_all_checks, verify_format
from backend.validator.context import ValidationContext
from tests.fixtures.validator_facts import baseline_context, compliant_plan, debt, make_sortie

#: 固定时区，避免报告里的时间戳随机器时区漂（铁律 9）
CST = timezone(timedelta(hours=8))
GENERATED_AT = datetime(2026, 8, 13, 9, 0, 0, tzinfo=CST)

#: 刘斌（P04）的 S-11 复训架次：`is_recurrent=True` + 单人「复训」机组。
#: 合规样本里没有它 —— 基准周的复训窗口跨到下一周（v6 §1.2.4），所以要单独造一个
#: 来覆盖「刘斌训」/「(AC84/复训)」/区块7 复训标记这三处呈现。
RECURRENT_SORTIE_ID = "S000015"


def recurrent_sortie() -> Sortie:
    return make_sortie(
        RECURRENT_SORTIE_ID,
        3,  # 2026-01-08 = 到期次日
        "09:00",
        "missionC-1",
        "AC84",
        (("P04", "复训"),),
        runway_id="RWY-1",
        is_recurrent=True,
    )


def sample_plan(*, with_recurrent: bool = True) -> SchedulePlan:
    """14 架次合规样本（+ 可选的 1 个复训架次）。"""
    plan = compliant_plan()
    if not with_recurrent:
        return plan
    return plan.model_copy(update={"sorties": [*plan.sorties, recurrent_sortie()]})


def sample_stats(**overrides: object) -> SolverStats:
    payload: dict[str, object] = {
        "status": "OPTIMAL",
        "num_candidates": 2276,
        "num_variables": 12568,
        "num_constraints": 37235,
        "objective_value": 82782100.0,
        "best_bound": 82782100.0,
        "gap": 0.0,
        "wall_time_ms": 21000.0,
        "num_branches": 1234,
        "num_conflicts": 56,
        "random_seed": 42,
        "num_workers": 8,
        "relaxation_tier": 0,
    }
    payload.update(overrides)
    return SolverStats(**payload)  # type: ignore[arg-type]


def sample_bundle(
    *,
    plan: SchedulePlan | None = None,
    ctx: ValidationContext | None = None,
    with_recurrent: bool = True,
    **overrides: object,
) -> ReportBundle:
    """一份可直接渲染的 bundle。14 条校验用**真实校验器**跑出来，不是编的。"""
    context = ctx or baseline_context()
    schedule = plan if plan is not None else sample_plan(with_recurrent=with_recurrent)
    payload: dict[str, object] = {
        "plan": schedule,
        "ctx": context,
        "validation": run_all_checks(schedule, context),
        "stats": sample_stats(),
        "generated_at": GENERATED_AT,
        "format_report": verify_format(schedule, context),
        "plan_type": "WEEKLY",
        "plan_status": "DRAFT",
        "org": "NAU",
        "approval": ApprovalInfo(),
        "provenance": ProvenanceInfo(code_version="git:0000000", solver_version="9.11.4210"),
        "solver_log": "",
    }
    payload.update(overrides)
    return ReportBundle(**payload)  # type: ignore[arg-type]


def relaxed_bundle() -> ReportBundle:
    """带一条 Tier 1 欠账与松弛记录的 bundle（区块3 的「松弛档」列与区块6 的松弛行）。"""
    plan = sample_plan()
    relaxed = plan.model_copy(
        update={
            "relaxation_tier": 1,
            "debts": [debt("P07", "missionC-2", required=1, scheduled=0)],
        }
    )
    return sample_bundle(
        plan=relaxed,
        relaxations=[
            RelaxationRecord(
                tier=1,
                action="missionC-2 本周顺延，欠账记入下周",
                cost="进度延迟 1 周",
                authority="排班员",
            )
        ],
        conflict_summary="C13 × 陈伟(P07) missionC-2",
    )


__all__ = [
    "CST",
    "GENERATED_AT",
    "RECURRENT_SORTIE_ID",
    "recurrent_sortie",
    "relaxed_bundle",
    "sample_bundle",
    "sample_plan",
    "sample_stats",
]
