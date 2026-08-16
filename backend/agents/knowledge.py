"""`KnowledgeAgent` 知识问答（v6 §7.2.2）—— 本系统两处受控自治之二。

> 自主决定检索几轮、用哪几路。**步数上限 6**，只读工具。

## 自治在哪，边界在哪

**自治**：一个问题要查几轮、先查 PG 还是先查向量、查到一半发现要换个问法，
运行前不可知。所以它是 Agent 而不是 LLM 节点。

**边界**：

| 边界 | 落点 |
|---|---|
| **步数上限 6**（v6 §7.2.2） | :data:`KNOWLEDGE_MAX_STEPS`，熔断即停并如实标注 |
| **只读工具** | ACL 里 `knowledge` 那一行只有检索类 + `memory.search`；**`memory.write` 不在其中** |
| **排班取数不经此路径**（v6 §7.1.5） | `compile_spec_node` 直连 PG，本模块不被任何确定性节点调用 |
| 答案的事实内容来自路 A | `retrieval.generate.answer` 的结构化事实优先 |

## 没有 LLM 也能答，这不是降级补丁

四阶段管线（`retrieval.pipeline`）本身是确定性的：改写有规则路径、三路召回
是代码、融合与精排是代码。Agent 加的是**多轮追问的自主性**，不是问答能力本身。
所以 `harness=None`（或 Ollama 挂了）时照常给出完整回答，只是少了那层自主
探测 —— `autonomous=False` 如实标着。

## 熔断之后不是报错，是「答到这里」

步数用尽时把已经查到的东西交给生成层，并在 `notes` 里写明「已达步数上限」。
把一次「查得不够深」变成一个异常，会让用户什么都拿不到 —— 而他要的往往
第一轮就查到了。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from backend.core.config import Settings, get_settings
from backend.core.errors import FTSError
from backend.harness import AgentSpec, ContextBlock, Harness, structured_summary
from backend.harness.types import ToolHandler
from backend.memory import semantic
from backend.memory.episodic import search_episodes
from backend.memory.procedural import list_preferences, preference_docs
from backend.retrieval.bm25 import Bm25Index
from backend.retrieval.corpus import Corpus, build_corpus
from backend.retrieval.documents import RetrievedDoc
from backend.retrieval.generate import GroundedAnswer, answer
from backend.retrieval.pipeline import RetrievalConfig, RetrievalResult, retrieve
from backend.retrieval.prereq_cte import evaluate_prereq
from backend.retrieval.rerank import Reranker, rerank
from backend.retrieval.rewrite import ConversationTurn
from backend.retrieval.rrf import fuse
from backend.retrieval.vector import VectorIndex, build_vector_index
from backend.routing.entities import EntityDirectory

#: v6 §7.2.2「步数上限 6」。**上限，不是目标** —— 多数问题一轮就够。
KNOWLEDGE_MAX_STEPS: Final[int] = 6

#: 暴露给模型的工具。全部只读，且都是 ACL 里 `knowledge` 那一行的子集。
#: **`memory.write` 不在其中** —— 它在 ACL 里只给了 `extract`（v6 §7.7.2）。
KNOWLEDGE_TOOLS: Final[tuple[str, ...]] = (
    "sql_query",
    "prereq_cte",
    "bm25_search",
    "vector_search",
    "rrf_fuse",
    "rerank",
    "memory.search",
)

KNOWLEDGE_AGENT: Final[AgentSpec] = AgentSpec(name="knowledge", tools=KNOWLEDGE_TOOLS)


@dataclass(frozen=True)
class KnowledgeOutcome:
    """一次问答的完整产物。"""

    answer: GroundedAnswer
    retrieval: RetrievalResult
    steps: int
    llm_calls: int
    autonomous: bool
    #: 步数用尽而停 —— v6 §7.2.2 那条上限的观测口
    steps_exhausted: bool = False
    tool_calls: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def text(self) -> str:
        return self.answer.text

    def summary(self) -> str:
        mode = "自主检索" if self.autonomous else "确定性管线（无 LLM）"
        stop = "（已达步数上限）" if self.steps_exhausted else ""
        return (
            f"{mode}：{self.steps} 步{stop}，"
            f"召回 {len(self.retrieval.contexts)} 条上下文，"
            f"结构化结论 {len(self.retrieval.answers)} 条"
        )


# ─────────────────────────────────────────────────────────────────────
# 工具接线（返回值必须可 JSON 序列化 —— 要进 trace 与 Redis 缓存）
# ─────────────────────────────────────────────────────────────────────
def knowledge_tool_handlers(
    session: Session,
    snapshot_id: str,
    *,
    corpus: Corpus,
    vector_index: VectorIndex,
    at: datetime,
) -> dict[str, ToolHandler]:
    """把七个只读工具接到本窗口的实现上。

    `sql_query` 的只读性由 `SqlQueryParams` 的参数校验兜住（非 SELECT/WITH
    直接判非法），**这里不再自己判一遍** —— 判两遍就会出现两套口径。
    """
    bm25 = Bm25Index.build(corpus)

    def run_sql(args: dict[str, Any]) -> Any:
        statement = str(args.get("sql", ""))
        params = dict(args.get("params") or {})
        limit = int(args.get("limit", 100))
        rows = session.execute(sql_text(statement), params).mappings().all()
        return [{k: _jsonable(v) for k, v in row.items()} for row in rows[:limit]]

    def run_prereq(args: dict[str, Any]) -> Any:
        person_id = str(args.get("person_id", ""))
        mission_id = str(args.get("mission_id", ""))
        prereqs = semantic.prereq_map(session, snapshot_id).get(mission_id, [])
        completed = semantic.completed_missions(session, snapshot_id, person_id)
        all_ids = [m.mission_id for m in semantic.all_missions(session, snapshot_id)]
        met, missing = evaluate_prereq(prereqs, completed, all_ids)
        return {
            "person_id": person_id,
            "mission_id": mission_id,
            "prereq_met": met,
            "missing": list(missing),
            "completed": list(completed),
        }

    def run_bm25(args: dict[str, Any]) -> Any:
        hits = bm25.search(str(args.get("query", "")), top_k=int(args.get("top_k", 10)))
        return [_hit(d) for d in hits]

    def run_vector(args: dict[str, Any]) -> Any:
        hits = vector_index.search(str(args.get("query", "")), top_k=int(args.get("top_k", 10)))
        return [_hit(d) for d in hits]

    def run_rrf(args: dict[str, Any]) -> Any:
        rankings = [[_stub(doc_id) for doc_id in ranking] for ranking in args.get("rankings") or []]
        entries = fuse(rankings, k=int(args.get("k", 60)), top_k=int(args.get("top_k", 10)))
        return [
            {"doc_id": e.doc.doc_id, "rrf_score": round(e.rrf_score, 6), "ranks": e.ranks}
            for e in entries
        ]

    def run_rerank(args: dict[str, Any]) -> Any:
        candidates = [str(c) for c in args.get("candidates") or []]
        docs = []
        for candidate in candidates:
            known = corpus.get(candidate)
            docs.append(
                RetrievedDoc(
                    doc_id=candidate,
                    # 语料里没有这个 id 时用 id 本身当文本：模型可能编了一个
                    # doc_id，重排照跑但它排不上去 —— 这比抛异常好，一次
                    # 幻觉不该让整轮问答挂掉
                    text=known.text if known is not None else candidate,
                    source_kind="bm25",
                )
            )
        result = rerank(str(args.get("query", "")), docs, top_k=int(args.get("top_k", 5)))
        return {
            "provider": result.provider,
            "docs": [_hit(d) for d in result.docs],
        }

    def run_memory_search(args: dict[str, Any]) -> Any:
        kinds = [str(k) for k in args.get("kinds") or []] or ["semantic", "episodic", "procedural"]
        top_k = int(args.get("top_k", 5))
        out: dict[str, Any] = {}
        if "episodic" in kinds:
            episodes = search_episodes(session, at=at)
            out["episodic"] = [
                {"memory_id": e.memory_id, "kind": e.kind, "summary": e.summary}
                for e in episodes[:top_k]
            ]
        if "procedural" in kinds:
            prefs = list_preferences(session, at=at)
            out["procedural"] = preference_docs(prefs)[:top_k]
        if "semantic" in kinds:
            hits = bm25.search(str(args.get("query", "")), top_k=top_k)
            out["semantic"] = [_hit(d) for d in hits]
        return out

    return {
        "sql_query": run_sql,
        "prereq_cte": run_prereq,
        "bm25_search": run_bm25,
        "vector_search": run_vector,
        "rrf_fuse": run_rrf,
        "rerank": run_rerank,
        "memory.search": run_memory_search,
    }


def _hit(doc: RetrievedDoc) -> dict[str, Any]:
    return {
        "doc_id": doc.doc_id,
        "text": doc.text[:400],
        "source": doc.source_kind,
        "score": doc.score,
        "authoritative": doc.authoritative,
    }


def _stub(doc_id: str) -> RetrievedDoc:
    """`rrf_fuse` 的入参是 doc_id 排序表，融合只用名次，文本无关紧要。"""
    return RetrievedDoc(doc_id=str(doc_id), text=str(doc_id), source_kind="bm25")


def _jsonable(value: Any) -> Any:
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, dict | list | str | int | float | bool) or value is None:
        return value
    return str(value)


# ─────────────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────────────
def _blocks(result: RetrievalResult, step: int, gathered: Sequence[str]) -> list[ContextBlock]:
    payload: dict[str, Any] = {
        "问题": result.query.original_query,
        "已消解实体": [e.entity_id for e in result.query.resolved_entities] or ["（无）"],
        "结构化结论": len(result.answers),
        "召回上下文": len(result.contexts),
        "本轮": f"{step}/{KNOWLEDGE_MAX_STEPS}",
    }
    blocks = [
        ContextBlock(kind="summary", content=structured_summary("当前检索状态", payload)),
    ]
    if gathered:
        blocks.append(
            ContextBlock(
                kind="evidence",
                content="【已经查到的】\n" + "\n".join(f"- {g}" for g in gathered[-6:]),
                role="user",
            )
        )
    blocks.append(
        ContextBlock(
            kind="history",
            content=(
                "还需要查什么就调工具；已经够回答了就不要再调。"
                "**不要自己写答案**，答案由后一步统一生成。"
            ),
            role="user",
        )
    )
    return blocks


def ask(
    question: str,
    *,
    session: Session,
    snapshot_id: str,
    directory: EntityDirectory,
    today: date,
    as_of: date | None = None,
    harness: Harness | None = None,
    history: Sequence[ConversationTurn] = (),
    config: RetrievalConfig | None = None,
    corpus: Corpus | None = None,
    vector_index: VectorIndex | None = None,
    reranker: Reranker | None = None,
    settings: Settings | None = None,
) -> KnowledgeOutcome:
    """回答一个问题。

    流程：**确定性四阶段管线 → （可选）自主追查 ≤6 步 → 带引用生成**。

    第一步就跑完整管线而不是让模型从零开始调工具，是因为管线本身已经把
    路 A/B/C 都跑过一遍了。让模型再调一遍 `bm25_search` 只是重复劳动；
    它的价值在于**发现第一轮没查到的东西**，而那要先看见第一轮的结果。
    """
    cfg = settings or get_settings()
    moment = as_of or today
    working_corpus = corpus if corpus is not None else build_corpus(session, snapshot_id)
    index = vector_index or build_vector_index(working_corpus.filter(), backend=cfg.VECTOR_BACKEND)

    result = retrieve(
        question,
        session=session,
        snapshot_id=snapshot_id,
        directory=directory,
        today=today,
        as_of=moment,
        harness=harness,
        history=history,
        corpus=working_corpus,
        vector_index=index,
        reranker=reranker,
        config=config,
        settings=cfg,
    )

    llm_calls = result.rewrite.llm_calls
    notes: list[str] = list(result.notes)
    steps = 0
    exhausted = False
    tool_calls: list[str] = []
    autonomous = harness is not None

    # 有歧义就反问，**不进自主循环** —— 连问的是谁都没定，多查几轮没有意义
    if harness is not None and not result.needs_clarification:
        harness.registry.register_many(
            knowledge_tool_handlers(
                session,
                snapshot_id,
                corpus=working_corpus,
                vector_index=index,
                at=datetime.combine(moment, datetime.min.time()),
            )
        )
        gathered: list[str] = []
        max_steps = min(KNOWLEDGE_MAX_STEPS, cfg.KNOWLEDGE_MAX_STEPS)
        for step in range(1, max_steps + 1):
            try:
                out = harness.call(KNOWLEDGE_AGENT, _blocks(result, step, gathered))
            except FTSError as exc:
                notes.append(f"自主检索中断（{exc.message}），已用现有召回作答")
                autonomous = False
                break
            llm_calls += out.llm_calls
            steps = step
            if out.degraded:
                notes.append(f"自主检索降级（{out.error_code}），已用现有召回作答")
                autonomous = False
                break
            if not out.calls:
                break  # 模型自己决定停 —— 这正是它的自治所在
            tool_calls.extend(call.name for call in out.calls)
            gathered.extend(_tool_notes(out.calls, out.results))
            if step == max_steps:
                # ★ 熔断：步数用尽不是错误，是「答到这里」（v6 §7.2.2 步数上限 6）
                exhausted = True
                notes.append(
                    f"已达步数上限 {max_steps} 步，用已经查到的内容作答"
                    "（这不是失败：多数问题第一轮就查到了）"
                )

    grounded = answer(result, harness=harness)
    llm_calls += grounded.llm_calls
    notes.extend(grounded.notes)

    return KnowledgeOutcome(
        answer=grounded,
        retrieval=result,
        steps=steps,
        llm_calls=llm_calls,
        autonomous=autonomous and harness is not None,
        steps_exhausted=exhausted,
        tool_calls=tuple(tool_calls),
        notes=tuple(notes),
    )


def _tool_notes(calls: Any, results: Any) -> list[str]:
    out: list[str] = []
    for call, result in zip(calls, results, strict=False):
        payload = result.value if result.ok else result.error
        out.append(f"{call.name}: {json.dumps(payload, ensure_ascii=False, default=str)[:300]}")
    return out


__all__ = [
    "KNOWLEDGE_AGENT",
    "KNOWLEDGE_MAX_STEPS",
    "KNOWLEDGE_TOOLS",
    "KnowledgeOutcome",
    "ask",
    "knowledge_tool_handlers",
]
