"""FTS 前端（Streamlit，v6 §8）。

```
streamlit run frontend/app.py --server.port 8501
```

## 交互模型：低频轮询，无实时流式（v6 §8.1）

```
提交 ──► POST /chat | /schedule ──► job_id（立即返回）
          │
          └─► 每 1.5 s 一次 GET /jobs/{job_id}（只有阶段 + 百分比 + 状态，几百字节）
                     │
                     └─ DONE / FAILED / AWAITING_HUMAN ──► GET /runs/{trace_id} 一次取全
```

**没有 WebSocket、没有后台线程、没有事件队列。** v6 §8.1 把这条当设计约束：
轮询响应体小到可以忽略，Streamlit 的 rerun 开销也就可以忽略，
于是整个前端只剩「取数 → 渲染」这一件事。

## 四个页签 + 一个门禁

排班结果 / 约束校验 / 运作过程 / 解释报告（§8.3 那张图），
人工门禁固定在页面底部——它不属于任何一个页签，因为无论用户在看哪一页，
「这版方案要不要」都得能按下去。

## 浏览器存储：一律不用

会话态在 `st.session_state`（服务端内存），产物在 PG 与 `data/plans/`。
理由见 `frontend/state.py`。
"""

from __future__ import annotations

import time
import uuid
from datetime import date
from typing import Any

import streamlit as st

from backend.api.security import AuthError, TokenTable
from backend.core.config import Settings, get_settings
from backend.schemas.api import JobStatus, RunResultView
from frontend import state
from frontend.api_client import ApiClient, ApiError
from frontend.views import explanation as explanation_view
from frontend.views import hitl as hitl_view
from frontend.views import ingest as ingest_view
from frontend.views import process as process_view
from frontend.views import results as results_view
from frontend.views import validation as validation_view

TERMINAL_STATES = {JobStatus.DONE, JobStatus.FAILED, JobStatus.AWAITING_HUMAN}


def build_client(settings: Settings) -> ApiClient:
    return ApiClient.from_settings(settings)


def current_role(settings: Settings) -> str:
    """本前端这个 token 对应的角色。

    **本地解析，不额外加一个 `/me` 端点**：v6 §9.1 是 11 个端点，多一个就得改
    设计文档。前端与后端同机部署、读同一份 `.env`（v6 §11.1 裸装），所以
    `TokenTable` 在这里解析出来的角色与后端判定的**必然是同一个**。

    它只用于 UI 提示与 T3 的禁用态 —— **真正的鉴权在后端**，UI 这一道拦不住
    直接打 API 的人，也不打算拦。
    """
    try:
        return str(TokenTable.from_settings(settings).resolve(settings.FRONTEND_API_TOKEN).role)
    except (AuthError, ValueError):
        return "viewer"


def render_topbar(settings: Settings, run: RunResultView | None, health: dict[str, Any]) -> None:
    """顶栏：快照 / 规则版本 / 语义版本 / 跑道模型 / 离线运行标识（v6 §8.3）。"""
    snapshot = (run.snapshot_id if run else None) or "—"
    ruleset = (run.ruleset_version if run else None) or health.get("ruleset_version", "—")
    semantics = (run.semantics_version if run else None) or health.get("semantics_version", "—")
    runway = run.plan.runway_model if run and run.plan else "dual_runway"
    offline = "● 离线运行" if health.get("offline", True) else "○ 在线"
    st.markdown(
        f"### FTS 智能排班　"
        f"<span style='font-size:0.6em;font-weight:normal'>"
        f"快照 <code>{snapshot}</code> · 规则 <code>{ruleset}</code> / "
        f"语义 <code>{semantics}</code> · 跑道模型 <code>{runway}</code> · "
        f"<b>{offline}</b>（{settings.APP_ENV}）</span>",
        unsafe_allow_html=True,
    )


def submit_message(client: ApiClient, message: str) -> None:
    """提交一句话。**客户端 UUID 就是幂等键**（v6 §9.1）。"""
    payload = client.chat(
        {
            "message": message,
            "client_request_id": uuid.uuid4().hex,
        }
    )
    state.clear_run()
    state.put(state.KEY_JOB_ID, payload["job_id"])
    state.put(state.KEY_TRACE_ID, payload["trace_id"])


def submit_form(client: ApiClient, monday: date) -> None:
    """结构化排班（`POST /schedule`）。**零 LLM**，FTS-4001 的降级入口。"""
    payload = client.schedule(
        {"week_start": monday.isoformat(), "client_request_id": uuid.uuid4().hex}
    )
    state.clear_run()
    state.put(state.KEY_JOB_ID, payload["job_id"])
    state.put(state.KEY_TRACE_ID, payload["trace_id"])


def render_sidebar(client: ApiClient, settings: Settings, role: str) -> None:
    with st.sidebar:
        st.header("会话与上传")
        st.caption(f"后端 `{settings.API_BASE_URL}` · 角色 `{role}`")

        with st.expander("数据摄取 / 确认", expanded=False):
            ingest_view.render(client, approver=role)

        st.divider()
        message = st.text_area(
            "输入",
            value=state.get(state.KEY_MESSAGE, ""),
            placeholder="例：高超一周都参加不了，AC84 本周维修，重排 2026W02",
            height=120,
        )
        if st.button("提交", type="primary", key="fts_submit"):
            if not message.strip():
                st.warning("先说一句话再提交。")
            else:
                state.put(state.KEY_MESSAGE, message)
                try:
                    submit_message(client, message)
                    st.rerun()
                except ApiError as exc:
                    # FTS-4005：有人正在排这一周。把「谁在排、还剩多久」说全
                    st.error(f"{exc.code or 'ERROR'} {exc.message}")
                    for suggestion in exc.suggestions:
                        st.caption("→ " + suggestion)

        with st.expander("表单排班（LLM 不可用时的降级路径）", expanded=False):
            st.caption(
                "v6 §9.3 FTS-4001：**LLM 挂了，排班能力必须还在**。"
                "这条路径走 `POST /schedule`，一次模型调用都没有。"
            )
            monday = st.date_input("排班周的周一", value=date(2026, 1, 5), key="fts_form_monday")
            if st.button("按表单排班", key="fts_form_submit"):
                try:
                    submit_form(client, monday if isinstance(monday, date) else date.today())
                    st.rerun()
                except ApiError as exc:
                    st.error(f"{exc.code or 'ERROR'} {exc.message}")
                    for suggestion in exc.suggestions:
                        st.caption("→ " + suggestion)

        st.divider()
        st.subheader("状态")
        job_id = state.get(state.KEY_JOB_ID)
        if not job_id:
            st.caption("尚未提交任务。")
        notice = state.get(state.KEY_NOTICE)
        if notice:
            st.info(notice)

        st.divider()
        with st.expander("历史计划", expanded=False):
            week = st.text_input("ISO 周", value="", placeholder="2026-W02")
            if st.button("查询", key="fts_plans_btn"):
                try:
                    payload = client.plans(week or None)
                    st.dataframe(payload["plans"], width="stretch", hide_index=True)
                except ApiError as exc:
                    st.error(f"{exc.code or 'ERROR'} {exc.message}")


def poll_job(client: ApiClient, settings: Settings) -> RunResultView | None:
    """轮询 + 取回完整结果。返回 None 表示还没有可展示的运行。"""
    job_id = state.get(state.KEY_JOB_ID)
    trace_id = state.get(state.KEY_TRACE_ID)
    if not job_id or not trace_id:
        cached = state.get(state.KEY_RUN)
        return RunResultView.from_payload(cached) if cached else None

    try:
        job = client.job(job_id)
    except ApiError as exc:
        st.error(f"{exc.code or 'ERROR'} {exc.message}")
        return None

    status = JobStatus(job["status"])
    if status not in TERMINAL_STATES:
        with st.status(f"{job['stage']}…", expanded=True):
            st.progress(job["percent"] / 100, text=f"{job['status']} · {job['percent']}%")
            st.caption(
                f"任务 `{job_id}` · 每 {settings.FRONTEND_POLL_INTERVAL_S} 秒轮询一次"
                "（v6 §8.1：无实时流式）"
            )
        time.sleep(settings.FRONTEND_POLL_INTERVAL_S)
        st.rerun()

    if status is JobStatus.FAILED:
        st.error(f"任务失败：{job.get('error_code') or '未知错误'}", icon="🚨")

    try:
        payload = client.run(trace_id)
    except ApiError as exc:
        st.error(f"{exc.code or 'ERROR'} {exc.message}")
        return None
    state.put(state.KEY_RUN, payload)
    return RunResultView.from_payload(payload)


def render_export(client: ApiClient, run: RunResultView) -> None:
    if not run.workbook_path:
        return
    try:
        content = client.export(run.trace_id)
    except ApiError:
        return
    st.download_button(
        "⬇ 下载 xlsx",
        data=content,
        file_name=run.workbook_path.rsplit("/", 1)[-1],
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def main() -> None:
    settings = get_settings()
    st.set_page_config(page_title="FTS 智能排班", page_icon="✈", layout="wide")
    client = build_client(settings)

    try:
        health = client.health()
    except ApiError as exc:
        st.error(f"后端不可用：{exc.message}")
        health = {}

    role = current_role(settings)
    run = poll_job(client, settings)
    render_topbar(settings, run, health)
    render_sidebar(client, settings, role)

    tabs = st.tabs(["排班结果", "约束校验", "运作过程", "解释报告"])
    if run is None:
        with tabs[0]:
            st.info("提交一次排班或提问后，结果会显示在这里。")
        return

    with tabs[0]:
        results_view.render(run)
        render_export(client, run)
    with tabs[1]:
        validation_view.render(run)
    with tabs[2]:
        process_view.render(run)
    with tabs[3]:
        explanation_view.render(run)

    st.divider()
    hitl_view.render(client, run, role=role)


if __name__ == "__main__":
    main()
