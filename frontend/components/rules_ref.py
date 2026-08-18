"""约束校验面板的「判定依据」：规则原文 + Chroma 溯源（v6 §8.2）。

## 溯源不查向量库，查的是**确定性的 doc_id**

`backend/retrieval/corpus.py::rule_docs()` 给 14 条规则各自生成一个文档，
id 形如 `rule:1.3.0:06`，文本是 `约束6·资源有效性与容量（R0，hard）：<原文>`。
那个 id 是**由规则版本与编号算出来的**，不依赖任何一次检索。

所以这里直接调 `rule_docs()`：拿到的是「Chroma 里那一条文档的 id 与正文」，
**与检索路 C 召回到的是同一份东西**，但不需要嵌入模型、不需要起 Chroma。
前端本来也不该有能力去做向量查询。

`tests/unit/test_frontend_rules_ref.py` 拿 `rule_docs()` 的 id 与本模块的
输出逐条比对——哪天 id 方案变了，这条测试会红，而不是等到界面上显示出一个
查不到的引用。

## 约束6 与约束9 的显示名与措辞

- **约束6 的显示名是「资源有效性与容量」**（v6 更名，含空域并发扫描）；
- **约束9 的说明必须写明「20 分钟窗口按跑道；7 分钟间隔全场」**（D-2）——
  这两句在 UI 上分开写是有原因的：把 7 分钟也写成「按跑道」是本项目最容易犯的
  实现错误之一（CLAUDE.md §11 反模式），界面上就把正确口径摆在那里。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.core.ruleset import Ruleset, load_ruleset
from backend.retrieval.corpus import rule_docs

#: 需要额外一句口径说明的规则（v6 §10.4 区块2 的「说明」列）。
RULE_NOTES: dict[str, str] = {
    "C06": "含空域同时段容量的并发扫描（S-10，并入约束6，对外仍称 14 条）",
    "C09": "20 分钟窗口按跑道；7 分钟间隔全场统一（D-2）",
    "C13": "先修未满足的组合按规则排除，见区块 4 阻塞项",
    "C03": "对全部学员生效，不论完成状态（S-13）；本周每一天都不可用的学员除外（Z-9）",
}


@dataclass(frozen=True)
class RuleReference:
    """一条规则的展示形态。"""

    check_id: str
    rule_id: int
    title: str
    tier: str
    statement: str
    note: str
    chroma_doc_id: str

    @property
    def label(self) -> str:
        return f"约束{self.rule_id} · {self.title}"


def rule_references(ruleset: Ruleset | None = None) -> dict[str, RuleReference]:
    """`C01..C14 → RuleReference`。"""
    rules = ruleset or load_ruleset()
    docs = {doc.metadata["rule_id"]: doc.doc_id for doc in rule_docs(rules)}
    out: dict[str, RuleReference] = {}
    for rule_id, spec in sorted(rules.rules.items()):
        out[spec.check_id] = RuleReference(
            check_id=spec.check_id,
            rule_id=rule_id,
            title=spec.title,
            tier=spec.tier,
            statement=spec.statement,
            note=RULE_NOTES.get(spec.check_id, ""),
            chroma_doc_id=docs.get(rule_id, ""),
        )
    return out


__all__ = ["RULE_NOTES", "RuleReference", "rule_references"]
