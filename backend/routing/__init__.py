"""两级意图路由（v6 §7.2.1）：规则分类器 + LLM 兜底 + 确定性实体消解。

```python
from backend.routing import EntityDirectory, classify_intent

d = classify_intent("给何超排下周的班", directory=directory, today=date(2026, 1, 5))
d.intent        # "schedule"（一级规则命中，0 次 LLM 调用）
d.next_node     # "planner"
d.ambiguities   # 消解不了的表述，非空即触发反问
```
"""

from backend.routing.classify import (
    INTENT_OUTPUT_SCHEMA,
    INTENT_VALUES,
    ROUTE_AGENT,
    IntentResult,
    build_request,
    classify_intent,
    llm_classify,
    scan_slots,
)
from backend.routing.entities import (
    Candidate,
    EntityDirectory,
    Resolution,
    collect_ambiguities,
    directory_from_session,
    levenshtein,
    monday_of,
    resolve_aircraft,
    resolve_mission,
    resolve_person,
    resolve_week,
    week_start_of,
)
from backend.routing.rules import (
    INTENT_HANDOFF,
    INTENT_NEXT_NODE,
    INTENT_RULES,
    SCHEDULING_INTENTS,
    match_rule,
    next_node_for,
)

__all__ = [
    "INTENT_HANDOFF",
    "INTENT_NEXT_NODE",
    "INTENT_OUTPUT_SCHEMA",
    "INTENT_RULES",
    "INTENT_VALUES",
    "ROUTE_AGENT",
    "SCHEDULING_INTENTS",
    "Candidate",
    "EntityDirectory",
    "IntentResult",
    "Resolution",
    "build_request",
    "classify_intent",
    "collect_ambiguities",
    "directory_from_session",
    "levenshtein",
    "llm_classify",
    "match_rule",
    "monday_of",
    "next_node_for",
    "resolve_aircraft",
    "resolve_mission",
    "resolve_person",
    "resolve_week",
    "scan_slots",
    "week_start_of",
]
