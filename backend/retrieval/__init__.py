"""检索管线（v6 §6.5）。

M1 只交付 :mod:`~backend.retrieval.prereq_cte` —— §6.1 的先修链递归 CTE，
以及 S-01 的类引用展开（`compile_spec_node` 与摄取侧共用**同一份**实现，
避免两处漂移）。

`rewrite.py` / `bm25.py` / `vector.py` / `rrf.py` / `rerank.py` 属 M5 窗口。
"""

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

__all__ = [
    "MAX_PREREQ_DEPTH",
    "PREREQ_CHAIN_SQL",
    "PrereqEdge",
    "evaluate_prereq",
    "expand_class_ref",
    "expand_prereq_refs",
    "fetch_prereq_chain",
    "transitive_prereqs",
]
