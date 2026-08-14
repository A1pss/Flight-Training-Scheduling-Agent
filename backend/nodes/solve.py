"""确定性节点 ②：`solve_node`（v6 §7.2.4）。

> CP-SAT 求解、预算管理、warm start、局部重排。**纯函数；正确性绝不依赖概率模型。**

**不经 Harness、不读 Skill、不注册为任何 LLM 组件的工具**（铁律 4）。
`probe_solve` 是确定性边界的唯一例外，它在 `agents/diagnosis.py` 里，不在这。

## 三态在这里就分开了，一路分到底

| 求解状态 | 下一跳 | 错误码 | 语义 |
|---|---|---|---|
| `OPTIMAL` / `FEASIBLE` | `validate` | — | 有方案，交给独立校验器 |
| `INFEASIBLE` | `diagnosis` | FTS-3001 | 数学上不可满足，**加时间也没用** |
| `UNKNOWN` | `human_gate` | FTS-3002 | **没算完**，不是没解；带回当前可行解（若有）并提供延长时限 |
| `MODEL_INVALID` | `human_gate` | FTS-3003 | 建模本身有问题，CRITICAL |

`UNKNOWN` 与 `INFEASIBLE` 分开是铁律 8。混起来的后果很具体：排班员看到
「不可行」会去砍需求，而实际上只要多给 30 秒就有解。
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Command
from sqlalchemy.orm import Session

from backend.core.errors import ErrorCode
from backend.graph.events import emit, error
from backend.graph.state import FTSState, model_get
from backend.graph.state import get as state_get
from backend.nodes.compile_spec import bundle_from_spec
from backend.schemas.intent import ConstraintSpec
from backend.schemas.plan import SchedulePlan
from backend.solver.data import NO_OVERRIDES, ScenarioOverrides
from backend.solver.model import RelaxationSettings
from backend.solver.solve import SolveOutcome, solve

#: 有方案的两种状态。
SOLVED: tuple[str, ...] = ("OPTIMAL", "FEASIBLE")


def run_solve(
    session: Session,
    spec: ConstraintSpec,
    *,
    prev_plan: SchedulePlan | None = None,
    overrides: ScenarioOverrides = NO_OVERRIDES,
    capture_log: bool = False,
) -> SolveOutcome:
    """按黑板上的规格跑一次求解。

    `prev_plan` 非空时开 warm start 并把最小扰动目标接上（v6 §3.8）——
    多轮修订的第 2 轮起就走这条路，否则每轮都会重新洗牌，用户会看到
    「我只说了挪两个，怎么整周都变了」。
    """
    bundle = bundle_from_spec(session, spec, overrides=overrides)
    return solve(
        bundle,
        relaxation=RelaxationSettings(tier=spec.relaxation_tier),
        prev_plan=prev_plan,
        warm_start=prev_plan is not None,
        capture_log=capture_log,
    )


def solve_node(
    state: FTSState,
    session: Session,
    *,
    overrides: ScenarioOverrides = NO_OVERRIDES,
    capture_log: bool = False,
) -> Command[str]:
    """确定性节点 ②。"""
    spec = model_get(state, "constraint_spec", ConstraintSpec)
    if spec is None:
        raise ValueError("solve_node 需要 constraint_spec —— compile_spec 没跑或没落黑板")

    prev_plan = model_get(state, "solution", SchedulePlan)
    attempts = int(state_get(state, "solve_attempts", 0)) + 1
    outcome = run_solve(
        session, spec, prev_plan=prev_plan, overrides=overrides, capture_log=capture_log
    )
    stats = outcome.stats

    base: dict[str, Any] = {
        "solver_stats": stats,
        "solve_attempts": attempts,
        "blocked_items": list(outcome.blocked_items),
        "trace_events": emit(
            state,
            "solve",
            "solver_stats",
            {
                "status": stats.status,
                "sorties": len(outcome.plan.sorties) if outcome.plan else 0,
                "candidates": stats.num_candidates,
                "variables": stats.num_variables,
                "constraints": stats.num_constraints,
                "wall_time_ms": stats.wall_time_ms,
                "relaxation_tier": stats.relaxation_tier,
                "attempt": attempts,
            },
        ),
    }

    if stats.status in SOLVED and outcome.plan is not None:
        return Command(goto="validate", update={**base, "solution": outcome.plan})

    if stats.status == "INFEASIBLE":
        return Command(
            goto="diagnosis",
            update={
                **base,
                "solution": None,
                "errors": error(
                    ErrorCode.INFEASIBLE,
                    f"{spec.iso_week} 在 Tier{spec.relaxation_tier} 下不可满足全部硬约束",
                    severity="ERROR",
                    stage="solve",
                    details={"iso_week": spec.iso_week, "relaxation_tier": spec.relaxation_tier},
                    suggestions=["查看最小冲突集与松弛提案；不可行与求解时长无关，加时间没用"],
                ),
            },
        )

    if stats.status == "UNKNOWN":
        # ⚠️ 铁律 8：UNKNOWN ≠ INFEASIBLE。带回当前可行解（若有）并标注非最优。
        return Command(
            goto="human_gate",
            update={
                **base,
                "solution": outcome.plan,
                "needs_human": True,
                "errors": error(
                    ErrorCode.SOLVE_TIMEOUT_UNKNOWN,
                    f"{spec.iso_week} 未在 {spec.solver_time_limit_s:.0f}s 内完成求解 —— "
                    "这是**没算完**，不是不可行",
                    severity="WARN",
                    stage="solve",
                    details={
                        "time_limit_s": spec.solver_time_limit_s,
                        "has_partial_solution": outcome.plan is not None,
                    },
                    suggestions=["延长求解时限后重试", "或缩小排班范围"],
                    retryable=True,
                ),
            },
        )

    return Command(
        goto="human_gate",
        update={
            **base,
            "solution": None,
            "needs_human": True,
            "errors": error(
                ErrorCode.VALIDATOR_SOLVER_DISAGREE,
                f"求解器返回 {stats.status} —— 模型本身非法，这是建模 bug 不是数据问题",
                severity="CRITICAL",
                stage="solve",
                details={"status": stats.status},
                suggestions=["按 CLAUDE.md §7 第 5 条停下来报告，不要重试"],
            ),
        },
    )


__all__ = ["SOLVED", "run_solve", "solve_node"]
