"""文档分类器（v6 §5.1）：规则优先 + LLM 兜底，六类。

人员档案 / 飞机资源 / 课目标准 / 规则条文 / 情况文件(请假·维修) / 未知。

**为什么规则优先**：这四份 PDF 的标题与表头是稳定的，规则匹配零成本、零延迟、
可复现。LLM 只在规则全部落空时兜底，且兜底结果被约束在六个字面量里 —— 它
返回别的东西就是 `未知`，不会凭空造出第七类。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, get_args

from backend.core.logging import get_logger
from backend.ingestion.prompts import build_extraction_messages
from backend.ingestion.repair import repair_text
from backend.ingestion.schema import DocumentClass
from backend.llm.provider import LLMProvider

logger = get_logger(__name__)

#: 六类的合法字面量集合，直接取自契约，避免两处维护
DOCUMENT_CLASSES: Final[tuple[str, ...]] = get_args(DocumentClass)

#: 规则匹配表：类别 → 关键特征正则（命中任意一条即判定）。顺序即优先级。
RULE_SIGNATURES: Final[tuple[tuple[DocumentClass, tuple[re.Pattern[str], ...]], ...]] = (
    (
        "规则条文",
        (
            re.compile(r"排班规则"),
            re.compile(r"约束\s*\d+\s*[（(].+?[）)]\s*【"),
            re.compile(r"本规则共\s*\d+\s*条"),
        ),
    ),
    (
        "人员档案",
        (
            re.compile(r"飞行人员资质档案"),
            re.compile(r"人员资质总表"),
            re.compile(r"课目级资质明细"),
            re.compile(r"编号\s+姓名\s+身份"),
        ),
    ),
    (
        "飞机资源",
        (
            re.compile(r"飞机与空域资源清单"),
            re.compile(r"飞机资源明细"),
            re.compile(r"空域[/／]航线资源与容量"),
            re.compile(r"机号\s+机型\s+座位"),
        ),
    ),
    (
        "课目标准",
        (
            re.compile(r"课目频率标准"),
            re.compile(r"课目编号\s+名称\s+类型"),
            re.compile(r"课程周期与频率要求"),
        ),
    ),
    (
        "情况文件",
        (
            re.compile(r"请假(申请|通知|单)"),
            re.compile(r"维修(通知|计划变更)"),
            re.compile(r"停飞(通知|申请)"),
            re.compile(r"临时(调整|变更)通知"),
        ),
    ),
)

#: 兜底时给 LLM 的受约束解码 schema
CLASSIFY_SCHEMA: Final[dict[str, object]] = {
    "type": "object",
    "properties": {"doc_class": {"type": "string", "enum": list(DOCUMENT_CLASSES)}},
    "required": ["doc_class"],
    "additionalProperties": False,
}

_CLASSIFY_INSTRUCTION: Final[str] = (
    "判断下面这份文档属于以下六类中的哪一类，只输出 JSON：\n"
    "人员档案 / 飞机资源 / 课目标准 / 规则条文 / 情况文件 / 未知\n"
    "拿不准就输出「未知」，不要勉强归类。"
)

#: 兜底只看开头这么多字符，够判类型，也避免把整份文档塞进上下文
LLM_PROBE_CHARS: Final[int] = 1500


@dataclass(frozen=True)
class Classification:
    """分类结果。`by` 记录是规则命中还是 LLM 兜底，进 snapshot manifest。"""

    doc_class: DocumentClass
    by: str
    evidence: str = ""


def classify_by_rules(text: str) -> Classification | None:
    """规则匹配。命中返回结果，全部落空返回 ``None``（交给兜底）。"""
    normalized = repair_text(text)
    for doc_class, patterns in RULE_SIGNATURES:
        for pattern in patterns:
            match = pattern.search(normalized)
            if match:
                return Classification(doc_class=doc_class, by="rule", evidence=match.group(0))
    return None


def classify_by_llm(text: str, provider: LLMProvider) -> Classification:
    """LLM 兜底。输出被 :data:`CLASSIFY_SCHEMA` 约束在六个字面量内。

    解析失败或返回了枚举外的值，一律降级为「未知」而不是抛异常 —— 分类
    拿不准不该阻断管线，后面还有人工确认门禁；真正必须阻断的是抽取失败
    （铁律 7），不是分类失败。
    """
    import json

    messages = build_extraction_messages(
        _CLASSIFY_INSTRUCTION, text[:LLM_PROBE_CHARS], source="待分类文档"
    )
    try:
        raw = provider.complete(messages, schema=CLASSIFY_SCHEMA, temperature=0.0)
        parsed = json.loads(raw)
        candidate = parsed.get("doc_class", "未知")
    except (json.JSONDecodeError, KeyError, AttributeError, TypeError) as exc:
        logger.warning("文档分类 LLM 兜底解析失败，降级为未知", error=str(exc))
        return Classification(doc_class="未知", by="llm", evidence="LLM 输出不可解析")

    if candidate not in DOCUMENT_CLASSES:
        logger.warning("文档分类 LLM 兜底返回枚举外的值，降级为未知", candidate=str(candidate))
        return Classification(doc_class="未知", by="llm", evidence=f"枚举外取值 {candidate!r}")
    return Classification(doc_class=candidate, by="llm", evidence="")


def classify_document(text: str, provider: LLMProvider | None = None) -> Classification:
    """分类器主入口：规则优先，规则落空且给了 provider 时才走 LLM。"""
    hit = classify_by_rules(text)
    if hit is not None:
        return hit
    if provider is None:
        return Classification(doc_class="未知", by="rule", evidence="规则未命中且未提供 LLM 兜底")
    return classify_by_llm(text, provider)


__all__ = [
    "CLASSIFY_SCHEMA",
    "DOCUMENT_CLASSES",
    "LLM_PROBE_CHARS",
    "RULE_SIGNATURES",
    "Classification",
    "classify_by_llm",
    "classify_by_rules",
    "classify_document",
]
