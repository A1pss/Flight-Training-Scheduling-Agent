"""页签一 · 排班结果（v6 §8.3）。

三表（Sheet 1~3）+ **Sheet 4 七区块预览** + 周甘特图 + BLOCKED 黄色提示。

## BLOCKED 提示为什么是黄色而不是红色

被先修挡住的组合**不是错误**——它是规则正确生效的结果（约束13），基准周就该有
7 项。红色会让排班员以为排班失败了；黄色说的是「这里有事情你得知道」。
真正的错误（`INFEASIBLE` / 校验未过）另有红色提示，两者在配色上必须分开
（与铁律 8 的三态分色同一条道理）。
"""

from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from backend.schemas.api import RunResultView
from frontend.components import sheet4, tables


def _table(rows: list[dict[str, Any]], *, empty: str) -> None:
    """渲染一张预览表。

    **用 `st.table` 而不是 `st.dataframe`**：后者是 canvas 网格，内容不进 DOM
    —— 屏幕上看得见，但选不中、复制不了，E2E 也读不到（实测：Sheet 4 的
    「语义开关」在页面文本里根本不存在）。这几张表最多几十行，交互性换成
    可读性是划算的；真正要拿去用的产物是归档的 xlsx。
    """
    if not rows:
        st.caption(empty)
        return
    st.table(pd.DataFrame(rows))


def render_gantt(run: RunResultView) -> None:
    """周甘特图：横轴当天分钟数，纵轴星期，颜色按飞机。"""
    rows = tables.gantt_rows(run.plan)
    if not rows:
        st.caption("暂无架次可画")
        return
    frame = pd.DataFrame(rows)
    order = [w for w in tables.WEEKDAY_ORDER if w in set(frame["星期"])]
    chart = (
        alt.Chart(frame)
        .mark_bar(cornerRadius=3, height=14)
        .encode(
            x=alt.X("起飞分钟:Q", title="当日时刻（分钟，06:00=360）", scale=alt.Scale(zero=False)),
            x2="着陆分钟:Q",
            y=alt.Y("星期:N", sort=order, title=""),
            color=alt.Color("飞机:N", title="飞机"),
            tooltip=["架次", "飞机", "跑道", "课目", "起飞", "着陆", "机组"],
        )
        .properties(height=28 * max(1, len(order)) + 60)
    )
    st.altair_chart(chart, use_container_width=True)


def render(run: RunResultView) -> None:
    plan = run.plan
    summary = tables.week_summary(plan)
    columns = st.columns(5)
    for column, (label, value) in zip(columns, summary.items(), strict=False):
        column.metric(label, value)

    banner = sheet4.blocked_banner(plan)
    if banner:
        # 黄色：这是规则生效的结果，不是错误（见模块注释）
        st.warning(banner, icon="⚠️")

    if plan is None:
        st.info("本次运行还没有方案。可能是问答类请求，或求解未完成。")
        return

    sheet_tabs = st.tabs(["分日飞行计划", "飞行员训练", "飞机排班", "合规与解释（Sheet 4）"])
    with sheet_tabs[0]:
        _table(tables.sheet1_rows(plan), empty="本周没有架次")
    with sheet_tabs[1]:
        _table(tables.sheet2_rows(plan), empty="本周没有架次")
    with sheet_tabs[2]:
        _table(tables.sheet3_rows(plan), empty="本周没有架次")
    with sheet_tabs[3]:
        st.caption(
            "以下七个区块与归档 Excel 的 Sheet 4 同源（v6 §10.4）。"
            "跑道只出现在区块 7 —— Sheet 1~3 不加跑道列，以免偏离版式基准。"
        )
        for title, rows in sheet4.all_blocks(run):
            st.markdown(f"**{title}**")
            _table(rows, empty="（本区块本次为空）")
            if title.startswith("区块 2"):
                st.caption("格式校验三层：" + sheet4.format_gates(run))

    st.markdown("**周甘特图**")
    render_gantt(run)


__all__ = ["render", "render_gantt"]
