"""Planner：把模糊需求翻译成精确的求解输入（v6 §7.3）。

```python
def planner_node(state: FTSState) -> Command:
    intent = planner_llm.invoke(build_planner_prompt(state))
    radius = estimate_scope(intent, state.prev_plan)            # ① 影响面探测
    if radius > BLAST_RADIUS_THRESHOLD and intent.freeze_policy == "AGGRESSIVE":
        intent = downgrade_freeze(intent, ...)
    for tier in intent.pre_authorized_tiers:                    # ② 权限校验
        if RELAX_TIER_AUTHORITY[tier] > state.user_role: ...
    if intent.open_questions:                                   # ③ 有未决问题就追问
        return Command(goto="route", update={..., "needs_clarification": True})
    return Command(goto="compile_spec", update={"solve_intent": intent})
```

本模块是上面这段的落地，**不含图的部分**——`Command` 的构造在
`backend/components/planner.py`，这里只负责「算出该给什么 `SolveIntent`」。
分开是为了让这段逻辑能脱离 LangGraph 单测。

## 只能调四类旋钮

`SolveIntent` 的契约层已经把这件事写死了（`backend.schemas.intent`）：范围 /
冻结策略 / 目标权重 / 松弛档位。**它不能增删硬约束、不能指定具体架次、不能
绕过任何 R0 规则**。穿过 `compile_spec_node` 后还要与 `ruleset.yaml` 合并，
冲突时以 ruleset 为准——所以即使模型硬塞了什么，也到不了求解器。

## LLM 不在场时它照样工作

`harness=None`（或 LLM 降级）时走 :func:`deterministic_intent`：按请求里的
范围直接生成一个中性的 `SolveIntent`。这就是 **FTS-4001 的降级路径**——
「LLM 挂了，排班能力必须还在」（v6 §7.6 末句）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

from backend.core.config import Settings, get_settings
from backend.core.errors import FTSError
from backend.harness import AgentSpec, ContextBlock, Harness, structured_summary
from backend.planner.authority import authorized_tiers
from backend.planner.scope import ScopeDecision, apply_scope_policy
from backend.schemas.intent import (
    ObjectiveWeights,
    QueryRequest,
    SchedulingRequest,
    SolveIntent,
    UserRole,
)
from backend.schemas.plan import SchedulePlan

#: Planner 在主流程里暴露给模型的工具（必须是 ACL 行的子集，少给可以多给不行）
PLANNER_TOOLS: Final[tuple[str, ...]] = (
    "resolve_person",
    "resolve_aircraft",
    "resolve_week",
    "estimate_scope",
    "assess_disruption",
    "propose_solve_intent",
    "check_authority",
    "ask_user",
    "escalate",
)

PLANNER_AGENT: Final[AgentSpec] = AgentSpec(name="planner", tools=PLANNER_TOOLS)

#: 三项目标权重的中性默认值（R3 偏好档，怎么调都不影响可行性）
NEUTRAL_WEIGHTS: Final[ObjectiveWeights] = ObjectiveWeights(
    progress=1.0, disruption=1.0, balance=1.0
)


@dataclass(frozen=True)
class PlannerDecision:
    """Planner 一次调用的完整结论。"""

    intent: SolveIntent
    #: 无法自行决定、需向用户确认的点。非空 → 回路由组织追问（§7.3.3 第 ③ 步）
    open_questions: tuple[str, ...] = ()
    scope: ScopeDecision | None = None
    llm_calls: int = 0
    degraded: bool = False
    notes: tuple[str, ...] = ()

    @property
    def needs_clarification(self) -> bool:
        return bool(self.open_questions)

    @property
    def next_node(self) -> str:
        """下一跳恒定：要么回路由追问，要么去编译规格。Planner 不自主选路。"""
        return "route" if self.needs_clarification else "compile_spec"


def deterministic_intent(
    request: SchedulingRequest | QueryRequest | None,
    *,
    freeze_policy: str = "BALANCED",
) -> SolveIntent:
    """不经 LLM 的 `SolveIntent`（FTS-4001 降级路径 / 结构化入口）。

    范围直接取请求里已消解的编号；没点名就是 `ALL`。**不猜冻结档**：一律
    `BALANCED`，理由写清是「降级路径的中性默认」，让 Sheet 4 看得出来这次
    没有 LLM 参与。
    """
    persons: list[str] | str = "ALL"
    missions: list[str] | str = "ALL"
    if isinstance(request, SchedulingRequest):
        if request.persons:
            persons = list(request.persons)
        if request.missions:
            missions = list(request.missions)
    return SolveIntent(
        scope_persons=persons,  # type: ignore[arg-type]
        scope_missions=missions,  # type: ignore[arg-type]
        freeze_policy=freeze_policy,  # type: ignore[arg-type]
        freeze_reason="中性默认档（未经 LLM 规划：结构化入口或 LLM 降级路径）",
        objective_weights=NEUTRAL_WEIGHTS,
        pre_authorized_tiers=[0],
        incremental_constraints=[],
        estimated_blast_radius=0,
        open_questions=[],
    )


def _planner_blocks(
    request: SchedulingRequest | QueryRequest | None,
    prev_plan: SchedulePlan | None,
    *,
    user_role: UserRole,
) -> list[ContextBlock]:
    """装配 Planner 的上下文。**结构化数据只入摘要**（v6 §7.7.1 第 5 行）。"""
    summary: dict[str, Any] = {"角色": user_role}
    if isinstance(request, SchedulingRequest):
        summary.update(
            {
                "意图": request.kind,
                "目标周": request.iso_week or "（未指定）",
                "点名人员": request.persons or "（未点名，视为全体）",
                "点名飞机": request.aircraft or "（无）",
                "点名课目": request.missions or "（未点名，视为全部）",
            }
        )
    if prev_plan is not None:
        summary["上一版方案"] = f"{prev_plan.plan_id}（{len(prev_plan.sorties)} 架次）"

    blocks = [ContextBlock(kind="summary", content=structured_summary("本次请求", summary))]
    if request is not None:
        blocks.append(ContextBlock(kind="history", content=request.raw_text, role="user"))
    return blocks


def _intent_from_calls(output: Any) -> tuple[SolveIntent | None, list[str]]:
    """从工具调用里取出 `SolveIntent` 与追问。

    `ask_user` 的问题**原样进 `open_questions`**：模型认为自己拿不准的地方，
    正是该问用户的地方，改写它只会把信息磨掉。
    """
    intent: SolveIntent | None = None
    questions: list[str] = []
    for call in output.calls:
        if call.name == "propose_solve_intent":
            payload = call.arguments.get("intent")
            if isinstance(payload, SolveIntent):
                intent = payload
            elif isinstance(payload, dict):
                intent = SolveIntent.model_validate(payload)
            elif isinstance(payload, str):
                intent = SolveIntent.model_validate(json.loads(payload))
        elif call.name == "ask_user":
            question = str(call.arguments.get("question", "")).strip()
            if question:
                questions.append(question)
        elif call.name == "escalate":
            reason = str(call.arguments.get("reason", "")).strip()
            if reason:
                questions.append(f"需人工处理：{reason}")
    return intent, questions


def plan_solve_intent(
    request: SchedulingRequest | QueryRequest | None,
    *,
    user_role: UserRole,
    prev_plan: SchedulePlan | None = None,
    harness: Harness | None = None,
    settings: Settings | None = None,
) -> PlannerDecision:
    """v6 §7.3.3 的三步，完整落地。

    Planner **在一次请求内只被调用一次**，不自主循环、不自主选择下一跳。
    """
    cfg = settings or get_settings()
    notes: list[str] = []
    questions: list[str] = []
    llm_calls = 0
    degraded = False
    intent: SolveIntent | None = None

    if harness is not None:
        try:
            output = harness.call(
                PLANNER_AGENT, _planner_blocks(request, prev_plan, user_role=user_role)
            )
            llm_calls = output.llm_calls
            if output.degraded:
                degraded = True
                notes.append(f"Planner 降级（{output.error_code}），改用中性默认 SolveIntent")
            else:
                intent, questions = _intent_from_calls(output)
                if intent is None:
                    notes.append("模型未产出 propose_solve_intent，改用中性默认 SolveIntent")
        except FTSError as exc:
            degraded = True
            notes.append(f"Planner 不可用（{exc.message}），改用中性默认 SolveIntent")

    if intent is None:
        intent = deterministic_intent(request)

    # ① 影响面探测 + 自我降档
    scope = apply_scope_policy(intent, prev_plan, threshold=cfg.BLAST_RADIUS_THRESHOLD)
    intent = scope.intent
    if scope.verdict == "downgraded":
        notes.append(scope.reason)

    # ② 权限校验：预授权 R1 档需要角色权限
    kept, denied = authorized_tiers(list(intent.pre_authorized_tiers), user_role)
    questions.extend(denied)
    intent = intent.model_copy(update={"pre_authorized_tiers": kept})

    # ③ 未决问题一并落进 SolveIntent，供 Sheet 4 与追问文案取用
    merged = list(dict.fromkeys([*intent.open_questions, *questions]))
    intent = intent.model_copy(update={"open_questions": merged})

    return PlannerDecision(
        intent=intent,
        open_questions=tuple(merged),
        scope=scope,
        llm_calls=llm_calls,
        degraded=degraded,
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class ClarificationRequest:
    """回路由时带的追问内容（v6 §7.3.3 第 ③ 步 + §7.2.1 的实体反问）。"""

    questions: tuple[str, ...] = ()
    ambiguities: tuple[dict[str, Any], ...] = field(default=())

    def as_text(self) -> str:
        """拼成一段能直接发给用户的话。"""
        lines: list[str] = []
        for item in self.ambiguities:
            question = str(item.get("question", "")).strip()
            if question:
                lines.append(question)
        lines.extend(self.questions)
        if not lines:
            return ""
        numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, start=1))
        return f"还有几点需要您确认：\n{numbered}"


__all__ = [
    "NEUTRAL_WEIGHTS",
    "PLANNER_AGENT",
    "PLANNER_TOOLS",
    "ClarificationRequest",
    "PlannerDecision",
    "deterministic_intent",
    "plan_solve_intent",
]
