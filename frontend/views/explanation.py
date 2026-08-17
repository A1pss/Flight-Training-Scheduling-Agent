"""页签四 · 解释报告（v6 §7.2.3 / §8.3）。

显示 `explain` 节点（或 `KnowledgeAgent`）给出的解释，以及**逐句核验的结果**。

## `supported_ratio` 不是 Faithfulness

`GroundingReport.supported_ratio` 是逐句事实核验的通过率（M5 §9.1 第 2 条特意
写明的一条）。v6 §12.4.1 的 Faithfulness 要离线 judge，而 judge 本身要先过
一致率 ≥85% + Kappa ≥0.70 的验证。**页面上标注清楚这是哪一个**，
免得有人把它当成 Faithfulness 抄进报告。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from backend.schemas.api import RunResultView


def render(run: RunResultView) -> None:
    if not run.explanation:
        st.info("本次运行还没有解释。")
        return

    st.markdown(run.explanation)

    report = run.grounding
    if report is None:
        st.caption("本次解释未附核验报告（例如零 LLM 的降级路径直接给出事实型解释）。")
        return

    columns = st.columns(2)
    columns[0].metric("逐句核验通过率", f"{report.supported_ratio:.0%}")
    columns[1].metric("存疑句", len(report.unsupported_claims))
    st.caption(
        "这是**逐句事实核验的通过率**，不是 v6 §12.4.1 的 Faithfulness"
        "——后者要离线 judge 且 judge 需先过一致性验证。"
    )

    if report.unsupported_claims:
        st.warning("以下句子没有找到依据：\n\n- " + "\n- ".join(report.unsupported_claims))

    if report.claims:
        st.table(
            pd.DataFrame(
                [
                    {
                        "句子": claim.claim,
                        "有据": "✅" if claim.supported else "❌",
                        "引用": "、".join(f"{c.source_kind}:{c.source_id}" for c in claim.citations)
                        or "—",
                    }
                    for claim in report.claims
                ]
            )
        )


__all__ = ["render"]
