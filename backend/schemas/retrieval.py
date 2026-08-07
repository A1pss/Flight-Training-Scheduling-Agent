"""检索与接地契约（v6 §6.5.3 查询改写 / §7.4 `grounding_report`）。

**实体消解不靠 LLM 猜**（v6 §6.5.3）：LLM 只负责识别「这里提到了一个人名」，
实际映射走 `resolve_person` 做精确字典匹配 + 编辑距离候选。命中多个候选或
编辑距离过近（如同时命中何超/高超）时，**不自行选择**，写入 `ambiguities`
触发反问——契约层用 `_ambiguity_blocks_resolution` 把这条规矩固化下来。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, computed_field

from backend.schemas.common import DateRange, EntityRef


class RewrittenQuery(BaseModel):
    """查询改写产物（v6 §6.5.3）。改写是三路召回中唯一需要 LLM 的环节。"""

    model_config = ConfigDict(extra="forbid")

    original_query: str = Field(min_length=1, description="保留原句：改写有时会丢语义")
    resolved_entities: list[EntityRef] = Field(default_factory=list)
    normalized_timerange: DateRange | None = None
    sub_queries: list[str] = Field(default_factory=list)
    keyword_terms: list[str] = Field(default_factory=list, description="供 BM25")
    semantic_query: str = Field(default="", description="供向量检索的语义化表述")
    ambiguities: list[str] = Field(default_factory=list, description="无法消解的歧义 → 触发反问")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def needs_clarification(self) -> bool:
        """有歧义就必须反问，不允许下游自行挑一个继续。"""
        return bool(self.ambiguities)


class Citation(BaseModel):
    """一条引用来源。结构化来源（路 A）优先级最高（v6 §6.5.4）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_kind: str = Field(min_length=1, description="structured / bm25 / vector / ruleset")
    source_id: str = Field(min_length=1)
    snippet: str = Field(default="", description="用于人工核对的原文片段")
    score: float | None = None


class GroundedClaim(BaseModel):
    """生成文本中的一条断言及其引用。"""

    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1)
    citations: list[Citation] = Field(default_factory=list)
    supported: bool = False


class GroundingReport(BaseModel):
    """解释生成的事实核验报告（v6 §7.4 `grounding_report`）。

    `unsupported_claims` 非空即意味着解释里有无出处的说法——这类文本
    不得直接呈现，需触发重写（v6 §7.6「explain 生成 + 核验」含 1 轮重写）。
    """

    model_config = ConfigDict(extra="forbid")

    claims: list[GroundedClaim] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def supported_ratio(self) -> float:
        if not self.claims:
            return 0.0
        return sum(1 for c in self.claims if c.supported) / len(self.claims)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unsupported_claims(self) -> list[str]:
        return [c.claim for c in self.claims if not c.supported]


__all__ = [
    "Citation",
    "GroundedClaim",
    "GroundingReport",
    "RewrittenQuery",
]
