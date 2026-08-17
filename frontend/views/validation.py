"""页签二 · 约束校验（v6 §8.2 的「约束校验面板」+ §8.3）。

14 条逐条 ✅/❌，每条显示**「已检查 N 项」**，可展开看判定依据（规则原文 +
Chroma 溯源）与违规明细；末尾是格式校验三层结果。

## 「已检查 N 项」不是装饰

v6 §4.2 的脚注写得很直白：它是用来**发现「检查了 0 项」这种假通过**的。
一条规则显示「✅ 通过 · 已检查 0 项」，说明它根本没找到可检查的对象——
在界面上和「真的都合规」长得一模一样，所以必须把数字摆出来。
本页对 `checked_items == 0` 的通过项额外标一个 ⚠️。

## 缺规则也要显示

`ValidationReport.missing_rules()` 非空即说明校验没跑全，这时**不能**宣称
100% 合规（v6 §0.3）。页面顶部直接把缺的编号列出来。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from backend.schemas.api import RunResultView
from frontend.components.rules_ref import rule_references
from frontend.components.sheet4 import format_gates


def render(run: RunResultView) -> None:
    report = run.validation
    if report is None:
        st.info("本次运行还没有校验报告。")
        return

    passed = sum(1 for r in report.results if r.passed)
    missing = report.missing_rules()
    columns = st.columns(3)
    columns[0].metric("规则通过", f"{passed}/14")
    columns[1].metric("检查项合计", report.total_checked_items)
    columns[2].metric("违规合计", len(report.all_violations()))

    if missing:
        st.error(
            "以下规则**未被校验**，本次不得宣称 100% 合规（v6 §0.3）：" + "、".join(missing),
            icon="🚨",
        )

    st.caption("格式校验三层：" + format_gates(run))

    references = rule_references()
    for result in report.results:
        reference = references.get(result.rule_id)
        icon = "✅" if result.passed else "❌"
        suspicious = " ⚠️ 检查了 0 项，请核对" if result.passed and result.checked_items == 0 else ""
        title = (
            f"{icon} {result.rule_id} {result.rule_title}"
            f"　已检查 {result.checked_items} 项{suspicious}"
        )
        with st.expander(title, expanded=not result.passed):
            if reference is not None:
                st.markdown(f"**规则原文（{reference.tier}）**：{reference.statement}")
                if reference.note:
                    st.caption("口径：" + reference.note)
                st.caption(f"Chroma 溯源：`{reference.chroma_doc_id}`")
            st.caption(f"判定耗时 {result.duration_ms:.1f} ms")
            for note in result.notes:
                st.info(note, icon="📌")
            if result.violations:
                # `st.table` 而不是 `st.dataframe`：违规明细要能被复制进邮件，
                # 而 canvas 网格里的文字选不中（见 `views/results.py::_table`）
                st.table(
                    pd.DataFrame(
                        [
                            {
                                "严重度": v.severity,
                                "涉及对象": "、".join(v.subjects),
                                "明细": v.detail,
                                "处置建议": v.fix_hint or "—",
                            }
                            for v in result.violations
                        ]
                    )
                )
            else:
                st.caption("无违规明细。")


__all__ = ["render"]
