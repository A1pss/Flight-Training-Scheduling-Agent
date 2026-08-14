"""Planner 与意图路由的工具接线（v6 §7.7.2 第 1~4 行）。

M4-A 把 33 个工具的**入参契约**全部定稿，实现分属各自里程碑；
「Planner 类是 M4-B」是它在收工报告 §9.3 里写下的分工。本模块就是那份接线。

## 三条硬要求（M4-A §8 第 2 条）

1. **handler 签名是 `Callable[[dict], Any]`**；
2. **返回值必须可 JSON 序列化** —— 要进 trace 与 Redis 缓存，不可序列化的对象
   在重放时对不上；
3. **没接线的工具调用时抛 `ToolNotBoundError`，不返回空结果**。这条不用我们做，
   `ToolRegistry` 已经保证了——**而它正是本模块存在的原因**：真机跑第一次
   端到端时，`planner` 节点当场抛
   `ToolNotBoundError: 工具 'resolve_week' 在目录中但没有接上实现`。
   单测用 `FakeHarness` 是照不出这个的。

## 实体消解的返回形状

`resolve_*` 返回的是**消解结果**，不是一个编号字符串：

```json
{"resolved": true, "entity_id": "P08", "confidence": 1.0, "reason": "exact_name"}
{"resolved": false, "reason": "ambiguous", "question": "「郝超」有多个可能：…",
 "candidates": [{"entity_id": "P02", "label": "高超", "distance": 1}, …]}
```

**消解不了时返回 `resolved: false` 而不是抛异常**：模型该看见的是「这个名字有
歧义，去问用户」，不是一个栈回溯。歧义本身是要反问的信号，不是错误。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from backend.harness.types import ToolHandler
from backend.planner.authority import check_authority
from backend.planner.revision import echo_text, rule_translate
from backend.planner.scope import assess_disruption, estimate_scope
from backend.routing.entities import (
    EntityDirectory,
    Resolution,
    resolve_aircraft,
    resolve_person,
    resolve_week,
)
from backend.schemas.intent import SolveIntent
from backend.schemas.plan import SchedulePlan


def _as_payload(resolution: Resolution) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "resolved": resolution.resolved,
        "entity_id": resolution.entity_id,
        "confidence": round(resolution.confidence, 4),
        "reason": resolution.reason,
    }
    if not resolution.resolved:
        payload["question"] = resolution.question()
        payload["candidates"] = [
            {"entity_id": c.entity_id, "label": c.label, "distance": c.distance}
            for c in resolution.candidates
        ]
    return payload


def planner_tool_handlers(
    *,
    directory: EntityDirectory,
    today: date,
    prev_plan: SchedulePlan | None = None,
    user_role: str = "scheduler",
    sink: list[dict[str, Any]] | None = None,
) -> dict[str, ToolHandler]:
    """把 route / Planner 两行的工具接到本包的实现上。

    `sink` 收集 `ask_user` / `escalate` 的内容——它们是**要交给人的东西**，
    不是给模型自己看的返回值。节点从 `sink` 里取出来放进 `open_questions`。
    """
    collected = sink if sink is not None else []

    def _resolve_person(args: dict[str, Any]) -> Any:
        return _as_payload(resolve_person(str(args["surface"]), directory))

    def _resolve_aircraft(args: dict[str, Any]) -> Any:
        return _as_payload(resolve_aircraft(str(args["surface"]), directory))

    def _resolve_week(args: dict[str, Any]) -> Any:
        # 参照日期由调用方给，模型给的 `reference_date` 只在它明确写了时才用——
        # 「本周」在重放时必须解出同一周（§12.5.2 的重放一致率）
        raw = str(args.get("reference_date") or "")
        reference = date.fromisoformat(raw) if raw else today
        return _as_payload(resolve_week(str(args["surface"]), today=reference))

    def _ask_user(args: dict[str, Any]) -> Any:
        item = {
            "kind": "ask_user",
            "question": str(args["question"]),
            "resolution": str(args.get("resolution", "answer")),
            "options": [str(o) for o in args.get("options", [])],
        }
        collected.append(item)
        return {"recorded": True, "question": item["question"]}

    def _escalate(args: dict[str, Any]) -> Any:
        item = {
            "kind": "escalate",
            "reason": str(args["reason"]),
            "severity": str(args.get("severity", "WARN")),
        }
        collected.append(item)
        return {"recorded": True, "reason": item["reason"]}

    def _estimate_scope(args: dict[str, Any]) -> Any:
        """影响面估算。**首轮排班恒为 0** —— 没有既有方案就没有扰动。"""
        intent = SolveIntent(
            scope_persons=args.get("scope_persons", "ALL"),
            scope_missions=args.get("scope_missions", "ALL"),
            freeze_policy="BALANCED",
            freeze_reason="estimate_scope 的试算，不进 Sheet 4",
            objective_weights=_neutral_weights(),
            estimated_blast_radius=0,
        )
        radius = estimate_scope(intent, prev_plan)
        return {
            "iso_week": str(args.get("iso_week", "")),
            "estimated_blast_radius": radius,
            "baseline_sorties": len(prev_plan.sorties) if prev_plan else 0,
            "note": "首轮排班没有既有方案，影响面按定义为 0" if prev_plan is None else "",
        }

    def _assess_disruption(args: dict[str, Any]) -> Any:
        """相对基线的扰动。没有基线时如实说「没有基线」，不编一个 0 出来。"""
        if prev_plan is None:
            return {
                "baseline_plan_id": str(args.get("baseline_plan_id", "")),
                "has_baseline": False,
                "note": "本次没有基线方案可比（首轮排班）",
            }
        report = assess_disruption(prev_plan, prev_plan)
        changed_persons = {str(p) for p in args.get("changed_persons", [])}
        changed_aircraft = {str(a) for a in args.get("changed_aircraft", [])}
        touched = [
            s.sortie_id
            for s in prev_plan.sorties
            if s.aircraft_id in changed_aircraft
            or any(c.person_id in changed_persons for c in s.crew)
        ]
        return {
            "baseline_plan_id": report.baseline_plan_id,
            "has_baseline": True,
            "baseline_sorties": report.total_baseline,
            "directly_touched": touched,
            "directly_touched_count": len(touched),
        }

    def _propose_solve_intent(args: dict[str, Any]) -> Any:
        """回显 Planner 给出的 `SolveIntent`。

        **这里不做校验以外的任何事**：意图的三步处理（影响面 → 授权 → 追问）
        在 `planner.intent.plan_solve_intent` 里，那是节点代码的职责，不是工具的。
        工具返回的东西会进上下文给模型看，在这里偷偷改一版会让模型看到一个
        它没提过的意图。
        """
        payload = args.get("intent")
        intent = (
            payload if isinstance(payload, SolveIntent) else SolveIntent.model_validate(payload)
        )
        return {
            "accepted": True,
            "iso_week": str(args.get("iso_week", "")),
            "scope_persons": intent.scope_persons,
            "scope_missions": intent.scope_missions,
            "freeze_policy": intent.freeze_policy,
            "pre_authorized_tiers": list(intent.pre_authorized_tiers),
            "rationale": str(args.get("rationale", "")),
        }

    def _translate_revision(args: dict[str, Any]) -> Any:
        """规则路径的修订翻译（v6 §7.3.4）。

        **认不出返回 `translated: false`，不瞎猜一个 `kind`。** 模型看到这个
        回执就知道该换一种说法或去问用户，而不是拿到一条编出来的约束。
        """
        utterance = str(args["utterance"])
        round_no = int(args.get("round_no", 1))
        constraint = rule_translate(
            utterance, round_no=round_no, plan=prev_plan, directory=directory
        )
        if constraint is None:
            return {
                "translated": False,
                "utterance": utterance,
                "note": "这句话没能翻译成六种增量约束之一，请换一种说法或指明架次号",
            }
        return {
            "translated": True,
            "kind": constraint.kind,
            "targets": list(constraint.targets),
            "params": constraint.params,
            "round_no": constraint.round_no,
            "echo": echo_text(constraint, plan=prev_plan, directory=directory),
        }

    def _check_authority(args: dict[str, Any]) -> Any:
        result = check_authority(
            int(args["requested_tier"]), str(args.get("actor_role") or user_role)
        )
        return {
            "tier": result.tier,
            "granted": result.granted,
            "required_role": result.required_role,
            "actor_role": result.actor_role,
            "reason": result.reason,
        }

    return {
        "resolve_person": _resolve_person,
        "resolve_aircraft": _resolve_aircraft,
        "resolve_week": _resolve_week,
        "ask_user": _ask_user,
        "escalate": _escalate,
        "estimate_scope": _estimate_scope,
        "assess_disruption": _assess_disruption,
        "propose_solve_intent": _propose_solve_intent,
        "translate_revision": _translate_revision,
        "check_authority": _check_authority,
    }


def _neutral_weights() -> Any:
    from backend.schemas.intent import ObjectiveWeights

    return ObjectiveWeights(progress=1.0, disruption=1.0, balance=1.0)


def route_tool_handlers(
    *, directory: EntityDirectory, today: date, sink: list[dict[str, Any]] | None = None
) -> Mapping[str, ToolHandler]:
    """意图路由那一行的子集（§7.7.2 第 1~2 行）。

    路由与 Planner 共用前五个工具，所以这里只是把 Planner 那份**按 ACL 行裁剪**
    —— 少给可以，多给不行。
    """
    allowed = ("resolve_person", "resolve_aircraft", "resolve_week", "ask_user", "escalate")
    handlers = planner_tool_handlers(directory=directory, today=today, sink=sink)
    return {name: handlers[name] for name in allowed}


__all__ = ["planner_tool_handlers", "route_tool_handlers"]
