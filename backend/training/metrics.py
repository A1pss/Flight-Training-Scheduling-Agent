"""把 `toolcall_eval` 的原始结果聚合成 §12.5.1 / §15.4 的指标。

**分子分母的口径写在这里，只写一次。** `toolcall_eval` 只记事实（每条跑成什么样），
本模块负责算，报告负责渲染 —— 三件事分开，是为了让「这个数怎么来的」永远有唯一
一处可查的定义。

## 三条容易出错的口径

**① 跑挂的条目不进分母。** Ollama 掉线那条不是「模型答错了」。`error` 非空的
条目单独计数（`errored`），既不算分子也不算分母 —— 混进去会把一次运维事故记成
模型能力下降。

**② 一次通过率是「调用级」的，本数据集下一条场景 = 一次调用。** §12.5.1 的
口径栏点名过这件事：前三行是每次工具调用的统计，降级触发率是每次用户请求的
统计，两者差一个复合次数。本数据集一条场景就是一次调用，所以两者恰好同分母；
**换成端到端场景集时这条不再成立**，别照抄。

**③ 失败模式分布取「首次尝试」的。** §15.2 ⑥ 要的是「模型第一反应会犯什么错」，
拿全程（含重试）的分布去挖难负例，会把「回灌之后的二次错误」也算成一类高频失败，
而那类错误在生产里根本不会单独出现。全程分布另存一份，用来看重试救回了什么。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from backend.harness.types import FailureMode
from backend.training.toolcall_eval import ToolCallOutcome

#: 失败模式的固定列序 —— 报告里的表头永远这个顺序，便于跨轮次肉眼比对。
FAILURE_MODE_ORDER: Final[tuple[str, ...]] = (
    FailureMode.MISSING_FIELD.value,
    FailureMode.TYPE_ERROR.value,
    FailureMode.ENTITY_HALLUCINATION.value,
    FailureMode.ENUM_OUT_OF_RANGE.value,
    FailureMode.JSON_MALFORMED.value,
)


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


class ValidMetrics(BaseModel):
    """`valid` 层（200 条）的指标。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: str
    rendering: str = "task"
    rounds: int = 0
    #: 参与统计的调用数（已剔除跑挂的）
    calls: int = 0
    errored: int = 0

    first_pass: int = 0
    first_pass_rate: float = 0.0
    #: 模型点了 ACL 行之外的工具的次数（口径 B 才会非零）。**单列**：
    #: 它是权限失败不是契约失败，混进五类分布表会污染 §15.2 ⑥ 的挑样输入。
    acl_attempts: int = 0
    acl_attempt_rate: float = 0.0
    final_pass: int = 0
    final_pass_rate: float = 0.0
    degraded: int = 0
    degrade_rate: float = 0.0
    #: 平均重试系数 = 每次工具调用实际发出的 LLM 请求数（含重试）
    retry_coefficient: float = 0.0

    #: 诊断指标（**不作准入门禁**，业务方 2026-08-20 裁定）
    tool_correct: int = 0
    tool_selection_rate: float = 0.0
    params_exact: int = 0
    params_exact_rate: float = 0.0

    #: 失败模式分布：首次尝试的（进 §15.2 ⑥）与全程的
    first_failure_modes: dict[str, int] = Field(default_factory=dict)
    all_failure_modes: dict[str, int] = Field(default_factory=dict)

    #: 每轮的一次通过率 —— 温度 0 下三轮应当几乎一致，飘得厉害说明推理端不稳。
    #: 键是**字符串形态的轮次号**：三个分组字段用同一个 `_group_rate`，
    #: 键型跟着统一成 str，直接进 JSON 不用再转一次。
    per_round_first_pass: dict[str, float] = Field(default_factory=dict)
    #: 每个组件的一次通过率
    per_component_first_pass: dict[str, float] = Field(default_factory=dict)
    #: 一次通过率最低的工具（工具名 → 一次通过率），只列有失败的
    per_tool_first_pass: dict[str, float] = Field(default_factory=dict)

    mean_wall_s: float = 0.0


class GuardrailMetrics(BaseModel):
    """越权层与超预算层（各 30 条）—— 确定性，目标都是 100%。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config: str
    rendering: str = "task"
    acl_total: int = 0
    acl_intercepted: int = 0
    acl_intercept_rate: float = 0.0
    budget_total: int = 0
    budget_correct: int = 0
    budget_trip_rate: float = 0.0


def valid_metrics(
    outcomes: Sequence[ToolCallOutcome], config: str, rendering: str = "task"
) -> ValidMetrics:
    """算 `valid` 层的全部指标。

    **必须同时按 `rendering` 过滤**：口径 A 与口径 B 测的是两件事（前者给已知条件、
    后者不给），混在一个分母里算出来的一次通过率哪一件都不代表。
    """
    rows = [
        o
        for o in outcomes
        if o.config == config and o.rendering == rendering and o.stratum == "valid"
    ]
    errored = [o for o in rows if not o.ok]
    good = [o for o in rows if o.ok]
    n = len(good)

    first = Counter(m for o in good for m in o.first_failure_modes)
    every = Counter(m for o in good for m in o.all_failure_modes)

    return ValidMetrics(
        config=config,
        rendering=rendering,
        rounds=len({o.round_index for o in rows}),
        calls=n,
        errored=len(errored),
        first_pass=sum(o.first_pass for o in good),
        first_pass_rate=_rate(sum(o.first_pass for o in good), n),
        acl_attempts=sum(o.acl_attempt for o in good),
        acl_attempt_rate=_rate(sum(o.acl_attempt for o in good), n),
        final_pass=sum(o.final_pass for o in good),
        final_pass_rate=_rate(sum(o.final_pass for o in good), n),
        degraded=sum(o.degraded for o in good),
        degrade_rate=_rate(sum(o.degraded for o in good), n),
        retry_coefficient=round(sum(o.llm_calls for o in good) / n, 4) if n else 0.0,
        tool_correct=sum(o.tool_correct for o in good),
        tool_selection_rate=_rate(sum(o.tool_correct for o in good), n),
        params_exact=sum(o.params_exact for o in good),
        params_exact_rate=_rate(sum(o.params_exact for o in good), n),
        first_failure_modes={m: first.get(m, 0) for m in FAILURE_MODE_ORDER},
        all_failure_modes={m: every.get(m, 0) for m in FAILURE_MODE_ORDER},
        per_round_first_pass=_group_rate(good, lambda o: o.round_index),
        per_component_first_pass=_group_rate(good, lambda o: o.component),
        per_tool_first_pass=_group_rate(good, lambda o: o.tool),
        mean_wall_s=round(sum(o.wall_s for o in good) / n, 3) if n else 0.0,
    )


def guardrail_metrics(
    outcomes: Sequence[ToolCallOutcome], config: str, rendering: str = "task"
) -> GuardrailMetrics:
    """算越权层与超预算层的拦截率。

    这两层与渲染无关（不调模型），但仍按 `rendering` 过滤 —— 同一个文件里两种口径
    各写了一份，不过滤会把同一条场景数两遍，拦截率的分母凭空翻倍。
    """
    acl = [
        o
        for o in outcomes
        if o.config == config and o.rendering == rendering and o.stratum == "acl_violation"
    ]
    bgt = [
        o
        for o in outcomes
        if o.config == config and o.rendering == rendering and o.stratum == "budget_exhaustion"
    ]
    acl_hit = sum(o.intercepted and o.error_code == o.expected_error_code for o in acl)
    bgt_hit = sum(o.intercepted for o in bgt)
    return GuardrailMetrics(
        config=config,
        rendering=rendering,
        acl_total=len(acl),
        acl_intercepted=acl_hit,
        acl_intercept_rate=_rate(acl_hit, len(acl)),
        budget_total=len(bgt),
        budget_correct=bgt_hit,
        budget_trip_rate=_rate(bgt_hit, len(bgt)),
    )


def _group_rate(
    rows: Sequence[ToolCallOutcome],
    key: Callable[[ToolCallOutcome], object],
) -> dict[str, float]:
    """按某个键分组算一次通过率。键统一转成 str，便于直接进 JSON。"""
    totals: Counter[str] = Counter()
    passes: Counter[str] = Counter()
    for row in rows:
        bucket = str(key(row))
        totals[bucket] += 1
        passes[bucket] += int(row.first_pass)
    return {k: _rate(passes[k], totals[k]) for k in sorted(totals)}


def worst_tools(metrics: ValidMetrics, limit: int = 10) -> tuple[tuple[str, float], ...]:
    """一次通过率最低的若干工具 —— §15.2 ⑥ 难负例的挑样入口。"""
    ranked = sorted(metrics.per_tool_first_pass.items(), key=lambda kv: (kv[1], kv[0]))
    return tuple(item for item in ranked if item[1] < 1.0)[:limit]


__all__ = [
    "FAILURE_MODE_ORDER",
    "GuardrailMetrics",
    "ValidMetrics",
    "guardrail_metrics",
    "valid_metrics",
    "worst_tools",
]
