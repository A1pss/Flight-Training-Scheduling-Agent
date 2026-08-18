"""页签三 · 运作过程（v6 §8.2）。

时间线（按 `seq` 的可折叠 expander）+ 步进回放 slider + Graphviz 调用图
（含回环次数）+ 求解面板（含跑道分配统计）。

## 步进回放是纯前端计算

v6 §8.2：「拖动即显示到第 N 步的状态。**纯前端计算，无需后端交互**」。
所有事件在 `GET /runs/{trace_id}` 时已经一次性取回，slider 只是切片。
所以拖动 slider **不会**再打后端——这也是为什么轮询响应体必须小、
而完整结果一次取回（§8.1）。

## 回放完整性显示在页面上

`seq` 必须是 `0..n-1` 的连续整数。页面把这个判定显示出来，不合格直接红字。
这条是 M6 的出口标准之一，藏在测试里不够——**运行时也要看得见**。
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from backend.schemas.api import RunResultView
from frontend.components import process as proc


def render(run: RunResultView) -> None:
    events = run.trace_events
    if not events:
        st.info("本次运行还没有轨迹事件。")
        return

    complete = proc.replay_complete(events)
    columns = st.columns(4)
    columns[0].metric("事件数", len(events))
    columns[1].metric("回放完整性", "100%" if complete else "不完整")
    columns[2].metric("节点数", len({e.agent for e in events}))
    columns[3].metric("阶段", str(run.stage))
    if not complete:
        st.error(
            "轨迹 `seq` 不连续 —— 回放不完整，步进覆盖不到全部步骤。"
            "这属于缺陷，请把 trace_id 交给运维。",
            icon="🚨",
        )

    st.markdown("**三类节点**")
    legend = st.columns(3)
    for column, kind in zip(legend, ("agent", "llm", "deterministic"), strict=False):
        fill, font = proc.NODE_COLORS[kind]
        column.markdown(
            f"<div style='background:{fill};color:{font};padding:6px 10px;border-radius:6px'>"
            f"{proc.NODE_ICONS[kind]} {proc.NODE_KIND_LABELS[kind]}</div>",
            unsafe_allow_html=True,
        )

    items = proc.timeline_items(events)
    step = st.slider(
        "步进回放",
        min_value=0,
        max_value=len(items) - 1,
        value=len(items) - 1,
        help="拖到第 N 步即只显示前 N 步（纯前端计算，不打后端）",
    )
    st.caption(f"显示第 0 ~ {step} 步，共 {len(items)} 步")

    st.markdown("**时间线**")
    for item in items[: step + 1]:
        with st.expander(f"#{item['seq']} {item['title']}"):
            st.caption(f"{item['ts']} · {proc.NODE_KIND_LABELS[item['node_kind']]}")
            st.code(
                json.dumps(item["payload"], ensure_ascii=False, indent=2, default=str),
                language="json",
            )

    st.markdown("**调用图（本次实际走过的路径，含回环次数）**")
    st.graphviz_chart(proc.call_graph_dot(events[: step + 1]))

    st.markdown("**求解面板**")
    st.table(pd.DataFrame(proc.solver_panel(run)))


__all__ = ["render"]
