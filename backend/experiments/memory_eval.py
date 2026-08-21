"""实验三 · 长期记忆与检索（v6 §12.4）的检索层评测。

## 三件与 M9-A 交接面直接相关的事

**① 算召回前两侧 id 都要过 `canonical_doc_id()`（`Z-30`）。**
路 A 发 `pg:persons:P04`、语料发 `ent:person:P04`，是同一个实体的两种形态。
不归一的话语义类最强的那一路命中会被全判成未召回，而 Recall@5 是**验收主指标**。

**② 程序记忆没有 doc id，本模块补那个适配（M9-A §7 第 1 条）。**
`preference_docs()` 只返回句子。这里按 `memory_320` 的约定发
`proc:<namespace>/<key>`。⚠️ 同时要照实说清楚：程序记忆**不经过 `retrieve()`**
——它由 `memory.search` 工具取 `list_preferences(at=...)` 的**前 top_k 条**，
**没有按查询排序**。所以「程序类 Recall@5」量的是「有效期过滤对不对 + 条数够不够
挤进前 5」，不是排序质量。报告里必须这么写，否则那个数会被误读成检索质量。

**③ `absent` 探针是负例，不进 Recall 的分母。**
正确行为是一条都不召回。把它们混进分母会让 Recall 无端变低；单独统计误召回率。

## 情景记忆要先落库

320 条里 200 条问的是情景/程序记忆，而 M9-A 跑完探针后**逐行清理**了。
本模块跑之前先 `seed_timeline()`，跑完还原并复核计数 —— 与 `run_probes.py`
同一套约定（往真库写过就要还原，并且要验证还原到位）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as _time
from typing import Any

from sqlalchemy.orm import Session

from backend.datasets.schemas import canonical_doc_id
from backend.memory.procedural import list_preferences
from backend.retrieval.pipeline import RetrievalConfig, retrieve

#: 与 `memory_320` 的约定一致的程序记忆召回单位。
PROC_ID_TEMPLATE = "proc:{namespace}/{key}"


def preference_doc_ids(rows: Sequence[Any]) -> list[str]:
    """M9-A §7 第 1 条点名要补的那个适配。

    `preference_docs()` 只发句子，这里发与之**同序**的 id，
    两者按下标对齐即可。
    """
    return [PROC_ID_TEMPLATE.format(namespace=r.namespace, key=r.key) for r in rows]


@dataclass
class ProbeOutcome:
    """一条探针的检索观测。"""

    item_id: str
    memory_type: str
    probe_kind: str
    variant: str
    expected_doc_ids: list[str]
    retrieved_doc_ids: list[str] = field(default_factory=list)
    #: 命中在 Top-5 里的名次（1 起算）；未命中为 0
    first_hit_rank_at5: int = 0
    #: 命中在 Top-10 里的名次，用于 MRR@10
    first_hit_rank_at10: int = 0
    filtered_by_time: int = 0
    rewrite_used: bool = False
    wall_s: float = 0.0
    error: str = ""

    @property
    def is_absent_probe(self) -> bool:
        """负例：正确行为是一条都不召回。"""
        return self.probe_kind == "absent"

    @property
    def hit_at_5(self) -> bool:
        return self.first_hit_rank_at5 > 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _first_hit_rank(retrieved: Sequence[str], expected: Sequence[str], *, limit: int) -> int:
    """归一化之后，期望 id 第一次出现在第几名（1 起算，没有就是 0）。"""
    gold = {canonical_doc_id(e) for e in expected}
    for rank, doc_id in enumerate(retrieved[:limit], start=1):
        if canonical_doc_id(doc_id) in gold:
            return rank
    return 0


def run_probe(
    item: Mapping[str, Any],
    *,
    session: Session,
    snapshot_id: str,
    directory: Any,
    today: date,
    corpus: Any,
    vector_index: Any,
    reranker: Any,
    harness: Any,
    config: RetrievalConfig,
    variant: str,
) -> ProbeOutcome:
    """跑一条探针的**检索层**（不生成回答）。"""
    as_of = date.fromisoformat(str(item["as_of"])) if item.get("as_of") else today
    out = ProbeOutcome(
        item_id=str(item["item_id"]),
        memory_type=str(item["memory_type"]),
        probe_kind=str(item["probe_kind"]),
        variant=variant,
        expected_doc_ids=[str(x) for x in (item.get("expected_doc_ids") or [])],
    )
    started = time.monotonic()

    try:
        if out.memory_type == "procedural":
            # ★ 程序记忆不走 `retrieve()` —— 它由 `memory.search` 取
            #   `list_preferences(at=...)` 的前 top_k 条，**不按查询排序**。
            #   时间过滤消融在这里的落点就是 `at` 传不传。
            # `list_preferences` 的 `at` 是**必填 datetime**，没有「不过滤」这一档。
            # 关掉时间过滤 = 用一个远期时点取「永远是最新版」，这与 §12.4 消融三
            # 想验的东西一致：拿掉版本管理之后，问哪个时点都只会拿到当前版本。
            moment = (
                datetime.combine(as_of, _time.min)
                if config.enable_time_filter
                else datetime.combine(date(2999, 12, 31), _time.min)
            )
            rows = list_preferences(session, at=moment)
            retrieved = preference_doc_ids(rows)
        else:
            result = retrieve(
                str(item["query"]),
                session=session,
                snapshot_id=snapshot_id,
                directory=directory,
                today=today,
                as_of=as_of,
                harness=harness if config.enable_rewrite else None,
                corpus=corpus,
                vector_index=vector_index,
                reranker=reranker,
                config=config,
            )
            retrieved = [d.doc_id for d in result.contexts]
            out.filtered_by_time = result.filtered_by_time
            # `RewrittenQuery` 没有「改写过没有」的布尔位；用它是否产出子查询
            # 或语义化表述作为观测口（两者都是改写环节的产物）。
            out.rewrite_used = bool(
                result.rewrite.query.sub_queries or result.rewrite.query.semantic_query
            )
        out.retrieved_doc_ids = list(retrieved)
        out.first_hit_rank_at5 = _first_hit_rank(retrieved, out.expected_doc_ids, limit=5)
        out.first_hit_rank_at10 = _first_hit_rank(retrieved, out.expected_doc_ids, limit=10)
    except Exception as exc:
        out.error = f"{exc.__class__.__name__}: {exc}"

    out.wall_s = time.monotonic() - started
    return out


def recall_at_5(outcomes: Sequence[ProbeOutcome]) -> tuple[int, int]:
    """(命中数, 分母)。**`absent` 探针不进分母**（它们是负例）。"""
    scored = [o for o in outcomes if not o.is_absent_probe and not o.error]
    return sum(1 for o in scored if o.hit_at_5), len(scored)


def false_recall_rate(outcomes: Sequence[ProbeOutcome]) -> tuple[int, int]:
    """`absent` 探针的误召回：本该一条都不召回，却召回了东西。"""
    absent = [o for o in outcomes if o.is_absent_probe and not o.error]
    return sum(1 for o in absent if o.retrieved_doc_ids), len(absent)


def mrr_at_10(outcomes: Sequence[ProbeOutcome]) -> float:
    """MRR@10。`absent` 同样不进分母。"""
    scored = [o for o in outcomes if not o.is_absent_probe and not o.error]
    if not scored:
        raise ValueError("没有可计分的探针 —— 空集不产出 MRR")
    return sum(1.0 / o.first_hit_rank_at10 if o.first_hit_rank_at10 else 0.0 for o in scored) / len(
        scored
    )


__all__ = [
    "PROC_ID_TEMPLATE",
    "ProbeOutcome",
    "false_recall_rate",
    "mrr_at_10",
    "preference_doc_ids",
    "recall_at_5",
    "run_probe",
]
