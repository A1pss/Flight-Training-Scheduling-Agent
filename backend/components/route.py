"""LLM 节点 ①：`route` 意图路由（v6 §7.2.3 / §7.5）。

```python
def route(state: FTSState) -> Command:
    d = classify_intent(state.messages[-1].content, state)
    if d.source == "llm" and d.confidence < CONFIDENCE_THRESHOLD:   # 宁可问，不可猜
        return Command(goto="human_gate", update={"needs_human": True, ...})
    return Command(goto=d.next_node, update={...})
```

## 它有两个入口，不是一个

| 入口 | 触发 | 行为 |
|---|---|---|
| **首次进入** | `START → route` | 两级分类 + 槽位消解 |
| **Planner 回退** | `planner` 判定 `open_questions` 非空 | **不重新分类**，组织追问后交人工门禁 |

第二个入口是 v6 §7.5「动态跳转只发生在三处」里的第二处。回退时重新跑一遍分类
是错的：意图早就定了，回来是因为缺信息，再分类一次只会把「缺信息」变成
「意图又变了」。

## 置信度阈值只对 LLM 兜底路径生效

规则命中的 `confidence=1.0` 是确定性事实。拿一个**未拟合期的保守占位值**
（`CONFIDENCE_THRESHOLD`，默认 0.75）去卡它，等于让一个还没校准的数字否决
一条可测的规则。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from langgraph.types import Command

from backend.core.config import Settings, get_settings
from backend.graph.events import emit, emit_all
from backend.graph.state import FTSState, model_get, user_utterance
from backend.graph.state import get as state_get
from backend.harness import Harness
from backend.planner.calibration import ConfidenceCalibrator
from backend.planner.intent import ClarificationRequest
from backend.routing.classify import IntentResult, classify_intent
from backend.routing.entities import EntityDirectory, week_start_of
from backend.routing.rules import INTENT_HANDOFF
from backend.schemas.intent import SchedulingRequest, SolveIntent


def clarification_command(state: FTSState) -> Command[str]:
    """Planner 回退路径：组织追问，交人工门禁。**不重新分类。**"""
    intent = model_get(state, "solve_intent", SolveIntent)
    request = ClarificationRequest(
        questions=tuple(intent.open_questions) if intent is not None else (),
        ambiguities=tuple(state_get(state, "ambiguities", [])),
    )
    return Command(
        goto="human_gate",
        update={
            "needs_human": True,
            "needs_clarification": False,
            "explanation": request.as_text(),
            "trace_events": emit(
                state,
                "route",
                "negotiation",
                {
                    "reason": "planner_open_questions",
                    "questions": list(request.questions),
                    "ambiguities": len(request.ambiguities),
                },
            ),
        },
    )


def route_node(
    state: FTSState,
    *,
    directory: EntityDirectory,
    today: date,
    harness: Harness | None = None,
    calibrator: ConfidenceCalibrator | None = None,
    settings: Settings | None = None,
) -> Command[str]:
    """LLM 节点 ①。规则命中时**一次 LLM 都不调**。"""
    if state_get(state, "needs_clarification", False):
        return clarification_command(state)

    cfg = settings or get_settings()
    text = user_utterance(state)
    decision = classify_intent(
        text,
        directory=directory,
        today=today,
        harness=harness,
        calibrator=calibrator,
        settings=cfg,
    )

    update: dict[str, Any] = {
        "intent": decision.intent,
        "request": decision.request,
        "intent_confidence": decision.confidence,
        "ambiguities": list(decision.ambiguities),
        "trace_events": emit(state, "route", "decision", _decision_payload(decision)),
    }
    if decision.errors:
        update["errors"] = list(decision.errors)

    week_start = _week_start(decision)
    if week_start is not None:
        update["week_start"] = week_start

    # ① 有歧义就问 —— 「郝超」到底是高超还是何超，不自行选择（v6 §7.2.1 末段）
    if decision.ambiguities:
        return Command(
            goto="human_gate",
            update={
                **update,
                "needs_human": True,
                "explanation": ClarificationRequest(
                    ambiguities=tuple(decision.ambiguities)
                ).as_text(),
            },
        )

    # ② 置信度不足就问（只对 LLM / 降级路径生效）
    if decision.below_threshold(cfg.CONFIDENCE_THRESHOLD):
        return Command(
            goto="human_gate",
            update={
                **update,
                "needs_human": True,
                "explanation": (
                    f"我不太确定您这句话的意图（置信度 {decision.confidence:.2f}，"
                    f"阈值 {cfg.CONFIDENCE_THRESHOLD:.2f}）。"
                    "请直接说明是要「排班 / 重排 / 查询 / 上传 / 导出」中的哪一件。"
                ),
            },
        )

    # ③ 图外承接的三类意图：把意图与槽位交出去，链路到此为止
    handoff = INTENT_HANDOFF.get(decision.intent)
    if handoff is not None:
        return Command(
            goto=decision.next_node,
            update={
                **update,
                # 一个节点写两条事件 → 用 `emit_all` 保证序号连续（见 events.py）
                "trace_events": emit_all(
                    state,
                    [
                        ("route", "decision", _decision_payload(decision)),
                        (
                            "route",
                            "handoff",
                            {"intent": decision.intent, "handled_by": handoff},
                        ),
                    ],
                ),
            },
        )

    return Command(goto=decision.next_node, update=update)


def _decision_payload(decision: IntentResult) -> dict[str, Any]:
    return {
        "intent": decision.intent,
        "source": decision.source,
        "confidence": round(decision.confidence, 4),
        "agreement": round(decision.agreement, 4),
        "llm_calls": decision.llm_calls,
        "ambiguities": len(decision.ambiguities),
        "next_node": decision.next_node,
    }


def _week_start(decision: IntentResult) -> str | None:
    """把消解出的周次落成 `week_start`（ISO 日期串）。"""
    request = decision.request
    if isinstance(request, SchedulingRequest) and request.week_start is not None:
        return request.week_start.isoformat()
    if request is not None and request.iso_week:
        return week_start_of(request.iso_week).isoformat()
    return None


__all__ = ["clarification_command", "route_node"]
