"""LLM Harness —— v6 §7.7 的八项职责。

对外只暴露装配一次调用需要的名字；实现细节留在各自模块里。

```python
from backend.harness import AgentSpec, ContextBlock, Harness

harness = Harness(snapshot_id="snap_x")
out = harness.call(
    AgentSpec(name="planner", tools=("resolve_person", "propose_solve_intent")),
    [ContextBlock(kind="summary", content=structured_summary("快照", {...}))],
)
```
"""

from backend.harness.acl import (
    ACL_MATRIX,
    DEFAULT_ACL,
    FORBIDDEN_NODES,
    PROBE_TOOL,
    WRITE_TOOL_ALLOWLIST,
    ToolACL,
)
from backend.harness.budget import (
    BudgetLedger,
    BudgetLimits,
    BudgetUsage,
    ProbeBudgetLimits,
)
from backend.harness.cache import (
    InMemoryCacheBackend,
    RedisCacheBackend,
    ToolResultCache,
    cache_key,
)
from backend.harness.context import (
    AssembledContext,
    ContextAssembler,
    ContextBlock,
    structured_summary,
)
from backend.harness.harness import Harness, HarnessStats, constrained_schema
from backend.harness.mode_selector import ModeSelector, ModeStats
from backend.harness.prompts import Prompt, PromptRegistry
from backend.harness.recorder import (
    ReplayResult,
    Trace,
    TraceMeta,
    TraceRecorder,
    load_trace,
    replay,
)
from backend.harness.registry import DEFAULT_REGISTRY, ToolNotBoundError, ToolRegistry
from backend.harness.tools import TOOL_CATALOG
from backend.harness.types import (
    ALL_COMPONENTS,
    AgentOutput,
    AgentSpec,
    ComponentName,
    FailureMode,
    ToolResult,
    ToolSpec,
    ValidatedCall,
    ValidationFailure,
)
from backend.harness.validation import (
    StaticEntityIndex,
    ToolCallValidator,
    build_error_feedback,
)

__all__ = [
    "ACL_MATRIX",
    "ALL_COMPONENTS",
    "DEFAULT_ACL",
    "DEFAULT_REGISTRY",
    "FORBIDDEN_NODES",
    "PROBE_TOOL",
    "TOOL_CATALOG",
    "WRITE_TOOL_ALLOWLIST",
    "AgentOutput",
    "AgentSpec",
    "AssembledContext",
    "BudgetLedger",
    "BudgetLimits",
    "BudgetUsage",
    "ComponentName",
    "ContextAssembler",
    "ContextBlock",
    "FailureMode",
    "Harness",
    "HarnessStats",
    "InMemoryCacheBackend",
    "ModeSelector",
    "ModeStats",
    "ProbeBudgetLimits",
    "Prompt",
    "PromptRegistry",
    "RedisCacheBackend",
    "ReplayResult",
    "StaticEntityIndex",
    "ToolACL",
    "ToolCallValidator",
    "ToolNotBoundError",
    "ToolRegistry",
    "ToolResult",
    "ToolResultCache",
    "ToolSpec",
    "Trace",
    "TraceMeta",
    "TraceRecorder",
    "ValidatedCall",
    "ValidationFailure",
    "build_error_feedback",
    "cache_key",
    "constrained_schema",
    "load_trace",
    "replay",
    "structured_summary",
]
