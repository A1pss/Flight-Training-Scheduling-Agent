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
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from backend.core.config import Settings, get_settings
from backend.core.errors import FTSError
from backend.harness import (
    AgentOutput,
    AgentSpec,
    ContextBlock,
    Harness,
    ToolResult,
    ValidatedCall,
    structured_summary,
)
from backend.planner.authority import authorized_tiers
from backend.planner.revision import for_solver
from backend.planner.scope import ScopeDecision, apply_scope_policy
from backend.routing.entities import EntityDirectory, iso_week_of
from backend.routing.modifiers import scan_modifiers
from backend.schemas.intent import (
    IncrementalConstraint,
    ObjectiveWeights,
    QueryRequest,
    SchedulingRequest,
    SolveIntent,
    UserRole,
)
from backend.schemas.plan import TRAINING_WINDOW_END, TRAINING_WINDOW_START, SchedulePlan

#: Planner 在主流程里暴露给模型的工具（必须是 ACL 行的子集，少给可以多给不行）
PLANNER_TOOLS: Final[tuple[str, ...]] = (
    "resolve_person",
    "resolve_aircraft",
    "resolve_week",
    "estimate_scope",
    "assess_disruption",
    "translate_revision",
    "propose_solve_intent",
    "check_authority",
    "ask_user",
    "escalate",
)

PLANNER_AGENT: Final[AgentSpec] = AgentSpec(
    name="planner",
    tools=PLANNER_TOOLS,
    required_tools=("propose_solve_intent",),
)

_WEEK_SURFACE = re.compile(r"\d{4}-?W\d{1,2}|本周|这周|下周|上周|下一周|上一周")
_GROUP_SCOPE = re.compile(r"所有人|全体|学员们|全体学员|全体教员|教员们")

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


def target_week_of(
    request: SchedulingRequest | QueryRequest | None,
    week_start: date | str | None = None,
) -> str | None:
    """本次请求的目标周，三级回退；**三处都没有就返回 `None`**。

    | 优先级 | 来源 | 何时有值 |
    |---|---|---|
    | ① | `request.iso_week` | 用户话里说了周次，`resolve_week` 消解出来了 |
    | ② | `request.week_start` | 结构化入口按日期给的 |
    | ③ | `state["week_start"]` | 图的黑板上已有周次（上游节点或 API 放进去的） |

    **③ 是本函数存在的理由。** 原实现只读 ①，于是「用户没说周次、但周次早就在
    state 里」这种再普通不过的情形，Planner 会看到「（未指定）」并如实追问 ——
    模型没做错，是它没拿到那个信息。这条缺陷会同时推高 §12.2 的主指标
    （「正确地反问」计为成功）、压低误执行率、并让由误执行率反推的反问阈值偏移。

    **三处皆空时返回 `None`，调用方据此渲染「（未指定）」并照旧追问** ——
    这里**绝不设默认周次**（S-14 / v6 §5.1.1 / `FTS-1004`：缺输入即提问）。
    业务方 2026-08-21 确认此口径。

    ⚠️ 与 M9-A「缺周次一律归歧义层」的裁定不冲突：那条说的是**用户这句话里**
    没有周次；周次来自 state 时不属于「缺」。
    """
    if isinstance(request, SchedulingRequest):
        if request.iso_week:
            return request.iso_week
        if request.week_start is not None:
            return iso_week_of(request.week_start)
    if isinstance(week_start, str):
        # state 里存的是 ISO 日期串。解析不了就当没有——不猜，照旧追问。
        try:
            week_start = date.fromisoformat(week_start)
        except ValueError:
            return None
    if isinstance(week_start, date):
        return iso_week_of(week_start)
    return None


def _planner_blocks(
    request: SchedulingRequest | QueryRequest | None,
    prev_plan: SchedulePlan | None,
    *,
    user_role: UserRole,
    week_start: date | str | None = None,
) -> list[ContextBlock]:
    """装配 Planner 的上下文。**结构化数据只入摘要**（v6 §7.7.1 第 5 行）。"""
    summary: dict[str, Any] = {"角色": user_role}
    if isinstance(request, SchedulingRequest):
        summary.update(
            {
                "意图": request.kind,
                "目标周": target_week_of(request, week_start) or "（未指定）",
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


def recommended_planner_tools(
    request: SchedulingRequest | QueryRequest | None,
) -> tuple[str, ...]:
    """按请求形状给出 Planner 的建议工具顺序。

    这些工具用于补证与影响面判断，但不是 Harness 的“全有或全无”契约。
    把它们全部塞进 ``AgentSpec.required_tools`` 会让一个辅助调用缺席时，连同
    已经正确生成的 ``propose_solve_intent`` 一起丢弃；M9-B 完整轨迹里这正是
    多条 Schedule/Reschedule 变成“零工具”的直接原因。

    真正不可缺的只有收口工具 ``propose_solve_intent``。用户已经由上游消解的
    人员/课目范围会在 :func:`_complete_explicit_scope` 再做确定性合并，因此
    辅助工具少一次不会让模型凭空扩大求解范围。
    """
    if not isinstance(request, SchedulingRequest):
        return ("propose_solve_intent",)
    recommended: list[str] = []
    if request.persons:
        recommended.append("resolve_person")
    if request.aircraft:
        recommended.append("resolve_aircraft")
    if (
        request.kind == "schedule"
        and _WEEK_SURFACE.search(request.raw_text)
        and _GROUP_SCOPE.search(request.raw_text)
    ):
        recommended.append("resolve_week")
    # 首轮排班中的显式限制需要翻译成增量约束；重排则先评估既有方案受影响面，
    # 修饰本身由下方确定性扫描兼并，避免把 ``translate_revision`` 错排到
    # ``assess_disruption`` 之前。
    if request.kind == "schedule" and scan_modifiers(request.raw_text):
        recommended.append("translate_revision")
    if _GROUP_SCOPE.search(request.raw_text):
        recommended.append("estimate_scope")
    if request.kind == "reschedule":
        recommended.append("assess_disruption")
    recommended.append("propose_solve_intent")
    return tuple(dict.fromkeys(recommended))


def required_planner_tools(
    request: SchedulingRequest | QueryRequest | None,  # noqa: ARG001 - 保留统一调用签名
) -> tuple[str, ...]:
    """Planner 唯一的强制收口契约。

    实体消解、范围估算和扰动评估是建议调用；只有结构化提案缺席时，本轮才是
    真正的半成品，必须由 Harness 回灌重试。
    """
    return ("propose_solve_intent",)


def _reschedule_preflight_calls(
    request: SchedulingRequest | QueryRequest | None,
    *,
    week_start: date | str | None,
    prev_plan: SchedulePlan | None,
) -> tuple[ValidatedCall, ...]:
    """构造重排的确定性前置检查。

    ``SchedulingRequest`` 已由 Route 消解出实体编号，重排也天然已有“先知道受
    影响对象、再决定冻结档”的固定因果顺序。实体确认与影响面评估因此不是让
    模型自由探索的工具选择，而是编译 ``SolveIntent`` 前的输入核验。

    仅在目标周已知时运行；缺周次仍由既有的“缺输入即提问”链路处理，绝不默认
    一个周次。
    """
    if not isinstance(request, SchedulingRequest) or request.kind != "reschedule":
        return ()
    iso_week = target_week_of(request, week_start)
    if not iso_week:
        return ()
    calls: list[ValidatedCall] = []
    calls.extend(
        ValidatedCall(name="resolve_person", arguments={"surface": person})
        for person in request.persons
    )
    calls.extend(
        ValidatedCall(name="resolve_aircraft", arguments={"surface": aircraft})
        for aircraft in request.aircraft
    )
    calls.append(
        ValidatedCall(
            name="assess_disruption",
            arguments={
                "iso_week": iso_week,
                "baseline_plan_id": prev_plan.plan_id if prev_plan is not None else "",
                "changed_persons": list(request.persons),
                "changed_aircraft": list(request.aircraft),
            },
        )
    )
    return tuple(calls)


def _preflight_feedback_block(
    calls: tuple[ValidatedCall, ...], results: tuple[ToolResult, ...]
) -> ContextBlock:
    """把确定性预检结果作为 Planner 的证据，不再要求模型重复查询。"""
    lines: list[str] = []
    for call, result in zip(calls, results, strict=True):
        outcome = _brief(result.value) if result.ok else f"失败：{result.error}"
        lines.append(f"- {call.name}({_brief(call.arguments)}) → {outcome}")
    return ContextBlock(
        kind="evidence",
        role="user",
        label="reschedule_preflight",
        content=(
            "以下重排前置核验已由系统确定性完成，请直接依据结果作答：\n"
            + "\n".join(lines)
            + "\n\n现在调用 `propose_solve_intent` 给出求解意图；信息仍不足时调用 `ask_user`。"
        ),
    )


def _complete_explicit_scope(
    intent: SolveIntent,
    request: SchedulingRequest | QueryRequest | None,
    directory: EntityDirectory | None = None,
) -> SolveIntent:
    """让上游已经消解的显式范围覆盖模型的猜测。

    Planner 仍决定冻结档与目标权重；但用户点名的人员/课目不是偏好，而是求解
    输入的明确边界。模型把 ``[P08, P05]`` 写成 ``ALL``、人名或别的编号时，
    不能把错误继续带进 ``compile_spec``。
    """
    if not isinstance(request, SchedulingRequest):
        return intent
    updates: dict[str, Any] = {}
    if request.persons:
        updates["scope_persons"] = list(request.persons)
    elif directory is not None and directory.person_identities:
        raw = request.raw_text
        if re.search(r"全体教员|教员们", raw):
            updates["scope_persons"] = sorted(
                pid for pid, identity in directory.person_identities.items() if identity == "教员"
            )
        elif re.search(r"全体学员|学员们", raw):
            updates["scope_persons"] = sorted(
                pid for pid, identity in directory.person_identities.items() if identity == "学员"
            )
    if request.missions:
        updates["scope_missions"] = list(request.missions)
    return intent.model_copy(update=updates) if updates else intent


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


#: Planner 在**一次节点执行内**允许的最多轮次。**实测后定回 1。**
#:
#: 曾经改成 2：第一轮只调消解类工具没给结论时，把工具结果回灌再问一轮
#: （v6 §12.6 的期望路径确实是「先消解、再提议」的多步形态）。
#: 两条原本复现 3/3 失败的用例也确实通了。
#:
#: **但 360 条 × 3 轮的全量实测证明它不兑现**（业务方 2026-08-22 裁定撤销）：
#:
#:     指标                  单轮      两轮
#:     端到端完成率         78.89%   76.94%
#:     槽位 F1              85.11%   84.81%   ← 跌破 ≥85%
#:     误执行率（全量）       9.44%   10.00%
#:     constraint_modifiers 16.02%   16.00%   ← 纹丝不动
#:     LLM 调用总数           3088     4329   ← +40%
#:
#: 第一轮 360 条的原始计数更直白：抽到的修饰 14/109 → 16/109（多 2 条），
#: 代价是 LLM 调用 1029 → 1443。**多花 40% 推理换 2 条修饰，其余全项持平或变差。**
#:
#: ⚠️ **这条教训比这个常量本身重要**：当初是拿两条**先前失败的**用例验证修复、
#: 两条都通就当它成立 —— 那是拿修复的目标样本去验证修复。
#: **几条样本能验证量具装没装对，不能验证改动有没有用。**
#:
#: 顺带更正了一处归因：`constraint_modifiers` 16% **不是**「Planner 没机会提议」。
#: 两轮下 Planner 跑过的条数完全一样（256/360），追问反而从 14 涨到 23 ——
#: 它本来就有机会，只是**大多数情况下模型压根不产出修饰**。
PLANNER_MAX_TURNS: Final[int] = 1


def _tool_feedback_block(output: AgentOutput) -> ContextBlock:
    """把第一轮的工具调用与结果回灌给模型，供它据此提议 `SolveIntent`。

    只回灌**已执行工具的结论**，不回灌提示词或工具表 —— 后者本来就还在
    上下文里，重复塞一遍只会把窗口撑大（M7 §5.3 实测过回灌导致提示词越滚越长）。
    """
    lines: list[str] = []
    for call, result in zip(output.calls, output.results, strict=False):
        if result.ok:
            lines.append(f"- {call.name}({_brief(call.arguments)}) → {_brief(result.value)}")
        else:
            lines.append(f"- {call.name}({_brief(call.arguments)}) → 失败：{result.error}")
    body = "\n".join(lines) or "（上一轮没有可用结果）"
    return ContextBlock(
        kind="evidence",
        role="user",
        label="planner_turn1",
        content=(
            "你上一轮调用的工具与结果如下：\n"
            f"{body}\n\n"
            "现在请基于这些结果调用 `propose_solve_intent` 给出本次求解意图。"
            "信息仍然不足时改调 `ask_user` 说明缺什么。"
        ),
    )


def _brief(value: Any, limit: int = 160) -> str:
    """把工具入参/返回压成一行，超长截断 —— 回灌的是结论不是明细。"""
    text = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if not isinstance(value, str)
        else value
    )
    return text if len(text) <= limit else text[: limit - 1] + "…"


def plan_solve_intent(
    request: SchedulingRequest | QueryRequest | None,
    *,
    user_role: UserRole,
    prev_plan: SchedulePlan | None = None,
    harness: Harness | None = None,
    settings: Settings | None = None,
    week_start: date | str | None = None,
    directory: EntityDirectory | None = None,
) -> PlannerDecision:
    """v6 §7.3.3 的三步，完整落地。

    Planner 节点**在一次请求内只执行一次**，不自主循环、不自主选择下一跳。

    节点内部的 LLM 调用轮数同样固定为 ``PLANNER_MAX_TURNS``（当前为 1，Z-43）。
    实体消解、影响面评估与提案可以放在同一次响应中；Planner 不自主循环。

    `week_start` 是黑板上已有的周次（`state["week_start"]`），作为目标周的
    第三级来源交给 :func:`target_week_of` —— 不给它，Planner 就看不到那个周次，
    会在信息其实已经有的情况下追问。
    """
    cfg = settings or get_settings()
    notes: list[str] = []
    questions: list[str] = []
    llm_calls = 0
    degraded = False
    intent: SolveIntent | None = None

    if harness is not None:
        blocks = _planner_blocks(request, prev_plan, user_role=user_role, week_start=week_start)
        required_tools = required_planner_tools(request)
        recommended_tools = recommended_planner_tools(request)
        preflight_calls = _reschedule_preflight_calls(
            request,
            week_start=week_start,
            prev_plan=prev_plan,
        )
        blocks.append(
            ContextBlock(
                kind="summary",
                content=structured_summary(
                    "本轮工具契约",
                    {
                        "必须调用": list(required_tools),
                        "建议顺序": list(recommended_tools),
                        "规则": (
                            "重排的实体确认与影响面评估由系统预检；"
                            "模型必须以 propose_solve_intent 收口"
                            if preflight_calls
                            else "辅助工具按需调用；必须以 propose_solve_intent 收口"
                        ),
                    },
                ),
            )
        )
        try:
            planner_agent = PLANNER_AGENT.model_copy(update={"required_tools": required_tools})
            if preflight_calls:
                preflight_results = harness.execute_deterministic_tools("planner", preflight_calls)
                blocks.append(_preflight_feedback_block(preflight_calls, preflight_results))
                # 预检已经留下可审计的真实工具轨迹；不再让模型重复调用同一批
                # 工具，收窄为“提案或追问”可避免一次响应里重复探索污染路径。
                planner_agent = planner_agent.model_copy(
                    update={
                        "tools": ("ask_user", "escalate", "propose_solve_intent"),
                    }
                )
            for turn in range(1, PLANNER_MAX_TURNS + 1):
                output = harness.call(
                    planner_agent,
                    blocks,
                )
                llm_calls += output.llm_calls
                if output.degraded:
                    degraded = True
                    notes.append(f"Planner 降级（{output.error_code}），改用中性默认 SolveIntent")
                    break

                intent, questions = _intent_from_calls(output)
                if intent is not None or questions:
                    # 提议出来了，或者模型明确说要问用户 —— 两种都是**结论**，收尾。
                    break
                if turn == PLANNER_MAX_TURNS:
                    notes.append(
                        f"模型{PLANNER_MAX_TURNS}轮仍未产出 propose_solve_intent，"
                        "改用中性默认 SolveIntent"
                    )
                    break
                # 只调了消解类工具、还没给结论 —— 把结果回灌，让它据此提议。
                notes.append(
                    f"第 {turn} 轮只调了 {'、'.join(c.name for c in output.calls) or '（无工具）'}，"
                    "回灌结果后再问一轮"
                )
                blocks = [*blocks, _tool_feedback_block(output)]
        except FTSError as exc:
            degraded = True
            notes.append(f"Planner 不可用（{exc.message}），改用中性默认 SolveIntent")

    if intent is None:
        intent = deterministic_intent(request)

    intent = _complete_explicit_scope(intent, request, directory)

    # ★ 确定性修饰扫描兼并（`Z-45`）——与 `merge_slots` 对周次的处置同构。
    #   模型抽修饰极不稳定（99 条里只抽到 8 条，且同一修饰换个前半句就抽不到），
    #   而这些表述高度模式化（全集 84 种、7 类）。**可枚举的不交给概率模型。**
    #   **兼并不是覆盖**：扫描器抓到的补进去，模型抽到的自由表述照样保留。
    if isinstance(request, SchedulingRequest) and request.raw_text:
        scanned = scan_modifiers(request.raw_text)
        if scanned:
            # 扫描器命中的模式化表达是该 kind 的权威翻译。模型有时能猜到
            # ``PIN_RESOURCE`` 这个 kind，却把 params 留空；若只按 kind 去重，
            # 正确的 AC10/AC27 集合会被空参数挡掉，求解侧最终禁掉全部候选。
            # 未被扫描器覆盖的自由表述仍完整保留。
            scanned_kinds = {c.kind for c in scanned}
            scanned_surfaces = [c.origin_utterance.strip() for c in scanned]

            def scanner_covers(candidate: IncrementalConstraint) -> bool:
                origin = candidate.origin_utterance.strip()
                return any(
                    surface and (surface in origin or origin in surface)
                    for surface in scanned_surfaces
                )

            merged_constraints = [
                c
                for c in intent.incremental_constraints
                if c.kind not in scanned_kinds and not scanner_covers(c)
            ]

            def solver_ready(constraint: IncrementalConstraint) -> IncrementalConstraint:
                return for_solver(
                    constraint,
                    window_start=TRAINING_WINDOW_START,
                    horizon_minutes=(
                        TRAINING_WINDOW_END.hour * 60
                        + TRAINING_WINDOW_END.minute
                        - TRAINING_WINDOW_START.hour * 60
                        - TRAINING_WINDOW_START.minute
                    ),
                )

            merged_constraints.extend(solver_ready(constraint) for constraint in scanned)
            intent = intent.model_copy(update={"incremental_constraints": merged_constraints})

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
    "recommended_planner_tools",
    "required_planner_tools",
]
