"""四阶段检索管线的装配（v6 §6.5.2）。

```
用户问题
   │
① 查询改写（唯一需要 LLM 的环节）        retrieval.rewrite
   │
② 多路并行召回（三路，互不阻塞）
   ├─ 路 A · 结构化：SQL / 递归 CTE      retrieval.structured
   ├─ 路 B · 稀疏：BM25                  retrieval.bm25
   └─ 路 C · 稠密：Chroma 向量           retrieval.vector
   │
③ RRF 融合（k=60）+ rerank Top-20 → Top-5   retrieval.rrf / retrieval.rerank
   │
④ 带引用生成 + 事实核验                  retrieval.generate
```

## 三路「可独立开关」是交付项，不是调试开关

出口标准要求三路能独立关掉，为 W13 的消融做准备（v6 §12.4 的三条消融：
去 SQL 精确路 / 去查询改写 / 去时间过滤）。所以开关落在
:class:`RetrievalConfig` 上，**每一路的关闭都是真关闭**（那一路一次都不跑，
不是跑完再丢弃）—— 否则消融测出来的延迟是假的，而 §12.4 的消融要量的正是
「拿掉它会怎样」。

## 「互不阻塞」在单机上的诚实说法

v6 §6.5.2 写的是「多路并行召回（三路，互不阻塞）」。本实现是**顺序执行**的：
三路之间没有任何数据依赖，谁先谁后不影响结果，但确实没有开线程。基准语料
规模下三路合计是毫秒量级（§5 的实测表），引入线程池换来的是竞态与不可复现
的风险，不划算。**「互不阻塞」在这里的落点是「不共享状态、任一路失败不影响
其余两路」**，不是并发执行 —— 这一条如实写在收工报告里，不含糊过去。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final

from sqlalchemy.orm import Session

from backend.core.config import Settings, get_settings
from backend.core.logging import get_logger
from backend.harness import Harness
from backend.memory import semantic
from backend.memory.temporal import is_active_at
from backend.retrieval.bm25 import Bm25Index
from backend.retrieval.corpus import Corpus, build_corpus
from backend.retrieval.documents import RetrievedDoc
from backend.retrieval.rerank import Reranker, RerankResult, rerank
from backend.retrieval.rewrite import ConversationTurn, RewriteOutcome, rewrite_query
from backend.retrieval.rrf import FusionEntry, fuse
from backend.retrieval.structured import FactAnswer, StructuredResult, structured_recall
from backend.retrieval.terms import Terminology
from backend.retrieval.vector import VectorIndex, build_vector_index
from backend.routing.entities import EntityDirectory
from backend.schemas.retrieval import RewrittenQuery

logger = get_logger(__name__)

#: 三路的名字。**顺序即优先级**：路 A 在最前，它的结果直接置顶（§6.5.4）
ROUTES: Final[tuple[str, str, str]] = ("structured", "bm25", "vector")


@dataclass(frozen=True)
class RetrievalConfig:
    """管线配置。三路开关 + 改写开关 + 时间过滤开关（消融用）。"""

    enable_structured: bool = True
    enable_bm25: bool = True
    enable_vector: bool = True
    #: 关掉改写 = v6 §12.4 消融的第二条
    enable_rewrite: bool = True
    #: 关掉时间过滤 = v6 §12.4 消融的第三条（「M1 探针会直接翻车」）
    enable_time_filter: bool = True
    rrf_k: int = 60
    route_top_k: int = 10
    fusion_top_k: int = 20
    rerank_top_k: int = 5
    #: 归档的情景记忆是否参与召回（§6.4 遗忘策略：默认不参与）
    include_archived: bool = False

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, **overrides: bool | int
    ) -> RetrievalConfig:
        cfg = settings or get_settings()
        base: dict[str, bool | int] = {
            "rrf_k": cfg.RRF_K,
            "route_top_k": cfg.ROUTE_TOP_K,
            "fusion_top_k": cfg.FUSION_TOP_K,
            "rerank_top_k": cfg.RERANK_TOP_K,
        }
        base.update(overrides)
        return cls(**base)  # type: ignore[arg-type]

    @property
    def enabled_routes(self) -> tuple[str, ...]:
        flags = {
            "structured": self.enable_structured,
            "bm25": self.enable_bm25,
            "vector": self.enable_vector,
        }
        return tuple(name for name in ROUTES if flags[name])


@dataclass(frozen=True)
class RetrievalResult:
    """一次检索的完整产物。"""

    rewrite: RewriteOutcome
    structured: StructuredResult
    #: 精排后的最终上下文：路 A 置顶 + 融合结果填充剩余位置
    contexts: tuple[RetrievedDoc, ...]
    fusion: tuple[FusionEntry, ...]
    per_route: dict[str, int]
    config: RetrievalConfig
    rerank_provider: str = ""
    #: 时间过滤滤掉的条数（§12.4 的「时效正确率」要看这个）
    filtered_by_time: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def query(self) -> RewrittenQuery:
        return self.rewrite.query

    @property
    def answers(self) -> tuple[FactAnswer, ...]:
        """路 A 算出来的确定性结论。关掉路 A 时为空。"""
        return self.structured.answers

    @property
    def needs_clarification(self) -> bool:
        """有歧义就必须反问，不允许下游自行挑一个继续。"""
        return self.query.needs_clarification


def retrieve(
    text: str,
    *,
    session: Session,
    snapshot_id: str,
    directory: EntityDirectory,
    today: date,
    as_of: date | None = None,
    harness: Harness | None = None,
    history: Sequence[ConversationTurn] = (),
    corpus: Corpus | None = None,
    vector_index: VectorIndex | None = None,
    reranker: Reranker | None = None,
    terminology: Terminology | None = None,
    config: RetrievalConfig | None = None,
    settings: Settings | None = None,
) -> RetrievalResult:
    """跑一次四阶段检索。

    `as_of` 决定时效过滤与「能不能飞」的判定基准；不给就取 `today`。
    **两者都由调用方传入**，本模块不调 `date.today()`（重放要它稳定，铁律 9）。
    """
    cfg = config or RetrievalConfig.from_settings(settings)
    moment = as_of or today
    notes: list[str] = []

    # 当前快照里实际存在的类别 / 跑道 / 空域 —— 术语表按它过滤（terms.py 口径 ②）
    missions = semantic.all_missions(session, snapshot_id)
    known_classes = sorted({m.mission_class for m in missions})
    known_runways = list(semantic.runway_ids(session, snapshot_id))
    known_airspaces = [a.airspace_id for a in semantic.airspace_facts(session, snapshot_id)]

    # ── ① 查询改写 ─────────────────────────────────────────────────
    if cfg.enable_rewrite:
        rewritten = rewrite_query(
            text,
            directory=directory,
            today=today,
            harness=harness,
            history=history,
            terminology=terminology,
            known_mission_classes=known_classes,
            known_runways=known_runways,
            known_airspaces=known_airspaces,
        )
    else:
        rewritten = _no_rewrite(text)
        notes.append("查询改写已关闭（消融配置）")

    # ── ② 三路召回，互不阻塞（任一路的空结果不影响其余两路）────────
    per_route: dict[str, int] = dict.fromkeys(ROUTES, 0)
    rankings: list[list[RetrievedDoc]] = []

    structured = StructuredResult()
    if cfg.enable_structured:
        structured = structured_recall(
            session,
            snapshot_id,
            query=rewritten.query.original_query,
            person_ids=[
                e.entity_id for e in rewritten.query.resolved_entities if e.kind == "person"
            ],
            aircraft_ids=[
                e.entity_id for e in rewritten.query.resolved_entities if e.kind == "aircraft"
            ],
            mission_ids=[
                e.entity_id for e in rewritten.query.resolved_entities if e.kind == "mission"
            ],
            mission_classes=rewritten.mission_classes,
            airspace_ids=rewritten.airspace_ids,
            as_of=moment,
        )
        per_route["structured"] = len(structured.docs)
        rankings.append(list(structured.docs))
    else:
        notes.append("路 A（SQL 精确通道）已关闭（消融配置）")

    working_corpus = corpus if corpus is not None else build_corpus(session, snapshot_id)
    searchable = working_corpus.filter(include_archived=cfg.include_archived)

    if cfg.enable_bm25:
        hits = Bm25Index.build(searchable).search(rewritten.bm25_query(), top_k=cfg.route_top_k)
        per_route["bm25"] = len(hits)
        rankings.append(hits)
    else:
        notes.append("路 B（BM25）已关闭（消融配置）")

    if cfg.enable_vector:
        index = vector_index or build_vector_index(
            searchable, backend=(settings or get_settings()).VECTOR_BACKEND
        )
        hits = index.search_many(rewritten.vector_queries(), top_k=cfg.route_top_k)
        per_route["vector"] = len(hits)
        rankings.append(hits)
    else:
        notes.append("路 C（向量）已关闭（消融配置）")

    # ── ③ 时间过滤 → RRF 融合 → 精排 ───────────────────────────────
    filtered = 0
    if cfg.enable_time_filter:
        pruned: list[list[RetrievedDoc]] = []
        for ranking in rankings:
            kept = [d for d in ranking if _valid_at(d, moment)]
            filtered += len(ranking) - len(kept)
            pruned.append(kept)
        rankings = pruned
    else:
        notes.append("时间过滤已关闭（消融配置）")

    fusion = fuse(rankings, k=cfg.rrf_k, top_k=cfg.fusion_top_k)
    ranked: RerankResult = rerank(
        rewritten.query.semantic_query or rewritten.query.original_query,
        [entry.doc for entry in fusion],
        top_k=cfg.rerank_top_k,
        reranker=reranker,
        settings=settings,
    )

    logger.info(
        "检索完成",
        routes=cfg.enabled_routes,
        per_route=per_route,
        fused=len(fusion),
        contexts=len(ranked.docs),
        filtered_by_time=filtered,
        rerank=ranked.provider,
    )
    return RetrievalResult(
        rewrite=rewritten,
        structured=structured,
        contexts=ranked.docs,
        fusion=tuple(fusion),
        per_route=per_route,
        config=cfg,
        rerank_provider=ranked.provider,
        filtered_by_time=filtered,
        notes=tuple(notes) + rewritten.notes + structured.notes,
    )


def _valid_at(doc: RetrievedDoc, at: date) -> bool:
    """时间过滤（§6.4「检索默认加时间过滤」）。

    **不带时效的文档一律保留**：规则原文与实体摘要句没有 `valid_from`，
    把它们当成「无效」会让整条管线在基准数据上召回为空。
    """
    if doc.valid_from is None and doc.valid_to is None:
        return True
    return is_active_at(_Window.of(doc), at)


@dataclass
class _Window:
    """把 `RetrievedDoc` 的时效字段适配成 `temporal.Versioned` 的形状。

    **是可变 dataclass 而不是 `frozen=True` + property**：`Versioned` 那几个
    成员在协议里是普通变量，用只读 property 去实现它们在 `--strict` 下不兼容
    （「expected settable variable, got read-only attribute」）。这里要的只是
    一个字段容器，没有不可变的需求。
    """

    memory_id: str
    valid_from: datetime
    valid_to: datetime | None
    superseded_by: str | None = None

    @classmethod
    def of(cls, doc: RetrievedDoc) -> _Window:
        return cls(
            memory_id=doc.doc_id,
            valid_from=_parse_ts(doc.valid_from) or datetime.min,
            valid_to=_parse_ts(doc.valid_to),
        )


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(raw), datetime.min.time())
        except ValueError:
            return None


def _no_rewrite(text: str) -> RewriteOutcome:
    """消融「去查询改写」时的产物：原句原样进三路，什么都不加。"""
    return RewriteOutcome(
        query=RewrittenQuery(
            original_query=text.strip(),
            semantic_query=text.strip(),
            sub_queries=[text.strip()],
        )
    )


__all__ = [
    "ROUTES",
    "RetrievalConfig",
    "RetrievalResult",
    "retrieve",
]
