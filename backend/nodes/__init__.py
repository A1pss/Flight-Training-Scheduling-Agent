"""六个确定性节点（v6 §7.2.4）。

| 节点 | 职责 |
|---|---|
| `compile_spec_node` | `ruleset.yaml + semantics.yaml + SolveIntent → ConstraintSpec`（含 S-01 展开、S-11 复训标记） |
| `solve_node` | CP-SAT 求解、预算管理、warm start |
| `validate_node` | 14 条独立校验 + 三层格式校验 |
| `resume_guard` | HITL 恢复时的快照陈旧性检查（FTS-3004） |
| `human_gate` | `interrupt()` 人工确认 |
| `commit_plan_node` | 事务内：归档 + 推进进度 + 结算欠账 + **写 `last_done_date` 锚点** |

**这六个节点不经 Harness、不读 Skill、不注册为任何 LLM 组件的工具**
（CLAUDE.md 铁律 4；`.importlinter` 禁令二强制 `backend.nodes ↛ backend.skills_loader`）。

它们的签名统一是 `(state, session, **options) -> Command`——**依赖靠参数传，
不靠模块级单例**。这样既能脱离 LangGraph 单测，也避免把 `backend.graph.graph`
（它 import 了 `skills_loader`）拖进本包的依赖里。
"""

from backend.nodes.commit_plan import CommitResult, ProgressAdvance, commit_plan, commit_plan_node
from backend.nodes.compile_spec import (
    SpecBundle,
    bundle_from_spec,
    compile_spec,
    compile_spec_node,
    default_intent,
    intent_from_spec,
)
from backend.nodes.human_gate import DECISION_ROUTES, gate_payload, human_gate, parse_decision
from backend.nodes.resume_guard import StalenessVerdict, check_staleness, resume_guard
from backend.nodes.solve import run_solve, solve_node
from backend.nodes.validate import inject_nogoods, validate_node

#: 六个确定性节点的名字。**这张表与 `harness.acl.FORBIDDEN_NODES` 必须逐字相等**
#: ——前者是图里真实存在的节点，后者是「不许注册为工具」的黑名单。两者漂移意味着
#: 新加的确定性节点没被加进禁令，于是它可以被注册成 LLM 工具（铁律 4 当场失效）。
DETERMINISTIC_NODE_NAMES: tuple[str, ...] = (
    "compile_spec",
    "solve",
    "validate",
    "resume_guard",
    "human_gate",
    "commit_plan",
)

__all__ = [
    "DECISION_ROUTES",
    "DETERMINISTIC_NODE_NAMES",
    "CommitResult",
    "ProgressAdvance",
    "SpecBundle",
    "StalenessVerdict",
    "bundle_from_spec",
    "check_staleness",
    "commit_plan",
    "commit_plan_node",
    "compile_spec",
    "compile_spec_node",
    "default_intent",
    "gate_payload",
    "human_gate",
    "inject_nogoods",
    "intent_from_spec",
    "parse_decision",
    "resume_guard",
    "run_solve",
    "solve_node",
    "validate_node",
]
