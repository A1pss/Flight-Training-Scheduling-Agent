"""人工门禁（v6 §7.2.4、`Z-19`）。

## 同一个门禁，两种问法 —— 这是 M5 改过的交互形状

`gate.pending_revision` 为真时，这一屏问的**不是**「这版方案要不要归档」，
而是「我把你那句话理解成这样，对不对」。三种决策的含义随之整体平移：

| 决策 | 常规 | 回显确认（`pending_revision=True`） |
|---|---|---|
| `APPROVE` | 归档 | **理解对了 → 去重解**（不是去归档！） |
| `REVISE` | 进入下一轮修订 | 换个说法重新翻译 |
| `REJECT` | 驳回，结束 | 撤回这条修订 |

所以修订轮是**两次门禁往返**（M5 §3.3）：第一屏确认翻译，第二屏才看新方案。
**按旧形状做 UI 会让用户看不到回显**，直接在一个没确认过的翻译上排了一版。

## 松弛档位 T0~T3

`T2` 的文案按 **D-6** 是「约束3 整体降级为软目标」，**不是**旧的
「A 类降至每人 1 次」——S-02 之后旧定义已成空操作。文案取自
`frontend/components/sheet4.py::TIER_LABELS`，只有一处定义。

`T3` 需训练主任授权，UI 上对不够格的角色直接禁用（后端还会再核一次，
两道都要有：UI 那道是体验，后端那道才是权限）。
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from backend.schemas.api import RunResultView
from frontend.api_client import ApiClient, ApiError
from frontend.components.sheet4 import TIER_LABELS
from frontend.state import KEY_JOB_ID, KEY_NOTICE, KEY_RUN, put


def _submit(
    client: ApiClient, trace_id: str, decision: str, *, comment: str, tiers: list[int]
) -> dict[str, Any]:
    body = {"comment": comment, "authorized_tiers": tiers}
    if decision == "APPROVE":
        return client.approve(trace_id, body)
    return client.reject(trace_id, body)


def _follow(result: dict[str, Any], notice: str) -> None:
    """把轮询切到**决策产生的那个新任务**上。

    决策是一次新的运行（`Command(resume=...)` 走的是新的 job_id，trace_id 不变）。
    不切的话前端会继续轮询已经终态的旧任务，界面停在「等人确认」——
    而后台其实已经归档完了。E2E 实测踩到：点完确认，下载按钮永远不出现。
    """
    put(KEY_JOB_ID, result["job_id"])
    put(KEY_RUN, None)
    put(KEY_NOTICE, f"{notice}，任务 {result['job_id']}")


def render(client: ApiClient, run: RunResultView, *, role: str) -> None:
    gate = run.gate
    if not gate.awaiting:
        return

    if gate.pending_revision:
        st.subheader("请确认我的理解")
        st.info(gate.revision_echo or "（本轮没有回显文案）", icon="💬")
        st.caption(
            "确认之后才会重新求解（v6 §7.3.4 第 4 条）。**这一屏的「确认」= 去重解，不是去归档。**"
        )
        approve_label = "✓ 理解对了，去重解"
        reject_label = "✗ 撤回这条修订"
    else:
        st.subheader("人工确认")
        st.caption("确认后归档：写计划表、推进训练进度、结算欠账、写跨周锚点。")
        approve_label = "✓ 确认并归档"
        reject_label = "✗ 驳回"

    if gate.open_questions:
        st.warning("还有待澄清的问题：\n\n- " + "\n- ".join(gate.open_questions))
    if gate.ambiguities:
        st.warning(
            "实体消解出现歧义：\n\n- "
            + "\n- ".join(str(item.get("question", item)) for item in gate.ambiguities)
        )

    tier = st.radio(
        "松弛档位",
        options=[0, 1, 2, 3],
        index=gate.relaxation_tier,
        format_func=lambda t: TIER_LABELS[t],
        horizontal=False,
        help="T3 需训练主任授权；后端会再核一次角色",
        disabled=gate.pending_revision,
    )
    if tier == 3 and role not in ("director", "admin"):
        st.error("Tier 3 需训练主任（director）授权，当前角色不足。", icon="🔒")

    comment = st.text_input("意见（驳回时必填写理由）", key="fts_gate_comment")

    columns = st.columns(2)
    if columns[0].button(approve_label, type="primary"):
        try:
            result = _submit(
                client,
                run.trace_id,
                "APPROVE",
                comment=comment,
                tiers=[tier] if tier else [],
            )
            _follow(result, "已提交确认")
            st.rerun()
        except ApiError as exc:
            st.error(f"{exc.code} {exc.message}")
    if columns[1].button(reject_label):
        try:
            result = _submit(client, run.trace_id, "REJECT", comment=comment, tiers=[])
            _follow(result, "已提交驳回")
            st.rerun()
        except ApiError as exc:
            st.error(f"{exc.code} {exc.message}")


__all__ = ["render"]
