"""LangGraph 编排层（v6 §7.4 / §7.5 / §9.2）。

```python
from backend.graph import FTSState, initial_state
from backend.graph.graph import GraphDeps, build_graph     # ← 刻意不在这里 re-export
```

## `build_graph` 为什么不在本文件里 re-export

`backend/graph/graph.py` import 了 `backend.skills_loader`（它是读知识层的那一侧），
而 `backend.nodes` 需要 `backend.graph.state` 与 `backend.graph.events`。
如果本文件 re-export 了 `graph.py`，就会出现

```
backend.nodes → backend.graph（__init__）→ backend.graph.graph → backend.skills_loader
```

这条**间接**依赖链，`.importlinter` 禁令二（`backend.nodes ↛ backend.skills_loader`）
当场报红——而且它报得对：铁律 3 要的是「求解链路读不到 skill」，间接读到也是读到。

所以本文件只 re-export **状态、事件、Store** 这三样纯数据设施，图的组装从
`backend.graph.graph` 直接取。
"""

from backend.graph.checkpointer import (
    CHECKPOINT_TABLES,
    checkpoint_dsn,
    setup_checkpoint_tables,
)
from backend.graph.events import emit, error, next_seq
from backend.graph.state import FTSState, get, initial_state, user_utterance
from backend.graph.store import (
    MEMORY_KINDS,
    MemoryKind,
    build_store,
    namespace,
    postgres_store,
    recall,
    remember,
    store_dsn,
)

__all__ = [
    "CHECKPOINT_TABLES",
    "MEMORY_KINDS",
    "FTSState",
    "MemoryKind",
    "build_store",
    "checkpoint_dsn",
    "emit",
    "error",
    "get",
    "initial_state",
    "namespace",
    "next_seq",
    "postgres_store",
    "recall",
    "remember",
    "setup_checkpoint_tables",
    "store_dsn",
    "user_utterance",
]
