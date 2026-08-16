"""Chroma 嵌入式初始化与 collection 定义（v6 §6.1）。

三个 collection 按 v6 §6.1 的分工命名（**规则原文 / 实体摘要句 / 历史报告**），
外加一个承载 §5.3「情况文件」与「会议纪要/通知」两种切分策略产物的
`situation_docs` —— 那两类文档 §6.1 的三分法没有覆盖，硬塞进历史报告会污染
那一路召回。

**PG 是事实唯一真源，Chroma 只是索引。** 每条实体摘要句的 metadata 里都带
`field_map`（含 `table` + `pk`），命中后一律回 PG 取权威值。所以这里的
`metadata` schema 是契约的一部分，由 :data:`METADATA_SCHEMAS` 声明并在写入时
校验 —— 元数据字段名写错，检索侧的过滤会静默失效，那是最难查的一类 bug。

**嵌入不交给 Chroma 的默认实现**（它会去下载 ONNX 模型，违反离线要求）：
向量由 :mod:`backend.memory.embeddings` 显式算好后传入。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import chromadb
from chromadb.api import ClientAPI
from chromadb.config import Settings as ChromaSettings

from backend.core.config import get_settings
from backend.core.errors import IngestionError
from backend.core.logging import get_logger
from backend.memory.collections import (
    ALL_COLLECTIONS,
    COLLECTION_DESCRIPTIONS,
    COLLECTION_ENTITIES,
    COLLECTION_EPISODIC,
    COLLECTION_REPORTS,
    COLLECTION_RULES,
    COLLECTION_SITUATIONS,
)
from backend.memory.embeddings import Embedder, build_embedder

if TYPE_CHECKING:
    from backend.ingestion.chunkers import Chunk

logger = get_logger(__name__)

#: 每个 collection 的 metadata 契约：必填键 → 允许的 Python 类型
METADATA_SCHEMAS: Final[dict[str, dict[str, tuple[type, ...]]]] = {
    COLLECTION_RULES: {
        "rule_id": (int,),
        "hard_soft": (str,),
        "ruleset_version": (str,),
        "title": (str,),
    },
    COLLECTION_ENTITIES: {
        "entity_type": (str,),
        "entity_id": (str,),
        "snapshot_id": (str,),
        "field_map": (str,),  # JSON 序列化后存入（Chroma 只接受标量）
    },
    COLLECTION_REPORTS: {
        "week": (str,),
        "plan_version": (int,),
        "status": (str,),
        "section": (str,),
    },
    COLLECTION_SITUATIONS: {
        "doc_id": (str,),
        "page": (int,),
    },
    COLLECTION_EPISODIC: {
        "memory_id": (str,),
        "session_id": (str,),
        "kind": (str,),
        # ISO 字符串。Chroma 的 metadata 只吃标量，时间过滤在 PG 侧做
        # （§6.4：PG 存权威内容，Chroma 只存摘要向量）
        "valid_from": (str,),
        "archived": (bool,),
    },
}

#: METADATA_SCHEMAS 必须与 collection 清单一一对应，漏一个就是「写得进去、
#: 过滤不出来」，这里在 import 时就钉死。
assert set(METADATA_SCHEMAS) == set(ALL_COLLECTIONS), "METADATA_SCHEMAS 与 ALL_COLLECTIONS 不一致"

#: Chroma 的距离度量。摘要句与查询都做了 L2 归一化，余弦是正确选择。
_HNSW_SPACE: Final[str] = "cosine"


def build_client(path: Path | None = None) -> ClientAPI:
    """嵌入式（PersistentClient）Chroma。**关掉遥测**，全离线部署不许出网。"""
    target = path or get_settings().CHROMA_PATH
    target.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(target),
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
    )


def init_collections(
    client: ClientAPI | None = None, *, embedder: Embedder | None = None
) -> dict[str, Any]:
    """幂等地建好全部 collection，返回 {名字: collection}。"""
    api = client or build_client()
    emb = embedder or build_embedder()
    collections: dict[str, Any] = {}
    for name in ALL_COLLECTIONS:
        collections[name] = api.get_or_create_collection(
            name=name,
            metadata={
                "description": COLLECTION_DESCRIPTIONS[name],
                "embedder": emb.name,
                "dim": emb.dim,
                "hnsw:space": _HNSW_SPACE,
            },
        )
    logger.info("Chroma collection 就绪", collections=list(collections), embedder=emb.name)
    return collections


def _flatten_metadata(
    collection: str, metadata: dict[str, Any]
) -> dict[str, str | int | float | bool]:
    """Chroma 的 metadata 只接受标量，嵌套结构序列化为 JSON 字符串。

    校验必填键与类型 —— 字段名写错会让检索侧的过滤静默失效。
    """
    flat: dict[str, str | int | float | bool] = {}
    for key, value in metadata.items():
        if isinstance(value, str | bool | int | float):
            flat[key] = value
        else:
            flat[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)

    schema = METADATA_SCHEMAS.get(collection)
    if schema is None:
        raise IngestionError(
            f"未知的 collection：{collection}",
            details={"collection": collection, "known": list(ALL_COLLECTIONS)},
        )
    missing = [k for k in schema if k not in flat]
    if missing:
        raise IngestionError(
            f"collection {collection} 的 metadata 缺少必填键 {missing}",
            details={"collection": collection, "missing": missing, "provided": sorted(flat)},
        )
    for key, types in schema.items():
        if not isinstance(flat[key], types):
            raise IngestionError(
                f"collection {collection} 的 metadata 键 {key} 类型应为 "
                f"{[t.__name__ for t in types]}，实际 {type(flat[key]).__name__}",
                details={"collection": collection, "key": key, "value": str(flat[key])[:200]},
            )
    return flat


def upsert_chunks(
    chunks: Sequence[Chunk],
    *,
    client: ClientAPI | None = None,
    embedder: Embedder | None = None,
) -> dict[str, int]:
    """把 chunk 写进对应 collection，返回 {collection: 写入条数}。

    向量在这里显式算好传入，**不让 Chroma 自己去下嵌入模型**（离线要求）。

    ⚠️ **顺序陷阱（M1 踩过，实测复现）**：必须**先把全部向量算完，再创建
    Chroma client**。反过来（先建 client 再 `SentenceTransformer(...)`）会在本机
    环境下让进程直接 `Segmentation fault (core dumped)`，连 Python traceback 都
    没有 —— chromadb 与 torch 各自带的原生运行时在同一进程里初始化会打架。
    所以下面的两步顺序是**功能性要求，不是风格偏好**，改回去会当场崩。

    同理，调用方若自己传 `client`，请确保它是在嵌入模型加载**之后**才创建的。
    """
    emb = embedder or build_embedder()

    grouped: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk.collection, []).append(chunk)
    unknown = sorted(set(grouped) - set(ALL_COLLECTIONS))
    if unknown:
        raise IngestionError(
            f"chunk 指向未定义的 collection：{unknown}",
            details={"collections": unknown, "known": list(ALL_COLLECTIONS)},
        )

    # ① 先算向量（此时进程里还没有 Chroma 的原生运行时）
    vectors = {name: emb.embed([c.text for c in items]) for name, items in grouped.items()}
    # ② 再建 client / collection
    api = client or build_client()
    collections = init_collections(api, embedder=emb)

    written: dict[str, int] = {}
    for name, items in grouped.items():
        collections[name].upsert(
            ids=[c.chunk_id for c in items],
            documents=[c.text for c in items],
            embeddings=vectors[name],
            metadatas=[_flatten_metadata(name, c.metadata) for c in items],
        )
        written[name] = len(items)
    logger.info("Chroma 写入完成", written=written, embedder=emb.name)
    return written


def collection_counts(client: ClientAPI | None = None) -> dict[str, int]:
    """各 collection 的条数，落库核对用。"""
    api = client or build_client()
    return {name: api.get_or_create_collection(name).count() for name in ALL_COLLECTIONS}


def field_map_of(metadata: dict[str, Any]) -> dict[str, Any]:
    """把 metadata 里 JSON 化的 `field_map` 还原成字典。"""
    raw = metadata.get("field_map")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    parsed = json.loads(str(raw))
    return parsed if isinstance(parsed, dict) else {}


__all__ = [
    "ALL_COLLECTIONS",
    "COLLECTION_DESCRIPTIONS",
    "METADATA_SCHEMAS",
    "build_client",
    "collection_counts",
    "field_map_of",
    "init_collections",
    "upsert_chunks",
]
