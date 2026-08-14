"""四个 LLM 节点（v6 §7.2.3）。

| # | 节点 | 调用形态 | 温度 |
|---|---|---|---|
| 1 | `route` 意图路由 | 单次分类，**仅规则未命中时触发** | 0.0 |
| 2 | `Planner` 求解规划器 | 单次调用（修订场景每轮一次） | 0.1 |
| 3 | `extract_llm` 自由文本抽取 | 单次受约束解码 | 0.0 |
| 4 | `explain_llm` + Critic | 循环，但**循环条件由确定性核验器判定** | 0.3 |

四个都是 **LLM 节点，不是 Agent**：单次调用，或循环条件由确定性代码控制
（v6 §7.1.4 的判定准则）。真正的 Agent 只有两个，在 `backend/agents/`。
"""

from backend.components.explain import (
    Explanation,
    FactIndex,
    build_fact_index,
    explain,
    fallback_text,
    verify_claim,
    verify_claims,
)
from backend.components.extract import ExtractionResult, extract_events, skill_context
from backend.components.planner import planner_node, rollback_revision
from backend.components.route import clarification_command, route_node

__all__ = [
    "Explanation",
    "ExtractionResult",
    "FactIndex",
    "build_fact_index",
    "clarification_command",
    "explain",
    "extract_events",
    "fallback_text",
    "planner_node",
    "rollback_revision",
    "route_node",
    "skill_context",
    "verify_claim",
    "verify_claims",
]
