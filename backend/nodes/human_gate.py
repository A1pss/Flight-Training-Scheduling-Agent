"""确定性节点 ⑤：`human_gate`（v6 §7.2.4 / §7.5）。

> `interrupt()` 人工确认。**控制流原语**，不是 LLM。

```python
def human_gate(state: FTSState):
    decision = interrupt({
        "workbook": state.workbook_path,
        "validation": state.validation.model_dump(),
        "explanation": state.explanation,
        "relaxation_tier": state.relaxation_tier,
        "blocked_items": state.blocked_items,
    })
    return {"human_decision": decision}
```

## 跨日恢复靠的是「`interrupt()` + Checkpointer」这一对

`interrupt()` 把当前状态写进 checkpoint 后**抛出**，图的执行到此为止；进程可以
退出、机器可以重启。人隔天回来时，用同一个 `thread_id` 传入
`Command(resume=<决策>)`，图从断点继续——**不重跑求解**。

所以 `interrupt()` 的载荷有一条硬要求：**必须可序列化**。放进去的是
`model_dump()` 之后的普通字典，不是 Pydantic 对象本身，更不是 `SpecBundle`
这种带着几万个字段的东西。

## 三种决策，三个去向

| 决策 | 去向 | 说明 |
|---|---|---|
| `APPROVE` | `commit_plan` | 归档 + 推进进度 + 结算欠账 + 写锚点 |
| `REVISE` | `planner` | 进入下一轮修订（无界循环，轮数由用户决定） |
| `REJECT` | `END` | 驳回，附意见。**不自动重排**——驳回的理由要人来定，猜一个重排方向等于浪费一轮 |

`authorized_tiers` 在这里第一次成为**真的授权**：Planner 那一步只做了「你这个
角色够不够格预授权」的过滤，人工门禁这一步是训练主任本人按下的确认。
"""

from __future__ import annotations

from typing import Any, cast

from langgraph.types import Command, interrupt

from backend.graph.events import emit
from backend.graph.state import FTSState, model_get, model_list
from backend.graph.state import get as state_get
from backend.schemas.common import ErrorItem, HumanDecision
from backend.schemas.intent import SolveIntent
from backend.schemas.plan import BlockedItem, SchedulePlan
from backend.schemas.validation import ValidationReport

#: 决策 → 下一跳。
DECISION_ROUTES: dict[str, str] = {
    "APPROVE": "commit_plan",
    "REVISE": "planner",
    "REJECT": "END",
}


def gate_payload(state: FTSState) -> dict[str, Any]:
    """`interrupt()` 的载荷（v6 §7.5 逐字段）。**只放可序列化的普通结构。**"""
    validation = model_get(state, "validation", ValidationReport)
    plan = model_get(state, "solution", SchedulePlan)
    return {
        "trace_id": state_get(state, "trace_id", ""),
        "workbook": state_get(state, "workbook_path", ""),
        "validation": validation.model_dump(mode="json") if validation is not None else None,
        "explanation": state_get(state, "explanation", ""),
        "relaxation_tier": int(state_get(state, "relaxation_tier", 0)),
        "blocked_items": [
            b.model_dump(mode="json") for b in model_list(state, "blocked_items", BlockedItem)
        ],
        "plan": {
            "plan_id": plan.plan_id,
            "iso_week": plan.iso_week,
            "sorties": len(plan.sorties),
            "debts": len(plan.debts),
            "content_sha256": plan.content_sha256,
        }
        if plan is not None
        else None,
        "errors": [e.model_dump(mode="json") for e in model_list(state, "errors", ErrorItem)],
        "ambiguities": list(state_get(state, "ambiguities", cast(list[dict[str, Any]], []))),
        "open_questions": list(
            getattr(model_get(state, "solve_intent", SolveIntent), "open_questions", []) or []
        ),
    }


def parse_decision(raw: Any, *, fallback_user: str = "unknown") -> HumanDecision:
    """把恢复时传进来的东西解析成 `HumanDecision`。

    容忍三种形态：`HumanDecision` 本身、字典、以及只给一个决策字符串。
    **认不出就抛**——把一个看不懂的输入默认成 `APPROVE` 是这套系统里最贵的
    默认值。
    """
    if isinstance(raw, HumanDecision):
        return raw
    if isinstance(raw, dict):
        payload = dict(raw)
        payload.setdefault("user_id", fallback_user)
        payload.setdefault("role", "scheduler")
        return HumanDecision.model_validate(payload)
    if isinstance(raw, str) and raw.upper() in DECISION_ROUTES:
        return HumanDecision(
            decision=cast(Any, raw.upper()), user_id=fallback_user, role="scheduler"
        )
    raise ValueError(
        f"无法解析人工决策：{raw!r}。必须是 APPROVE / REJECT / REVISE 之一，"
        "或一个含 decision 字段的对象"
    )


def human_gate(state: FTSState) -> Command[str]:
    """确定性节点 ⑤。**这是本图唯一会挂起的地方。**"""
    raw = interrupt(gate_payload(state))
    decision = parse_decision(raw, fallback_user=state_get(state, "user_id", "unknown"))
    goto = DECISION_ROUTES[decision.decision]

    update: dict[str, Any] = {
        "human_decision": decision,
        "needs_human": False,
        "trace_events": emit(
            state,
            "human_gate",
            "human_gate",
            {
                "decision": decision.decision,
                "user_id": decision.user_id,
                "role": decision.role,
                "authorized_tiers": list(decision.authorized_tiers),
                "comment": decision.comment,
            },
        ),
    }
    if decision.decision == "REVISE":
        # 修订轮次由用户决定，不由模型决定（v6 §7.1.2）。轮次在这里 +1，
        # planner 侧的 translate_revision 直接用它当 round_no。
        update["revision_round"] = int(state_get(state, "revision_round", 0)) + 1
    return Command(goto=goto, update=update)


__all__ = ["DECISION_ROUTES", "gate_payload", "human_gate", "parse_decision"]
