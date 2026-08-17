"""会话状态（**只用 `st.session_state`，绝不碰浏览器存储**）。

## 为什么不许用 localStorage / sessionStorage / cookie

CLAUDE.md §11 的反模式清单里写着「在 artifacts / 前端里用 localStorage」。
在这个系统里它还有一层具体的害处：排班方案与校验结论是**有版本、有快照、有
审批人**的东西，一旦某一份缓存在浏览器里，用户看到的可能是三天前那版
——而它看起来和最新的一模一样。

`st.session_state` 活在**服务端进程内存**里，随会话结束而去；真正要留下来的
东西（快照、计划、轨迹）在 PG 里。`tests/e2e` 有一条专门断言页面
**不出现任何 `localStorage` / `sessionStorage` / `document.cookie` 调用**。

## 键都在这里，不散在各个页面

散着写的后果是「某个页面写 `run`、另一个读 `current_run`」，而这类 bug 不报错，
只是界面上什么都不显示。
"""

from __future__ import annotations

from typing import Any

import streamlit as st

KEY_JOB_ID = "fts_job_id"
KEY_TRACE_ID = "fts_trace_id"
KEY_RUN = "fts_run"
KEY_INGEST_JOB = "fts_ingest_job"
KEY_CHANGESET = "fts_changeset"
KEY_MESSAGE = "fts_message"
KEY_NOTICE = "fts_notice"
KEY_STEP = "fts_replay_step"
KEY_TIER = "fts_relax_tier"

ALL_KEYS: tuple[str, ...] = (
    KEY_JOB_ID,
    KEY_TRACE_ID,
    KEY_RUN,
    KEY_INGEST_JOB,
    KEY_CHANGESET,
    KEY_MESSAGE,
    KEY_NOTICE,
    KEY_STEP,
    KEY_TIER,
)


def get(key: str, default: Any = None) -> Any:
    return st.session_state.get(key, default)


def put(key: str, value: Any) -> None:
    st.session_state[key] = value


def clear_run() -> None:
    """开新一轮之前清掉上一轮的运行态（**不清摄取态**，那是另一条线）。"""
    for key in (KEY_JOB_ID, KEY_TRACE_ID, KEY_RUN, KEY_STEP):
        st.session_state.pop(key, None)


__all__ = [
    "ALL_KEYS",
    "KEY_CHANGESET",
    "KEY_INGEST_JOB",
    "KEY_JOB_ID",
    "KEY_MESSAGE",
    "KEY_NOTICE",
    "KEY_RUN",
    "KEY_STEP",
    "KEY_TIER",
    "KEY_TRACE_ID",
    "clear_run",
    "get",
    "put",
]
