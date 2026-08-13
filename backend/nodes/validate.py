"""确定性节点 ③：`validate_node`（v6 §7.2.4 / §7.5）。

> 14 条独立校验 + 三层格式校验。**14 个纯函数的编排。**

## `validate → solve` 的回环触发即 CRITICAL

v6 §7.5 说得很清楚：

> 正常情况下**不应触发**（CP-SAT 已保证约束满足）。一旦触发即意味着建模与
> 校验对规则的理解出现分歧——系统将该事件标为 `CRITICAL`（FTS-3003），
> 因为它暴露的是规格理解 bug。按 `CLAUDE.md §7` 第 5 条，触发即停下来报告。
> 这是**自检机制**，不是常规路径。

所以本节点在回环时做三件事：标 `CRITICAL`、把双方判定详情写进 `details`、
把违规条款注入为 no-good cut。**不静默重试**——静默重试会让「求解器与校验器
对约束7 的理解差了 5 分钟」表现为「偶尔多跑一轮」，而那正是最该被人看见的信号。

## no-good cut 是「别再给我这一版」，不是「放宽约束」

回灌的增量约束把**本次违规的那些架次组合**禁掉（`FORBID` 到具体机组与飞机），
让求解器换一个解。它**不动任何硬约束**——放宽约束来让校验过，是把自检机制
变成掩盖机制。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Command
from sqlalchemy.orm import Session

from backend.core.config import Settings, get_settings
from backend.core.errors import ErrorCode
from backend.graph.events import emit, error
from backend.graph.state import FTSState, model_get
from backend.graph.state import get as state_get
from backend.schemas.intent import ConstraintSpec, IncrementalConstraint
from backend.schemas.plan import SchedulePlan
from backend.schemas.solver import SolverStats
from backend.schemas.validation import ValidationReport
from backend.validator import load_context, run_all_checks, verify_format


def inject_nogoods(
    spec: ConstraintSpec,
    report: ValidationReport,
    plan: SchedulePlan,
    *,
    round_no: int,
) -> ConstraintSpec:
    """把违规架次禁掉，作为下一轮的增量约束（v6 §7.5 的 `inject_nogoods`）。

    只禁**被点名的架次**，不禁整个人整周——后者会把一次自检变成一次误伤。
    点不到具体架次时（违规主体是人或飞机而非架次号）退而求其次禁该主体，
    并在 `origin_utterance` 里写清是自检回灌，便于事后区分「用户要求的」与
    「系统自己加的」。
    """
    subjects: list[str] = []
    sortie_ids = {s.sortie_id for s in plan.sorties}
    for violation in report.all_violations():
        for subject in violation.subjects:
            if subject in sortie_ids and subject not in subjects:
                subjects.append(subject)
    if not subjects:
        for violation in report.all_violations():
            for subject in violation.subjects:
                if subject not in subjects:
                    subjects.append(subject)
    if not subjects:
        return spec

    cut = IncrementalConstraint(
        kind="FORBID",
        targets=subjects,
        params={"source": "validator_nogood"},
        origin_utterance=(
            f"[系统自检回灌] 第 {round_no} 轮校验发现 "
            f"{len(report.all_violations())} 条违规，禁掉涉事对象重解"
        ),
        round_no=round_no,
    )
    return spec.model_copy(update={"incremental_constraints": [*spec.incremental_constraints, cut]})


def validate_node(
    state: FTSState,
    session: Session,
    *,
    settings: Settings | None = None,
) -> Command[str]:
    """确定性节点 ③。"""
    cfg = settings or get_settings()
    plan = model_get(state, "solution", SchedulePlan)
    spec = model_get(state, "constraint_spec", ConstraintSpec)
    if plan is None or spec is None:
        raise ValueError("validate_node 需要 solution 与 constraint_spec —— solve 没跑或没落黑板")

    # 校验器有**自己**的一份只读事实视图（v6 §4.2）：它从 PG 的事实表直接装配，
    # 不复用求解侧的数据装配。这是双通道校验的证据基础（铁律 2）。
    ctx = load_context(session, snapshot_id=spec.snapshot_id, week_start=spec.week_start)
    report = run_all_checks(plan, ctx)
    fmt = verify_format(plan, ctx)

    attempts = int(state_get(state, "solve_attempts", 0))
    stats = model_get(state, "solver_stats", SolverStats)
    base: dict[str, Any] = {
        "validation": report,
        "trace_events": emit(
            state,
            "validate",
            "constraint_check",
            {
                "all_passed": report.all_passed,
                "checked_items": report.total_checked_items,
                "violations": len(report.all_violations()),
                "missing_rules": report.missing_rules(),
                "format_passed": fmt.passed,
                "attempt": attempts,
            },
        ),
    }

    if report.all_passed and fmt.passed:
        return Command(goto="explain", update=base)

    # ── 这里之后全是自检路径，正常不该走到 ──────────────────────────
    detail: dict[str, Any] = {
        "rule_results": [
            {"rule_id": r.rule_id, "passed": r.passed, "violations": len(r.violations)}
            for r in report.results
            if not r.passed
        ],
        "format_errors": {
            "schema": list(fmt.schema_errors),
            "referential": list(fmt.integrity_errors),
            "cross_table": list(fmt.cross_table_errors),
        },
        "solver_status": stats.status if stats is not None else "",
        "attempt": attempts,
    }
    critical = error(
        ErrorCode.VALIDATOR_SOLVER_DISAGREE,
        "校验器判定与求解器不一致：求解器认为该方案满足全部硬约束，"
        f"独立校验器查出 {len(report.all_violations())} 条违规。"
        "这暴露的是**规格理解分歧**，不是数据问题",
        severity="CRITICAL",
        stage="validate",
        details=detail,
        suggestions=[
            "按 CLAUDE.md §7 第 5 条立刻停下来报告，先定位到具体条款",
            "不许通过放宽约束让校验通过",
        ],
    )

    if attempts >= cfg.MAX_SOLVE_ATTEMPTS:
        return Command(goto="diagnosis", update={**base, "errors": critical})

    return Command(
        goto="solve",
        update={
            **base,
            "errors": critical,
            "constraint_spec": inject_nogoods(spec, report, plan, round_no=attempts + 1),
        },
    )


__all__ = ["inject_nogoods", "validate_node"]
