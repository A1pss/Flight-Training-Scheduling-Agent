"""一级意图分类：正则规则表（v6 §7.2.1）。

`INTENT_RULES` **逐条照抄 v6 §7.2.1 的代码块**，包括顺序——顺序就是优先级：
「重新排一下何超的班」同时命中 reschedule 与 schedule 两条，先写的赢。
把 query 那条放最后，是因为它最松（`什么时候` / `为什么` 几乎出现在任何问句里），
提前就会把「什么时候能排完班」抢成问答。

> **收益**：约 70% 的请求省掉一次 LLM 调用；规则命中的分类**确定且可测**；
> LLM 只处理规则覆盖不到的表述。

## 六类意图各自的去向

| 意图 | 下一跳 | 说明 |
|---|---|---|
| `schedule` / `reschedule` | `planner` | 本窗口交付的完整排班链路 |
| `query` | `END` | 问答由 `KnowledgeAgent` 承接（W8）。本窗口把意图与槽位解析完整交出 |
| `ingest` | `END` | 摄取走 `/api/v1/ingest`（M1 已交付），不在对话图内跑管线 |
| `export` | `END` | 导出走 `/api/v1/schedule/{id}/export`（M3 已交付） |
| `unknown` | `human_gate` | 宁可问，不可猜 |

**后三类落到 `END` 不是占位**：它们各自的执行入口在 v6 §9.1 里就是独立端点，
对话图的职责到「把这句话判成哪一类、里面提到了谁」为止。图外的调度由 API 层
按 `state["intent"]` 与 `state["request"]` 分发，接口在本模块定死。
"""

from __future__ import annotations

import re
from typing import Final

from backend.schemas.intent import Intent

#: v6 §7.2.1 的规则表，逐条照抄（含顺序）。
INTENT_RULES: Final[tuple[tuple[str, Intent], ...]] = (
    (r"重新排|重排|改.*班|调整.*计划", "reschedule"),
    (r"排班|安排|生成.*(计划|时间表)", "schedule"),
    (r"上传|导入|读取.*文件", "ingest"),
    (r"导出|下载|输出.*(表|excel)", "export"),
    (r"(是|的)?(信息|资质|进度|什么时候|为什么)", "query"),
)

#: 预编译。`IGNORECASE` 只对 `excel` 那一条有意义，中文不受影响。
_COMPILED: Final[tuple[tuple[re.Pattern[str], Intent], ...]] = tuple(
    (re.compile(pattern, re.IGNORECASE), intent) for pattern, intent in INTENT_RULES
)

#: 意图 → 下一跳节点名。`END` 由 `graph.py` 翻译成 LangGraph 的终止符。
INTENT_NEXT_NODE: Final[dict[Intent, str]] = {
    "schedule": "planner",
    "reschedule": "planner",
    "query": "knowledge",
    "ingest": "END",
    "export": "END",
    "unknown": "human_gate",
}

#: 图外承接方（写进 `handoff` 轨迹事件，让「为什么这里就结束了」查得到）。
INTENT_HANDOFF: Final[dict[Intent, str]] = {
    # `query` 不在这里了 —— M5 把 `KnowledgeAgent` 接进了图（`INTENT_NEXT_NODE`
    # 那一行从 "END" 改成 "knowledge"），它不再是图外承接。
    "ingest": "POST /api/v1/ingest（M1 摄取管线）",
    "export": "GET /api/v1/schedule/{id}/export（M3 报表层）",
}

#: 需要走排班链路的两类意图。
SCHEDULING_INTENTS: Final[frozenset[Intent]] = frozenset({"schedule", "reschedule"})


def match_rule(text: str) -> Intent | None:
    """一级规则匹配。命中返回意图，未命中返回 None（交给 LLM 兜底）。"""
    for pattern, intent in _COMPILED:
        if pattern.search(text):
            return intent
    return None


def next_node_for(intent: Intent) -> str:
    """意图 → 下一跳。未知意图一律去人工门禁，不猜。"""
    return INTENT_NEXT_NODE.get(intent, "human_gate")


__all__ = [
    "INTENT_HANDOFF",
    "INTENT_NEXT_NODE",
    "INTENT_RULES",
    "SCHEDULING_INTENTS",
    "match_rule",
    "next_node_for",
]
