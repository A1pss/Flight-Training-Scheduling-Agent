"""两个 Agent（v6 §7.2.2 / §7.1.5）—— 系统里仅有的两处受控自治。

| # | Agent | 自治体现 | 边界 | 状态 |
|---|---|---|---|---|
| 1 | `KnowledgeAgent` 知识问答 | ReAct 循环，模型自主决定调哪几路、调几次、何时停（≤6 步） | 只读工具；**排班取数不经此路径** | **W8 交付** |
| 2 | `DiagnosisAgent` 冲突诊断 | 自主决定探测哪些约束组、跑几轮 `probe_solve` | 独立预算池；每条提案必经探针验证 | 本窗口交付 |

**两处自治都发生在求解之外**：Knowledge 服务问答链路，与排班无关；
Diagnosis 只在求解已判定 INFEASIBLE 之后进场，此时不存在待输出的方案。
"""

from backend.agents.diagnosis import (
    DIAGNOSIS_AGENT,
    DIAGNOSIS_TOOLS,
    DiagnosisOutcome,
    diagnosis_tool_handlers,
    run_diagnosis,
)

__all__ = [
    "DIAGNOSIS_AGENT",
    "DIAGNOSIS_TOOLS",
    "DiagnosisOutcome",
    "diagnosis_tool_handlers",
    "run_diagnosis",
]
