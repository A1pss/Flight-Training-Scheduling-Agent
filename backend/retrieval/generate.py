"""④ 带引用生成 + 事实核验（v6 §6.5.2 第四阶段）。

> 每条断言标注来源；**结构化来源（路 A）优先级最高**。

## 事实内容由路 A 出，LLM 只组织语言（业务方 2026-08-14 确认）

语义类事实问题（v6 §12.4 的 M1~M4）的答案**内容**来自
`structured.FactAnswer` —— 那是确定性代码按规格算出来的，带 `table` + `pk`
的引用。LLM 拿到的是**已核实的结构化事实**，任务只有措辞。

这不是不信任模型，是口径问题：§12.4 给语义类的目标是 **≥98%**，并注明
「走 SQL 精确通道，**不依赖模型**」。若答案内容由 14B 生成，那个 98% 就成了
模型当天发挥的函数，而 §12.4 明说这一路是「把加权值拉过交付线的结构性依靠」。

**核验不过就退回事实直出**（`fallback` 分支）。少一点文采，好过一句查无实据的话。

## 事实核验怎么判

逐句检查句子里的**数字**与**实体编号**是否都出现在召回内容里：

- 出现 → 该句有据，挂上给出该证据的那条引用；
- 不出现 → `supported=False`，进 `GroundingReport.unsupported_claims`。

与 `components/explain.py` 的核验器**是两套，且应当是两套**：那一套核验的是
「解释文本 vs 排班方案」，这一套核验的是「回答 vs 召回内容」，事实来源不同、
可核验的量也不同。共用一份反而会让两边都必须迁就对方的事实集。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from backend.core.errors import FTSError
from backend.core.logging import get_logger
from backend.harness import AgentSpec, ContextBlock, Harness
from backend.retrieval.documents import RetrievedDoc
from backend.retrieval.pipeline import RetrievalResult
from backend.retrieval.structured import FactAnswer
from backend.schemas.retrieval import Citation, GroundedClaim, GroundingReport

logger = get_logger(__name__)

#: 生成用的组件规格。纯生成，不给工具（走 native 模式）
ANSWER_AGENT: Final[AgentSpec] = AgentSpec(
    name="knowledge",
    tools=(),
    prompt_key="answer",
    requires_tool_call=False,
)

#: 句切分。与 `components/explain.py` 同口径（中文句号/问号/叹号/分号 + 换行）
_SENTENCE: Final[re.Pattern[str]] = re.compile(r"[^。！？；\n]+[。！？；]?")
#: 可核验的数字：整数、小数、日期、时刻
_NUMBER: Final[re.Pattern[str]] = re.compile(r"\d+(?:[-:.]\d+)*")
#: 可核验的实体编号
_ENTITY: Final[re.Pattern[str]] = re.compile(
    r"\b(?:P\d+|AC\d+|mission[A-Z]-\d+|RWY-\d+|\d{4}W\d{2})\b"
)


@dataclass(frozen=True)
class GroundedAnswer:
    """一次问答的完整产物。"""

    text: str
    report: GroundingReport
    citations: tuple[Citation, ...] = ()
    #: 路 A 给出的确定性结论（原文，未经改写）
    facts: tuple[FactAnswer, ...] = ()
    llm_calls: int = 0
    #: 生成经 LLM 但核验没过 → 退回事实直出
    fallback: bool = False
    degraded: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def faithful(self) -> bool:
        """全部断言都有出处。"""
        return not self.report.unsupported_claims


# ─────────────────────────────────────────────────────────────────────
# 核验
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class EvidenceIndex:
    """召回内容里可核验的量。**只有召回到的，没有推断的。**"""

    numbers: frozenset[str]
    entities: frozenset[str]
    citations: tuple[Citation, ...]
    #: token → 给出该 token 的引用（挂到句子上，让人能逐句回溯）
    provenance: dict[str, Citation] = field(default_factory=dict)

    @classmethod
    def build(cls, docs: Sequence[RetrievedDoc], facts: Sequence[FactAnswer] = ()) -> EvidenceIndex:
        numbers: set[str] = set()
        entities: set[str] = set()
        provenance: dict[str, Citation] = {}
        citations: list[Citation] = []

        def absorb(text: str, citation: Citation) -> None:
            for token in _NUMBER.findall(text):
                numbers.add(token)
                provenance.setdefault(token, citation)
            for token in _ENTITY.findall(text):
                entities.add(token)
                provenance.setdefault(token, citation)

        # 路 A 的结论优先进索引 —— 它们的引用优先级最高（§6.5.4）
        for fact in facts:
            citation = fact.citations[0] if fact.citations else _SELF_CITATION
            absorb(fact.statement, citation)
            citations.extend(fact.citations)
        for doc in docs:
            citation = doc.citation()
            citations.append(citation)
            absorb(doc.text, citation)

        return cls(
            numbers=frozenset(numbers),
            entities=frozenset(entities),
            citations=tuple(_dedupe_citations(citations)),
            provenance=provenance,
        )

    def check(self, claim: str) -> GroundedClaim:
        """核验一句话。**确定性，没有模型参与。**"""
        numbers = _NUMBER.findall(claim)
        entities = _ENTITY.findall(claim)
        if not numbers and not entities:
            # 没有事实内容的句子（「下面是查询结果：」）不需要核验
            return GroundedClaim(claim=claim, citations=[], supported=True)
        bad = [n for n in numbers if n not in self.numbers]
        bad += [e for e in entities if e not in self.entities]
        if bad:
            return GroundedClaim(claim=claim, citations=[], supported=False)
        used = _dedupe_citations(
            [self.provenance[t] for t in [*numbers, *entities] if t in self.provenance]
        )
        return GroundedClaim(claim=claim, citations=used, supported=True)


#: 路 A 的结论若没带引用（不该发生），用它占位以便核验仍能进行
_SELF_CITATION: Final[Citation] = Citation(
    source_kind="structured", source_id="pg:structured", snippet="结构化查询结果"
)


def split_claims(text: str) -> list[str]:
    """按句切分。空句与纯标点句丢掉。"""
    return [s.strip() for s in _SENTENCE.findall(text) if s.strip(" 。！？；\n")]


def verify(text: str, index: EvidenceIndex) -> GroundingReport:
    """逐句核验，产出 `GroundingReport`。"""
    return GroundingReport(claims=[index.check(c) for c in split_claims(text)])


# ─────────────────────────────────────────────────────────────────────
# 生成
# ─────────────────────────────────────────────────────────────────────
def compose_facts(result: RetrievalResult) -> str:
    """事实直出：把路 A 的结论按顺序拼成回答。

    **这是保底路径，也是正确性的基准线。** LLM 的产物必须核验通过才会取代它。
    """
    lines = [fact.statement for fact in result.answers]
    if not lines:
        lines = _from_contexts(result.contexts)
    return "\n".join(lines)


def _from_contexts(docs: Sequence[RetrievedDoc]) -> list[str]:
    """路 A 没给结论时，用召回到的内容直陈。

    **不做推理**：把召回到的句子原样列出来，并说明这是检索结果而非结论。
    「检索没查到就说没查到，不要用常识补」（`prompts/knowledge/system.md`）。
    """
    if not docs:
        return ["没有检索到与这个问题相关的内容。"]
    lines = ["检索到以下相关内容："]
    lines.extend(f"- {doc.text}" for doc in docs[:5])
    return lines


def answer(
    result: RetrievalResult,
    *,
    harness: Harness | None = None,
) -> GroundedAnswer:
    """生成带引用的回答并核验。

    三条出口：

    | 情形 | 产物 |
    |---|---|
    | 有歧义 | 反问文本，**不作答**（§6.5.3：不自行选择） |
    | 无 LLM / 核验不过 | 事实直出（`fallback=True`） |
    | LLM 且核验通过 | 模型措辞 + 逐句引用 |
    """
    index = EvidenceIndex.build(result.contexts, result.answers)

    if result.needs_clarification:
        text = "这个问题里有需要先确认的地方：\n" + "\n".join(
            f"- {a}" for a in result.query.ambiguities
        )
        return GroundedAnswer(
            text=text,
            report=GroundingReport(claims=[]),
            citations=index.citations,
            facts=result.answers,
            notes=("存在歧义，已反问而非作答",),
        )

    baseline = compose_facts(result)
    if harness is None:
        return GroundedAnswer(
            text=baseline,
            report=verify(baseline, index),
            citations=index.citations,
            facts=result.answers,
            fallback=True,
            degraded=True,
            notes=("未配置 Harness，走事实直出",),
        )

    try:
        out = harness.call(ANSWER_AGENT, _blocks(result, baseline))
    except FTSError as exc:
        return GroundedAnswer(
            text=baseline,
            report=verify(baseline, index),
            citations=index.citations,
            facts=result.answers,
            fallback=True,
            degraded=True,
            notes=(f"生成中断（{exc.message}），已退回事实直出",),
        )

    if out.degraded or not out.text.strip():
        return GroundedAnswer(
            text=baseline,
            report=verify(baseline, index),
            citations=index.citations,
            facts=result.answers,
            llm_calls=out.llm_calls,
            fallback=True,
            degraded=True,
            notes=(f"生成降级（{out.error_code or '空输出'}），已退回事实直出",),
        )

    report = verify(out.text, index)
    if report.unsupported_claims:
        logger.warning(
            "生成的回答有无出处的断言，已退回事实直出",
            unsupported=report.unsupported_claims[:3],
        )
        return GroundedAnswer(
            text=baseline,
            report=verify(baseline, index),
            citations=index.citations,
            facts=result.answers,
            llm_calls=out.llm_calls,
            fallback=True,
            notes=(f"模型输出有 {len(report.unsupported_claims)} 句查无实据，已退回事实直出",),
        )
    return GroundedAnswer(
        text=out.text.strip(),
        report=report,
        citations=index.citations,
        facts=result.answers,
        llm_calls=out.llm_calls,
    )


def _blocks(result: RetrievalResult, baseline: str) -> list[ContextBlock]:
    """给生成用的上下文：**已核实的事实在前，召回内容在后**（§6.5.4 的优先级）。"""
    blocks: list[ContextBlock] = []
    if result.answers:
        facts = "\n".join(f"{i}. {f.statement}" for i, f in enumerate(result.answers, start=1))
        blocks.append(
            ContextBlock(
                kind="summary",
                content=(
                    "【已核实的事实（来自 PG 精确查询，权威）】\n"
                    f"{facts}\n"
                    "**只能用上面这些事实作答，一个数字都不许改，也不要补充其他说法。**"
                ),
                role="user",
                label="structured_facts",
            )
        )
    if result.contexts:
        docs = "\n".join(f"[{d.doc_id}] {d.text}" for d in result.contexts)
        blocks.append(
            ContextBlock(
                kind="evidence",
                content=f"【召回到的相关内容】\n{docs}",
                role="user",
                label="contexts",
            )
        )
    blocks.append(
        ContextBlock(
            kind="history",
            content=(
                f"用户的问题：{result.query.original_query}\n"
                "请用上面的事实回答，措辞自然即可。\n"
                f"作为参照，事实直出的版本是：\n{baseline}"
            ),
            role="user",
        )
    )
    return blocks


def _dedupe_citations(citations: Sequence[Citation]) -> list[Citation]:
    seen: set[tuple[str, str]] = set()
    out: list[Citation] = []
    for citation in citations:
        key = (citation.source_kind, citation.source_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(citation)
    return out


__all__ = [
    "ANSWER_AGENT",
    "EvidenceIndex",
    "GroundedAnswer",
    "answer",
    "compose_facts",
    "split_claims",
    "verify",
]
