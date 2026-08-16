"""知识与长期记忆（v6 §6）。

## 三类记忆（§6.2），各占一个模块

| 类型 | 模块 | 存储 | 检索方式 |
|---|---|---|---|
| **语义**（事实） | :mod:`~backend.memory.semantic` | PG | **精确查询，不走向量** |
| **情景**（经历） | :mod:`~backend.memory.episodic` | PG + Chroma 摘要向量 | 三路召回 + RRF，叠加时间过滤 |
| **程序**（偏好） | :mod:`~backend.memory.procedural` | PG JSONB + LangGraph Store | key 前缀 + 语义 |

:mod:`~backend.memory.temporal` 横跨三类，实现 §6.4 的时效性与冲突消解：
时间过滤、同 key 多版本、写入冲突按来源可信度裁决、超期归档。

向量存储基础设施（Chroma 客户端、collection 定义、嵌入函数）在
:mod:`~backend.memory.chroma` 与 :mod:`~backend.memory.embeddings`。

⚠️ **`training_progress` 的推进不在本包**：那是 `commit_plan_node` 在人工确认
之后的事务里做的（v6 §7.7.2 注）。本包的 `semantic.progress_facts` 只**读**
那张表 —— 它是物化视图，真源是 `person_completed_missions`（§6.3.2）。
"""

from backend.memory.chroma import (
    ALL_COLLECTIONS,
    COLLECTION_DESCRIPTIONS,
    METADATA_SCHEMAS,
    build_client,
    collection_counts,
    field_map_of,
    init_collections,
    upsert_chunks,
)
from backend.memory.embeddings import BGEM3Embedder, Embedder, HashEmbedder, build_embedder
from backend.memory.temporal import (
    SOURCE_CONVERSATION,
    SOURCE_PG_FACT,
    SOURCE_PLAN_CONFIRMED,
    SOURCE_TRUST,
    MemoryConflict,
    VersionView,
    active_at,
    archive_horizon,
    detect_conflict,
    is_active_at,
    latest_version,
    rank_by_trust,
    trust_of,
)

__all__ = [
    "ALL_COLLECTIONS",
    "COLLECTION_DESCRIPTIONS",
    "METADATA_SCHEMAS",
    "SOURCE_CONVERSATION",
    "SOURCE_PG_FACT",
    "SOURCE_PLAN_CONFIRMED",
    "SOURCE_TRUST",
    "BGEM3Embedder",
    "Embedder",
    "HashEmbedder",
    "MemoryConflict",
    "VersionView",
    "active_at",
    "archive_horizon",
    "build_client",
    "build_embedder",
    "collection_counts",
    "detect_conflict",
    "field_map_of",
    "init_collections",
    "is_active_at",
    "latest_version",
    "rank_by_trust",
    "trust_of",
    "upsert_chunks",
]
