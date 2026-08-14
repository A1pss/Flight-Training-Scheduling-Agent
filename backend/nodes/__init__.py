"""六个确定性节点（v6 §7.2.4）。

| 节点 | 模块 | 职责 |
|---|---|---|
| `compile_spec_node` | `nodes.compile_spec` | `ruleset.yaml + semantics.yaml + SolveIntent → ConstraintSpec`（含 S-01 展开、S-11 复训标记） |
| `solve_node` | `nodes.solve` | CP-SAT 求解、预算管理、warm start |
| `validate_node` | `nodes.validate` | 14 条独立校验 + 三层格式校验 |
| `resume_guard` | `nodes.resume_guard` | HITL 恢复时的快照陈旧性检查（FTS-3004） |
| `human_gate` | `nodes.human_gate` | `interrupt()` 人工确认 |
| `commit_plan_node` | `nodes.commit_plan` | 事务内：归档 + 推进进度 + 结算欠账 + **写 `last_done_date` 锚点** |

**这六个节点不经 Harness、不读 Skill、不注册为任何 LLM 组件的工具**
（CLAUDE.md 铁律 4；`.importlinter` 禁令二强制 `backend.nodes ↛ backend.skills_loader`）。

它们的签名统一是 `(state, session, **options) -> Command`——**依赖靠参数传，
不靠模块级单例**。这样既能脱离 LangGraph 单测，也避免把 `backend.graph.graph`
（它 import 了 `skills_loader`）拖进本包的依赖里。

## ⚠️ 本文件刻意不 re-export 任何节点

`backend.solver.solve` **反向依赖** `backend.nodes.compile_spec`（`SpecBundle`
是 M2-A 定的求解入口契约）。如果本文件 re-export `nodes.solve`，就会出现

```
backend.solver.solve → backend.nodes.compile_spec → backend.nodes（__init__）
                     → backend.nodes.solve → backend.solver.solve（半初始化）
```

—— `ImportError: cannot import name 'SolveOutcome' from partially initialized
module`，而且**只在 `backend.solver.solve` 被先导入时**才炸（`tests/golden/`
就是这个顺序）。所以节点一律从各自的子模块导入：

```python
from backend.nodes.solve import solve_node          # ✅
from backend.nodes import solve_node                # ❌ 循环
```

同一个理由让 `backend/graph/__init__.py` 也不 re-export `build_graph`。
"""

#: 六个确定性节点的名字。**这张表与 `harness.acl.FORBIDDEN_NODES` 必须逐字相等**
#: ——前者是图里真实存在的节点，后者是「不许注册为工具」的黑名单。两者漂移意味着
#: 新加的确定性节点没被加进禁令，于是它可以被注册成 LLM 工具（铁律 4 当场失效）。
#: 由 `tests/unit/test_nodes_and_components.py` 钉住。
DETERMINISTIC_NODE_NAMES: tuple[str, ...] = (
    "compile_spec",
    "solve",
    "validate",
    "resume_guard",
    "human_gate",
    "commit_plan",
)

__all__ = ["DETERMINISTIC_NODE_NAMES"]
