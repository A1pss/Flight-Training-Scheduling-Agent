"""M5 检索/记忆的测试夹具。

**嵌入与精排一律显式传确定性替身**（`HashEmbedder` / `LexicalReranker`），
不靠环境变量：

- CI 上没有 bge-m3 / bge-reranker 那两个 2.2GB 权重（`.data/` 已 gitignore）；
- 本机 `.env` 里 `EMBED_PROVIDER=bge`，跟着环境走会让同一个用例在本地与 CI
  跑的是两套东西 —— CLAUDE.md §6：**验证时的视角必须与 CI 的视角一致**。

真模型（bge-m3 + bge-reranker-v2-m3）的实测另有专门用例，标 `@pytest.mark.ollama`
（它与 Ollama 一样属于「CI 不跑、真机跑」那一类），结果记在收工报告里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from backend.memory.embeddings import HashEmbedder
from backend.retrieval.corpus import Corpus, build_corpus
from backend.retrieval.rerank import LexicalReranker
from backend.retrieval.vector import InMemoryIndex


@dataclass(frozen=True)
class RetrievalRig:
    """一套装好的检索件：语料 + 向量索引 + 精排器。"""

    corpus: Corpus
    index: InMemoryIndex
    reranker: LexicalReranker

    def kwargs(self) -> dict[str, Any]:
        """展开成 `retrieve()` / `ask()` 的关键字参数。"""
        return {
            "corpus": self.corpus,
            "vector_index": self.index,
            "reranker": self.reranker,
        }


def build_rig(session: Session, snapshot_id: str) -> RetrievalRig:
    """按当前快照装一套检索件。"""
    corpus = build_corpus(session, snapshot_id)
    return RetrievalRig(
        corpus=corpus,
        index=InMemoryIndex(corpus.filter(), embedder=HashEmbedder()),
        reranker=LexicalReranker(),
    )


# ─────────────────────────────────────────────────────────────────────
# KnowledgeAgent 熔断用的 Harness 替身
# ─────────────────────────────────────────────────────────────────────
@dataclass
class LoopingRegistry:
    """`registry.register_many` 的最小替身。"""

    handlers: dict[str, Any] = field(default_factory=dict)

    def register_many(self, handlers: dict[str, Any]) -> None:
        self.handlers.update(handlers)

    def register(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


@dataclass
class NeverStoppingHarness:
    """**每一轮都要求再调一次工具**的 Harness 替身。

    专为熔断用例存在：真模型总会在某一轮停下来，而「步数上限」这条边界只有在
    模型**不肯停**的时候才生效。测边界就要构造那个边界（v6 §7.2.2 步数上限 6）。
    """

    calls: list[Any] = field(default_factory=list)
    registry: LoopingRegistry = field(default_factory=LoopingRegistry)

    def call(self, agent: Any, blocks: Any = (), **_: Any) -> Any:
        from backend.harness.types import AgentOutput, AttemptRecord, ToolResult, ValidatedCall

        self.calls.append(agent.prompt_key)
        if agent.prompt_key == "answer":
            # 生成阶段：给一句能过核验的空话，把注意力留给步数断言
            return AgentOutput(
                component="knowledge",
                text="",
                mode="native",
                attempts=(AttemptRecord(attempt=0, mode="native", failures=()),),
                llm_calls=1,
            )
        return AgentOutput(
            component="knowledge",
            calls=(ValidatedCall(name="bm25_search", arguments={"query": "何超", "top_k": 3}),),
            results=(ToolResult(tool="bm25_search", ok=True, value=[]),),
            mode="native",
            attempts=(AttemptRecord(attempt=0, mode="native", failures=()),),
            llm_calls=1,
        )

    @property
    def react_rounds(self) -> int:
        """只数 ReAct 轮（`prompt_key == "system"`），不数改写与生成那两次。"""
        return sum(1 for key in self.calls if key == "system")


__all__ = ["LoopingRegistry", "NeverStoppingHarness", "RetrievalRig", "build_rig"]
