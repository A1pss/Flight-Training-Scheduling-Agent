"""`rules.pdf` → :class:`IngestedRule`。

这份 PDF **没有表格**，是纯自由文本，但它高度结构化（每条以
`约束N(标题)【硬约束】:` 开头、以 `。` 结尾），所以走正则切分而不是 LLM ——
既确定、又零调用。

**切分单元是「单条约束」，禁止拆分**（v6 §5.3）：半条规则的检索结果是危险的。

> ⚠️ **抽出来的条文不进求解器。** 排班约束永远来自 `rules/ruleset_v1.3.yaml`
> 这份版本化文件（v6 §5.4 第 3 层）。这里抽的文本只进 Chroma，供检索与解释
> 引用原文。这是提示词注入防护里最硬的一层：污染上传文档改不了任何一条约束。
"""

from __future__ import annotations

import re

from backend.core.errors import IngestionError
from backend.ingestion.adapters import ExtractedDocument
from backend.ingestion.schema import IngestedRule

#: v6 §5.3 的切分正则。原文用全角括号书写，修复层 NFKC 归一化后成半角，
#: 这里两种宽度都接受 —— 只放宽匹配面，切分单元本身仍是「单条约束」。
RULE_RE = re.compile(
    r"约束\s*(?P<rule_id>\d+)\s*[（(](?P<title>.+?)[）)]\s*【(?P<hard_soft>.+?)】",
)
#: 总则里声明的条文总数，用于交叉验证抽全了没有
_TOTAL_RE = re.compile(r"本规则共\s*(\d+)\s*条")


def parse_rules_document(doc: ExtractedDocument) -> tuple[IngestedRule, ...]:
    """`rules.pdf` 主入口。"""
    text = doc.text
    matches = list(RULE_RE.finditer(text))
    if not matches:
        raise IngestionError(
            f"{doc.path.name} 中未匹配到任何「约束N（标题）【硬约束】」条文",
            details={"path": str(doc.path), "pattern": RULE_RE.pattern},
        )

    rules: list[IngestedRule] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.start() : end].strip()
        rules.append(
            IngestedRule(
                rule_id=int(match.group("rule_id")),
                title=match.group("title").strip(),
                hard_soft=match.group("hard_soft").strip(),
                text=body,
            )
        )

    ids = [r.rule_id for r in rules]
    if len(set(ids)) != len(ids):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        raise IngestionError(
            f"条文编号重复：{duplicates}",
            details={"duplicates": duplicates, "ids": ids},
        )

    declared = _TOTAL_RE.search(text)
    if declared and int(declared.group(1)) != len(rules):
        raise IngestionError(
            f"总则声明共 {declared.group(1)} 条，实际抽出 {len(rules)} 条",
            details={"declared": int(declared.group(1)), "extracted": len(rules), "ids": ids},
            suggestions=["检查是否有条文被硬换行拆散而未被修复层拼回"],
        )

    expected = list(range(1, len(rules) + 1))
    if sorted(ids) != expected:
        raise IngestionError(
            f"条文编号不连续：抽到 {sorted(ids)}，期望 {expected}",
            details={"ids": sorted(ids), "expected": expected},
        )
    return tuple(rules)


__all__ = ["RULE_RE", "parse_rules_document"]
