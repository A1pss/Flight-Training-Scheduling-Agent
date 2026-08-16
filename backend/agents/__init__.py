"""两个 Agent（v6 §7.2.2 / §7.1.5）—— 系统里仅有的两处受控自治。

| # | Agent | 自治体现 | 边界 |
|---|---|---|---|
| 1 | `KnowledgeAgent` 知识问答 | ReAct 循环，模型自主决定调哪几路、调几次、何时停（**≤6 步**） | 只读工具；**排班取数不经此路径** |
| 2 | `DiagnosisAgent` 冲突诊断 | 自主决定探测哪些约束组、跑几轮 `probe_solve` | 独立预算池；每条提案必经探针验证 |

**两处自治都发生在求解之外**：Knowledge 服务问答链路，与排班无关；
Diagnosis 只在求解已判定 INFEASIBLE 之后进场，此时不存在待输出的方案。

两者都**没有 LLM 也能工作**：Knowledge 退回确定性的四阶段检索管线，
Diagnosis 退回 M2-A 的确定性诊断四步。自治是加在能力之上的一层，不是能力本身。
"""

from backend.agents.diagnosis import (
    DIAGNOSIS_AGENT,
    DIAGNOSIS_TOOLS,
    DiagnosisOutcome,
    diagnosis_tool_handlers,
    run_diagnosis,
)
from backend.agents.knowledge import (
    KNOWLEDGE_AGENT,
    KNOWLEDGE_MAX_STEPS,
    KNOWLEDGE_TOOLS,
    KnowledgeOutcome,
    ask,
    knowledge_tool_handlers,
)

__all__ = [
    "DIAGNOSIS_AGENT",
    "DIAGNOSIS_TOOLS",
    "KNOWLEDGE_AGENT",
    "KNOWLEDGE_MAX_STEPS",
    "KNOWLEDGE_TOOLS",
    "DiagnosisOutcome",
    "KnowledgeOutcome",
    "ask",
    "diagnosis_tool_handlers",
    "knowledge_tool_handlers",
    "run_diagnosis",
]
