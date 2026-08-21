"""精排：bge-reranker-v2-m3 对融合后 Top-20 → Top-5（v6 §6.5.2 第 ③ 步）。

## 交叉编码器与双塔的区别，以及为什么这一步不能省

召回阶段的向量是**双塔**的：查询与文档各自独立编码，再比余弦。快，但查询与
文档之间没有任何交互，细粒度的匹配关系看不见。重排用的是**交叉编码器**：
把 `(查询, 文档)` 拼成一条输入过一遍模型，直接吐一个相关性分。慢得多，
所以只对前 20 条做。

## 两个实现

| 实现 | 何时 | 说明 |
|---|---|---|
| :class:`BGEReranker` | 本机 | `.data/models/bge-reranker-v2-m3`（2.2GB），离线加载 |
| :class:`LexicalReranker` | CI | 确定性的词元重合度，**不假装是语义重排** |

CI 上没有那 2.2GB 权重（`.data/` 已 gitignore），与 `memory/embeddings.py` 的
`HashEmbedder` 是同一个处置：**给一个真正确定性的替身，且如实标注**。
`RerankResult.provider` 会写明这一轮用的是哪个，检索质量指标（§12.4）
一律以 `bge` 那一路为准。

## 设备：默认 CPU

与摄取期嵌入同一条理由（M1 的隔离方案）：GPU 0 上常驻 Ollama 的 14B（约 13GB
/ 24GB），M7 微调还要占同一张卡。重排的输入是 20 条几十字的短文本，CPU 上
是毫秒到百毫秒量级，没有理由去抢显存。要切走 `RERANK_DEVICE` 即可。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from backend.core.config import Settings, get_settings
from backend.core.logging import get_logger
from backend.retrieval.bm25 import tokenize
from backend.retrieval.documents import RetrievedDoc

logger = get_logger(__name__)


@runtime_checkable
class Reranker(Protocol):
    """精排器契约。"""

    name: str

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        """给每条文档一个相关性分（越大越相关）。"""
        ...


@dataclass(frozen=True)
class RerankResult:
    """一次精排的产物。`provider` 必须如实写明用的是哪个实现。"""

    docs: tuple[RetrievedDoc, ...]
    provider: str
    #: 输入条数（融合后的 Top-N），供 §12.4 的分层评估核对口径
    candidates: int


class LexicalReranker:
    """确定性词元重合度。**不是语义重排，也不假装是。**

    分数 = 查询词元与文档词元的加权重合率，长词元权重更高（`missionc-2`
    这样的整串比单个汉字更有判别力）。同样输入永远同样输出。
    """

    name = "lexical"

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        query_tokens = set(tokenize(query))
        if not query_tokens:
            return [0.0] * len(texts)
        weights = {token: float(len(token)) for token in query_tokens}
        total = sum(weights.values())
        out: list[float] = []
        for text in texts:
            doc_tokens = set(tokenize(text))
            hit = sum(weights[t] for t in query_tokens & doc_tokens)
            out.append(hit / total)
        return out


class BGEReranker:
    """`bge-reranker-v2-m3` 的离线封装。"""

    name = "bge-reranker-v2-m3"

    def __init__(self, model_path: str | None = None, device: str | None = None) -> None:
        settings = get_settings()
        self._model_path = model_path or str(settings.BGE_RERANKER_PATH)
        self._device = device or settings.RERANK_DEVICE
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            logger.info("加载 bge-reranker-v2-m3", path=self._model_path, device=self._device)
            self._model = CrossEncoder(
                self._model_path,
                device=self._device,
                local_files_only=True,
                trust_remote_code=False,
            )
        return self._model

    def score(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        model = self._load()
        raw = model.predict([(query, text) for text in texts], show_progress_bar=False)
        return [float(v) for v in raw]


def build_reranker(settings: Settings | None = None) -> Reranker:
    """按 `RERANK_PROVIDER` 造精排器。"""
    cfg = settings or get_settings()
    if cfg.RERANK_PROVIDER == "lexical":
        return LexicalReranker()
    return BGEReranker()


def rerank(
    query: str,
    docs: Sequence[RetrievedDoc],
    *,
    top_k: int = 5,
    reranker: Reranker | None = None,
    settings: Settings | None = None,
) -> RerankResult:
    """对候选精排取前 `top_k`。

    **权威文档（路 A）不参与精排，直接置顶**（v6 §6.5.4）。理由与 RRF 那里
    完全相同：它们不是候选，是必须呈现的事实。让交叉编码器给一条 SQL 精确
    结果打分、再按分数决定它排第几，等于把「权威」交给了一个概率模型。

    并列时按 `doc_id` 排序，保证可复现（铁律 9）。
    """
    engine = reranker or build_reranker(settings)
    pinned = [d for d in docs if d.authoritative]
    rest = [d for d in docs if not d.authoritative]
    if not rest:
        return RerankResult(docs=tuple(pinned), provider=engine.name, candidates=len(docs))

    scores = engine.score(query, [d.text for d in rest])
    scored = sorted(
        zip(rest, scores, strict=True),
        key=lambda pair: (-pair[1], pair[0].doc_id),
    )
    top = [doc.with_score(round(float(score), 6)) for doc, score in scored[:top_k]]
    return RerankResult(docs=tuple(pinned + top), provider=engine.name, candidates=len(docs))


__all__ = [
    "BGEReranker",
    "LexicalReranker",
    "RerankResult",
    "Reranker",
    "build_reranker",
    "rerank",
]
