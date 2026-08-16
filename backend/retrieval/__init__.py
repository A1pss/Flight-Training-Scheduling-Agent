"""检索管线（v6 §6.5）。

四阶段，装配在 :mod:`~backend.retrieval.pipeline`：

```
① 查询改写      rewrite.py    ← 唯一需要 LLM 的环节
② 三路召回      structured.py（路 A）/ bm25.py（路 B）/ vector.py（路 C）
③ 融合与精排    rrf.py（k=60）/ rerank.py（Top-20 → Top-5）
④ 带引用生成    generate.py   ← 每条断言标注来源，结构化来源优先级最高
```

配套模块：`terms.py`（术语对齐表）、`corpus.py`（路 B/C 共用的语料）、
`documents.py`（三路统一的文档形状）、`prereq_cte.py`（§6.1 的先修链递归 CTE）。

**本包不提供任何写入口。** 检索是只读的，`memory.write` 在 ACL 里只给了
摄取抽取组件（v6 §7.7.2）。
"""

from backend.retrieval.documents import RetrievedDoc, structured_doc
from backend.retrieval.prereq_cte import (
    MAX_PREREQ_DEPTH,
    PREREQ_CHAIN_SQL,
    PrereqEdge,
    evaluate_prereq,
    expand_class_ref,
    expand_prereq_refs,
    fetch_prereq_chain,
    transitive_prereqs,
)
from backend.retrieval.terms import Terminology, get_terminology, load_terminology

__all__ = [
    "MAX_PREREQ_DEPTH",
    "PREREQ_CHAIN_SQL",
    "PrereqEdge",
    "RetrievedDoc",
    "Terminology",
    "evaluate_prereq",
    "expand_class_ref",
    "expand_prereq_refs",
    "fetch_prereq_chain",
    "get_terminology",
    "load_terminology",
    "structured_doc",
    "transitive_prereqs",
]
