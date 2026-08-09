"""人工确认门禁（v6 §5.1「【人工确认】硬性门禁：任何数据变更必须人工批准」）。

M1 交付**接口与全部判定逻辑**；UI 在 W9（M6 前端窗口）接上去。这不是占位：
:func:`review` 的每条规则都真的会拒绝不合规的批准请求，并有单测覆盖。

四条硬性判定：

1. **有 BLOCKING 冲突而未逐条给出裁决 → 拒绝**。X1 这类冲突不允许「整体批准
   一下就过」，必须对每个 `conflict_id` 明确给值。
2. **裁决值与 §5.5 裁定表不符 → 拒绝**，除非调用方显式 `override_adjudication`
   并给出理由（理由进审计日志）。这条是为了让「按裁定选 2026-01-07」这件事
   由**门禁**保证，而不是靠 parser 或运气。
3. **有 `OpenQuestion` 未回答（或答案不合法）→ 拒绝**；`resolution="upload"`
   的问题（缺整类数据）**永远无法用回答满足**，只能补传文件，并把问题原样放进
   `GateDecision.pending_questions` 交给调用方展示给用户。**门禁绝不替用户
   填默认值** —— 铁律 10「有疑问就问，不要猜」在这里是可执行的代码，
   不是注释（见 :mod:`backend.ingestion.questions`）。
4. **ChangeSet 为空且无待答问题 → 无需批准**，返回 `NO_CHANGE`，不产生新快照。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from backend.core.errors import DataConflictError, IngestionError
from backend.core.logging import get_logger
from backend.ingestion.conflicts import ADJUDICATIONS, Conflict
from backend.ingestion.diff import ChangeSet
from backend.ingestion.questions import (
    BASELINE_ANSWERS,
    QID_CYCLE_START,
    OpenQuestion,
    QuestionAnswer,
    parse_answer,
)

logger = get_logger(__name__)

GateOutcome = Literal["APPROVED", "REJECTED", "NO_CHANGE"]


@dataclass(frozen=True)
class ConflictResolution:
    """对单条冲突的人工裁决。"""

    conflict_id: str
    chosen_value: str
    decided_by: str
    #: 与 §5.5 裁定表不符时必须显式覆盖并给出理由
    override_adjudication: bool = False
    reason: str = ""


@dataclass
class GateDecision:
    """门禁结论。"""

    outcome: GateOutcome
    reasons: list[str] = field(default_factory=list)
    resolutions: dict[str, ConflictResolution] = field(default_factory=dict)
    #: 用户对 `OpenQuestion` 的回答（缺必需输入时的补充来源）
    answers: dict[str, QuestionAnswer] = field(default_factory=dict)
    approved_by: str = ""
    #: 未回答的问题 —— 拒绝时把它们原样带出来，供 CLI / UI 直接向用户展示
    pending_questions: list[OpenQuestion] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.outcome == "APPROVED"


def _adjudication_for(conflict: Conflict) -> str | None:
    entry = ADJUDICATIONS.get(conflict.kind)
    if entry is not None:
        return entry.value
    return conflict.adjudicated_value


def review(
    changeset: ChangeSet,
    resolutions: Mapping[str, ConflictResolution] | None = None,
    *,
    answers: Mapping[str, QuestionAnswer] | None = None,
    approver: str = "",
    approve: bool = True,
) -> GateDecision:
    """审这一批变更。返回 `GateDecision`，不抛异常（拒绝也是一种结论）。"""
    provided = dict(resolutions or {})
    given = dict(answers or {})

    if changeset.is_empty and not changeset.blocking_conflicts and not changeset.questions:
        return GateDecision(outcome="NO_CHANGE", reasons=["ChangeSet 为空，无需批准"])

    if not approve:
        return GateDecision(outcome="REJECTED", reasons=["人工驳回"], approved_by=approver)

    reasons: list[str] = []
    if not approver:
        reasons.append("未提供批准人身份，任何数据变更都必须有署名")

    # ── 待回答问题：必需的输入没人给 → 把问题原样抛回去，绝不替用户填 ──
    pending: list[OpenQuestion] = []
    for question in changeset.questions:
        if question.resolution == "upload":
            # 少了整整一类数据，给什么值都没用，必须补传文件
            pending.append(question)
            reasons.append(f"{question.topic}：需要补传文件，无法用回答替代")
            continue
        answer = given.get(question.question_id)
        if answer is None:
            pending.append(question)
            reasons.append(f"问题 {question.question_id}（{question.topic}）尚未回答")
            continue
        try:
            parse_answer(question, answer)
        except IngestionError as exc:
            pending.append(question)
            reasons.append(f"问题 {question.question_id} 的答案不合法：{exc.message}")

    for conflict in changeset.blocking_conflicts:
        resolution = provided.get(conflict.conflict_id)
        if resolution is None:
            reasons.append(
                f"冲突 {conflict.conflict_id}（{conflict.kind}）未给出裁决："
                f"{conflict.value_a!r} vs {conflict.value_b!r}"
            )
            continue
        if resolution.chosen_value not in (conflict.value_a, conflict.value_b):
            reasons.append(
                f"冲突 {conflict.conflict_id} 的裁决值 {resolution.chosen_value!r} "
                f"不是冲突两侧取值之一（{conflict.value_a!r} / {conflict.value_b!r}）"
            )
            continue
        expected = _adjudication_for(conflict)
        if expected is not None and resolution.chosen_value != expected:
            if not resolution.override_adjudication or not resolution.reason:
                reasons.append(
                    f"冲突 {conflict.conflict_id} 的裁决值 {resolution.chosen_value!r} 与 "
                    f"v6 §5.5 裁定 {expected!r} 不符；如确要偏离，须显式 "
                    f"override_adjudication=True 并给出理由"
                )
            else:
                logger.warning(
                    "人工偏离 §5.5 裁定表",
                    conflict_id=conflict.conflict_id,
                    chosen=resolution.chosen_value,
                    adjudicated=expected,
                    reason=resolution.reason,
                )

    if reasons:
        return GateDecision(
            outcome="REJECTED",
            reasons=reasons,
            approved_by=approver,
            pending_questions=pending,
        )

    logger.info(
        "人工确认门禁通过",
        approver=approver,
        **changeset.summary(),
    )
    return GateDecision(
        outcome="APPROVED", resolutions=provided, answers=given, approved_by=approver
    )


def baseline_resolutions(changeset: ChangeSet, *, decided_by: str) -> dict[str, ConflictResolution]:
    """按 §5.5 裁定表为全部 BLOCKING 冲突生成裁决。

    基准快照的入库要能非交互地重跑（铁律 9），但**裁定值必须来自 §5.5 裁定表**，
    不是 parser 随手挑的 —— 所以这里只对**已裁定**的冲突生成裁决；未裁定的
    留空，门禁会拒绝，逼人来看。
    """
    out: dict[str, ConflictResolution] = {}
    for conflict in changeset.blocking_conflicts:
        value = _adjudication_for(conflict)
        if value is None:
            continue
        out[conflict.conflict_id] = ConflictResolution(
            conflict_id=conflict.conflict_id,
            chosen_value=value,
            decided_by=decided_by,
            reason=ADJUDICATIONS[conflict.kind].note if conflict.kind in ADJUDICATIONS else "",
        )
    return out


def baseline_answers(changeset: ChangeSet) -> dict[str, QuestionAnswer]:
    """为基准数据集里**已经问过、业务方已经答过**的问题取回答案。

    与 :func:`baseline_resolutions` 同一口径：让基准快照能非交互重跑（铁律 9），
    而不是给系统开一条「没人答就自己填」的后门 —— 换一批新数据时
    `BASELINE_ANSWERS` 里没有对应记录，门禁照样会把问题抛给用户。
    """
    return {
        q.question_id: BASELINE_ANSWERS[q.question_id]
        for q in changeset.questions
        if q.question_id in BASELINE_ANSWERS
    }


def answered_cycle_start(decision: GateDecision) -> date | None:
    """取用户对「课程周期起点」问题的回答；没问过（文件里有）则返回 None。"""
    answer = decision.answers.get(QID_CYCLE_START)
    if answer is None:
        return None
    parsed = date.fromisoformat(answer.value.strip())
    return parsed


def format_questions(questions: Sequence[OpenQuestion]) -> str:
    """把待回答问题渲染成可直接展示给用户的中文文本（CLI 与 W9 共用）。"""
    if not questions:
        return ""
    blocks: list[str] = []
    for q in questions:
        lines = [
            f"【待确认】{q.topic}（{q.question_id}）",
            q.question,
            "",
            f"为什么要问：{q.why_it_matters}",
        ]
        if q.hints:
            lines.append("提示：")
            lines.extend(f"  - {h}" for h in q.hints)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def resolved_expiry_dates(
    decision: GateDecision, changeset: ChangeSet
) -> dict[tuple[str, str], date]:
    """把 X1 类裁决翻译成 `{(person_id, mission_class): 到期日}`，供落库时应用。"""
    out: dict[tuple[str, str], date] = {}
    by_id = {c.conflict_id: c for c in changeset.conflicts}
    for conflict_id, resolution in decision.resolutions.items():
        conflict = by_id.get(conflict_id)
        if conflict is None or not conflict.kind.startswith("X1_"):
            continue
        person_id = str(conflict.details.get("person_id", ""))
        mission_class = str(conflict.details.get("mission_class", ""))
        if not person_id or not mission_class:
            raise DataConflictError(
                f"冲突 {conflict_id} 缺少 person_id / mission_class，无法应用裁决",
                details={"conflict_id": conflict_id, "details": conflict.details},
            )
        out[(person_id, mission_class)] = date.fromisoformat(resolution.chosen_value)
    return out


__all__ = [
    "ConflictResolution",
    "GateDecision",
    "GateOutcome",
    "answered_cycle_start",
    "baseline_answers",
    "baseline_resolutions",
    "format_questions",
    "resolved_expiry_dates",
    "review",
]
