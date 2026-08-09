"""人工确认门禁（v6 §5.1「【人工确认】硬性门禁：任何数据变更必须人工批准」）。

M1 交付**接口与全部判定逻辑**；UI 在 W9（M6 前端窗口）接上去。这不是占位：
:func:`review` 的每条规则都真的会拒绝不合规的批准请求，并有单测覆盖。

三条硬性判定：

1. **有 BLOCKING 冲突而未逐条给出裁决 → 拒绝**。X1 这类冲突不允许「整体批准
   一下就过」，必须对每个 `conflict_id` 明确给值。
2. **裁决值与 §5.5 裁定表不符 → 拒绝**，除非调用方显式 `override_adjudication`
   并给出理由（理由进审计日志）。这条是为了让「按裁定选 2026-01-07」这件事
   由**门禁**保证，而不是靠 parser 或运气。
3. **ChangeSet 为空 → 无需批准**，直接返回 `NO_CHANGE`，不产生新快照。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from backend.core.errors import DataConflictError
from backend.core.logging import get_logger
from backend.ingestion.conflicts import ADJUDICATIONS, Conflict
from backend.ingestion.diff import ChangeSet

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
    approved_by: str = ""

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
    approver: str = "",
    approve: bool = True,
) -> GateDecision:
    """审这一批变更。返回 `GateDecision`，不抛异常（拒绝也是一种结论）。"""
    provided = dict(resolutions or {})

    if changeset.is_empty and not changeset.blocking_conflicts:
        return GateDecision(outcome="NO_CHANGE", reasons=["ChangeSet 为空，无需批准"])

    if not approve:
        return GateDecision(outcome="REJECTED", reasons=["人工驳回"], approved_by=approver)

    reasons: list[str] = []
    if not approver:
        reasons.append("未提供批准人身份，任何数据变更都必须有署名")

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
        return GateDecision(outcome="REJECTED", reasons=reasons, approved_by=approver)

    logger.info(
        "人工确认门禁通过",
        approver=approver,
        **changeset.summary(),
    )
    return GateDecision(outcome="APPROVED", resolutions=provided, approved_by=approver)


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
    "baseline_resolutions",
    "resolved_expiry_dates",
    "review",
]
