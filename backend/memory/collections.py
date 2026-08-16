"""Chroma collection 的名字与用途说明。

**刻意做成没有任何内部依赖的叶子模块**：切分侧（`ingestion.chunkers`）要给
chunk 打 collection 标签，存储侧（`memory.chroma`）要按名字建表，两边都需要
这组常量。放在任何一侧都会造成 `ingestion ↔ memory` 循环 import。
"""

from __future__ import annotations

from typing import Final

#: 规则原文（v6 §6.1）
COLLECTION_RULES: Final[str] = "rule_texts"
#: 实体摘要句（v6 §6.1）
COLLECTION_ENTITIES: Final[str] = "entity_summaries"
#: 历史报告（v6 §6.1）
COLLECTION_REPORTS: Final[str] = "historical_reports"
#: 情况文件与通知。§6.1 的三分法没有覆盖 §5.3 的「情况文件」「会议纪要/通知」
#: 两种切分策略，它们的 chunk 总得有个落点；单列一个 collection，避免污染
#: 历史报告那一路召回。
COLLECTION_SITUATIONS: Final[str] = "situation_docs"
#: 情景记忆的摘要向量（v6 §6.2「情景记忆：PG + Chroma（摘要向量）」，M5 新增）。
#:
#: §6.1 那张表列的是**文档**向量的四个落点，没给情景记忆留位置 —— 而 §6.2
#: 明确要求情景记忆「PG 存权威内容 + Chroma 存摘要向量」。塞进
#: `historical_reports` 会污染那一路召回（历史报告是排班产物，情景记忆是
#: 会话经历，两者的时效语义完全不同：前者按周归档、后者按 §6.4 三个训练周期
#: 归档到冷表），所以单列一个。
COLLECTION_EPISODIC: Final[str] = "episodic_summaries"

COLLECTION_DESCRIPTIONS: Final[dict[str, str]] = {
    COLLECTION_RULES: "规则原文：rules.pdf 抽出的单条约束，禁止拆分（v6 §5.3）",
    COLLECTION_ENTITIES: "实体摘要句：人员/飞机/课目/空域/跑道的自然语言摘要，field_map 回指 PG",
    COLLECTION_REPORTS: "历史报告：归档的排班报告，按周 + 按小节切分",
    COLLECTION_SITUATIONS: "情况文件与通知：语义段落 / 递归字符切分",
    COLLECTION_EPISODIC: "情景记忆摘要：历次会话、用户修改与驳回、松弛档选择（v6 §6.2）",
}

ALL_COLLECTIONS: Final[tuple[str, ...]] = (
    COLLECTION_RULES,
    COLLECTION_ENTITIES,
    COLLECTION_REPORTS,
    COLLECTION_SITUATIONS,
    COLLECTION_EPISODIC,
)

__all__ = [
    "ALL_COLLECTIONS",
    "COLLECTION_DESCRIPTIONS",
    "COLLECTION_ENTITIES",
    "COLLECTION_EPISODIC",
    "COLLECTION_REPORTS",
    "COLLECTION_RULES",
    "COLLECTION_SITUATIONS",
]
