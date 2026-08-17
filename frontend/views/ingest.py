"""摄取确认页（v6 §5.1 的人工确认门禁 + §5.5 的冲突条目）。

三步：上传 → 看 Diff（含 X1/X3 冲突与待答问题）→ 逐条裁定后确认入库。

## 冲突条目要把两侧取值都摆出来

§5.5 的 X1 是「刘斌 C 类到期日：总表 2026-01-07 / 明细表 2026-02-07」。
UI 上**必须显示两个值各自的出处**，让业务方看着原始出处选，而不是看系统
推荐的那个点确定。裁定表给出的建议值预选上，但**可以改**——改了后端会要求
显式覆盖并给理由（`gate.review` 那段逻辑）。

## 待答问题分两类，界面上也必须分开

`resolution="answer"` 给个值就行（如课程开始日期）；`resolution="upload"`
**给什么值都没用，必须补传整份文件**。混在一起显示会让用户在一个填不出结果的
输入框里瞎填（v6 §9.3 FTS-1004）。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from frontend.api_client import ApiClient, ApiError
from frontend.state import KEY_CHANGESET, KEY_INGEST_JOB, get, put


def render(client: ApiClient, *, approver: str) -> None:
    st.subheader("数据摄取")
    uploads = st.file_uploader(
        "上传人员 / 飞机 / 课目 / 规则文件",
        accept_multiple_files=True,
        type=["pdf", "xlsx", "csv", "md", "txt", "docx"],
    )
    if uploads and st.button("上传并解析", key="fts_upload_btn"):
        try:
            payload = client.upload([(f.name, f.getvalue()) for f in uploads])
            put(KEY_INGEST_JOB, payload["ingest_job_id"])
            hit = "（命中幂等键，同一批文件）" if payload.get("idempotent_hit") else ""
            st.success(f"已上传：{payload['ingest_job_id']}{hit}")
            put(KEY_CHANGESET, client.changeset(payload["ingest_job_id"]))
        except ApiError as exc:
            st.error(f"{exc.code or 'ERROR'} {exc.message}")
            for suggestion in exc.suggestions:
                st.caption("→ " + suggestion)

    job_id = get(KEY_INGEST_JOB)
    changeset: dict[str, Any] | None = get(KEY_CHANGESET)
    if not job_id or not changeset:
        st.caption("尚未上传。上传后这里会显示待确认的变更、冲突与待答问题。")
        return

    st.markdown(f"**变更集** `{job_id}`　基线快照：`{changeset.get('base_snapshot_id') or '—'}`")
    summary = changeset.get("summary", {})
    if summary:
        columns = st.columns(len(summary))
        for column, (key, value) in zip(columns, summary.items(), strict=False):
            column.metric(key, value)

    changes = changeset.get("changes", [])
    if changes:
        st.table(
            pd.DataFrame([{"类型": c["kind"], "表": c["table"], "主键": c["key"]} for c in changes])
        )
    else:
        st.caption("没有变更（与当前快照一致）。")

    resolutions: dict[str, str] = {}
    conflicts = changeset.get("conflicts", [])
    if conflicts:
        st.markdown("**§5.5 数据冲突 —— 请逐条裁定**")
        for conflict in conflicts:
            options = list(conflict.get("options", {}).items())
            labels = [f"{value}（出处：{source}）" for value, source in options]
            suggested = conflict.get("adjudication")
            index = next(
                (i for i, (value, _) in enumerate(options) if value == suggested),
                0,
            )
            choice = st.radio(
                f"{conflict['conflict_id']} · {conflict['subject']}",
                options=range(len(options)),
                index=index,
                format_func=lambda i, labels=labels: labels[i],
                key=f"conflict_{conflict['conflict_id']}",
            )
            if suggested:
                st.caption(f"v6 §5.5 裁定：{suggested}（改成别的值需要显式理由）")
            resolutions[conflict["conflict_id"]] = options[int(choice)][0]

    answers: dict[str, str] = {}
    questions = changeset.get("open_questions", [])
    if questions:
        st.markdown("**待回答的问题**")
        for question in questions:
            if question["resolution"] == "upload":
                st.error(
                    f"{question['question_id']}：{question['prompt']}\n\n"
                    f"→ 这一条**必须补传整份文件**，给值没用。{question['detail']}",
                    icon="📄",
                )
                continue
            answers[question["question_id"]] = st.text_input(
                f"{question['question_id']} · {question['prompt']}",
                help=question["detail"],
                key=f"answer_{question['question_id']}",
            )

    if st.button("确认入库", type="primary", key="fts_confirm_btn"):
        try:
            result = client.confirm(
                job_id,
                {
                    "approver": approver,
                    "resolutions": resolutions,
                    "answers": {k: v for k, v in answers.items() if v},
                },
            )
            st.success(f"已入库，新快照 `{result['snapshot_id']}`")
            st.json(result["table_counts"])
        except ApiError as exc:
            st.error(f"{exc.code or 'ERROR'} {exc.message}")
            for suggestion in exc.suggestions:
                st.caption("→ " + suggestion)
            pending = exc.details.get("questions")
            if pending:
                st.json(pending)


__all__ = ["render"]
