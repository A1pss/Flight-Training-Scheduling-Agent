"""实验四 · §12.5.1 的聚合与三条推导核对。

**本模块不跑模型。** M7 已经把两种配置 × 两种口径 × 3 轮跑完并落盘
（`reports/m7/*.jsonl`，1980 行逐条结果），这里只读盘、算指标、做 §12.5.1
点名要的三条推导：

1. 用实测的 `p` 反推 `r`，代回两个公式核对是否自洽；
2. 调用级 → 请求级的复合换算（一次请求约 3 次工具调用）；
3. 硬地板 `x`（`entity_hallucination` 占全部调用的比例）与天花板 `(100−x)%`。

⚠️ **口径必须跟着数走**：同一份数据集，口径 A 一次通过率 99.50%、口径 B
66.83%。两个数不可混用、不可平均（`Z-36`）。所以每个返回值都带 `rendering`。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from backend.experiments.stats import wilson_interval
from backend.training.metrics import guardrail_metrics, valid_metrics
from backend.training.toolcall_eval import ToolCallOutcome

#: §12.5.1 的假设：一次排班请求约含 3 次工具调用。
CALLS_PER_REQUEST = 3


def load_outcomes(paths: Sequence[Path]) -> list[ToolCallOutcome]:
    """读回 M7 落盘的逐条结果。"""
    out: list[ToolCallOutcome] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(ToolCallOutcome.model_validate(json.loads(line)))
    return out


def solve_r(p: float, final: float) -> float | None:
    """由 `最终通过率 = 1 − (1−p)(1−r)²` 反推每轮纠正成功率 `r`。

    `p = 1` 时 `r` 无定义（没有失败可纠正）—— 返回 `None` 而不是 1.0，
    「无定义」和「纠正成功率 100%」是两件事，报告里不该混。
    """
    if p >= 1.0:
        return None
    inner = (1.0 - final) / (1.0 - p)
    if inner < 0:
        return None
    return float(1.0 - inner**0.5)


def predicted_retry_coefficient(p: float, r: float | None) -> float:
    """`平均重试系数 = 1 + (1−p) + (1−p)(1−r)`。"""
    if r is None:
        return 1.0 + (1.0 - p)
    return 1.0 + (1.0 - p) + (1.0 - p) * (1.0 - r)


def request_level_failure(call_level_failure: float, calls: int = CALLS_PER_REQUEST) -> float:
    """调用级失败率 → 请求级失败率：`1 − (1−f)^calls`。

    §12.5.1 特别强调这一步不能漏：只报调用级 3% 会让人以为每 100 个请求出
    3 个问题，**实际是 8.7 个**。
    """
    return 1.0 - (1.0 - call_level_failure) ** calls


def hard_floor(outcomes: Sequence[ToolCallOutcome], config: str, rendering: str) -> dict[str, Any]:
    """硬地板 `x`：`entity_hallucination` 占全部调用的比例。

    §12.5.1：「若它占全部调用的 x%，最终通过率的天花板就是 (100−x)%，与 r 无关。」
    """
    rows = [
        o
        for o in outcomes
        if o.config == config and o.rendering == rendering and o.stratum == "valid" and o.ok
    ]
    hallucinated = sum(1 for o in rows if "entity_hallucination" in o.first_failure_modes)
    n = len(rows)
    interval = wilson_interval(hallucinated, n) if n else None
    return {
        "config": config,
        "rendering": rendering,
        "entity_hallucination_calls": hallucinated,
        "total_calls": n,
        "x": hallucinated / n if n else 0.0,
        "x_interval": interval.__dict__ if interval else None,
        "ceiling": 1.0 - (hallucinated / n if n else 0.0),
    }


def summarize(outcomes: Sequence[ToolCallOutcome], config: str, rendering: str) -> dict[str, Any]:
    """一组 (配置, 口径) 的六项指标 + 三条推导。"""
    vm = valid_metrics(outcomes, config, rendering)
    gm = guardrail_metrics(outcomes, config, rendering)
    r = solve_r(vm.first_pass_rate, vm.final_pass_rate)
    floor = hard_floor(outcomes, config, rendering)

    return {
        "config": config,
        "rendering": rendering,
        "calls": vm.calls,
        "errored": vm.errored,
        "first_pass": {
            "rate": vm.first_pass_rate,
            "interval": wilson_interval(vm.first_pass, vm.calls).__dict__ if vm.calls else None,
        },
        "final_pass": {
            "rate": vm.final_pass_rate,
            "interval": wilson_interval(vm.final_pass, vm.calls).__dict__ if vm.calls else None,
        },
        "retry_coefficient": vm.retry_coefficient,
        "degrade_rate": vm.degrade_rate,
        "acl_intercept_rate": gm.acl_intercept_rate,
        "acl": f"{gm.acl_intercepted}/{gm.acl_total}",
        "budget_trip_rate": gm.budget_trip_rate,
        "budget": f"{gm.budget_correct}/{gm.budget_total}",
        "tool_selection_rate": vm.tool_selection_rate,
        "params_exact_rate": vm.params_exact_rate,
        "acl_attempts": vm.acl_attempts,
        "first_failure_modes": vm.first_failure_modes,
        "all_failure_modes": vm.all_failure_modes,
        "per_round_first_pass": vm.per_round_first_pass,
        "mean_wall_s": vm.mean_wall_s,
        # ── §12.5.1 点名的三条推导 ──────────────────────────────────
        "derivation": {
            "p": vm.first_pass_rate,
            "final_measured": vm.final_pass_rate,
            "r_backsolved": r,
            "retry_predicted": predicted_retry_coefficient(vm.first_pass_rate, r),
            "retry_measured": vm.retry_coefficient,
            "retry_gap": predicted_retry_coefficient(vm.first_pass_rate, r) - vm.retry_coefficient,
            "call_level_failure": 1.0 - vm.final_pass_rate,
            "request_level_failure": request_level_failure(1.0 - vm.final_pass_rate),
            "calls_per_request": CALLS_PER_REQUEST,
        },
        "hard_floor": floor,
    }


__all__ = [
    "CALLS_PER_REQUEST",
    "hard_floor",
    "load_outcomes",
    "predicted_retry_coefficient",
    "request_level_failure",
    "solve_r",
    "summarize",
]
