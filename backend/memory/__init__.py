"""知识与长期记忆（v6 §6）。

M1 交付其中的**向量存储基础设施**：Chroma 嵌入式客户端、collection 定义与各自
的 metadata schema，以及跑在 CPU 上的嵌入函数。

`progress.py` / `episodic.py` / `procedural.py` / `store.py`（v6 §8 目录）的
读写逻辑分属 M2 与 M4b 窗口 —— 它们要写的表 M1 已经建好（`training_progress` /
`episodic_memories` / `procedural_memories`，见 :mod:`backend.models`）。
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

__all__ = [
    "ALL_COLLECTIONS",
    "COLLECTION_DESCRIPTIONS",
    "METADATA_SCHEMAS",
    "BGEM3Embedder",
    "Embedder",
    "HashEmbedder",
    "build_client",
    "build_embedder",
    "collection_counts",
    "field_map_of",
    "init_collections",
    "upsert_chunks",
]
