"""程序记忆：常用表述映射、偏好的松弛顺序、教员排班习惯（v6 §6.2 第三行）。

存储 **PG (JSONB) + LangGraph Store**，写入时机 **从情景记忆定期蒸馏**，
检索方式 **key 前缀 + 语义**。

## 两个存储各管一段，不是冗余

| 存储 | 管什么 |
|---|---|
| PG `procedural_memories` | **权威 + 版本化**：`valid_from` / `valid_to` / `superseded_by` / `source`，冲突消解也在这里做 |
| LangGraph Store | **运行时的快取**：图在跑的时候按命名空间 key 直接拿，不必开一次 DB 会话 |

写入一律先落 PG（带冲突检测），成功之后再同步一份到 Store。反过来会出现
「Store 里有、PG 里没有」的偏好 —— 那种偏好没有来源、没有时效，也就没法追责。

## 蒸馏：从经历到偏好

`distill()` 扫情景记忆，把**反复出现**的模式提炼成偏好。当前提炼两类：

1. **松弛档偏好** —— 用户批准过的方案里最常出现的 Tier，
   落 `relaxation.preferred_tier`；
2. **表述映射** —— 用户修订原话里反复出现的说法，落 `phrasing.<归一化表述>`。

**只提炼出现过 ≥2 次的模式。** 一次不算习惯 —— 把单次行为固化成偏好，
下次系统就会拿一个用户根本没打算长期坚持的选择去影响排班。

阈值做成参数（`min_support`），不写死在逻辑里。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from langgraph.store.base import BaseStore
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.core.logging import get_logger
from backend.graph.store import remember
from backend.memory.temporal import (
    SOURCE_CONVERSATION,
    SOURCE_PLAN_CONFIRMED,
    MemoryConflict,
    detect_conflict,
    latest_version,
)
from backend.models.memory import EpisodicMemory, ProceduralMemory

logger = get_logger(__name__)

#: 命名空间。与 `graph/store.py` 的 `procedural` 命名空间对应
NAMESPACE_RELAXATION: Final[str] = "relaxation"
NAMESPACE_PHRASING: Final[str] = "phrasing"
NAMESPACE_INSTRUCTOR: Final[str] = "instructor"

#: 「反复出现」的最小次数。一次不算习惯
DEFAULT_MIN_SUPPORT: Final[int] = 2


@dataclass(frozen=True)
class WriteOutcome:
    """一次程序记忆写入的结果。"""

    #: 写入成功时是新行；被拒绝或需要人工时为 None
    row: ProceduralMemory | None
    conflict: MemoryConflict | None = None

    @property
    def written(self) -> bool:
        return self.row is not None

    @property
    def needs_human(self) -> bool:
        return self.conflict is not None and self.conflict.needs_human


def _memory_id(namespace: str, key: str, valid_from: datetime) -> str:
    payload = f"{namespace}|{key}|{valid_from.replace(microsecond=0).isoformat()}"
    return "proc_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def get_preference(
    session: Session, *, namespace: str, key: str, at: datetime
) -> ProceduralMemory | None:
    """取一个偏好的**最新有效版本**（§6.4 ②）。"""
    return latest_version(_versions(session, namespace, key), at).current


def preference_with_history(
    session: Session, *, namespace: str, key: str, at: datetime
) -> tuple[ProceduralMemory | None, int]:
    """取最新有效版本 **+ 历史版本数量**（§6.4 ② 的「显式标注」）。"""
    view = latest_version(_versions(session, namespace, key), at)
    return view.current, view.history_count


def _versions(session: Session, namespace: str, key: str) -> list[ProceduralMemory]:
    return list(
        session.scalars(
            select(ProceduralMemory)
            .where(ProceduralMemory.namespace == namespace, ProceduralMemory.key == key)
            .order_by(ProceduralMemory.memory_id)
        )
    )


def put_preference(
    session: Session,
    *,
    namespace: str,
    key: str,
    value: Mapping[str, Any],
    source: str = SOURCE_CONVERSATION,
    at: datetime,
    store: BaseStore | None = None,
    tenant_id: str = "default",
) -> WriteOutcome:
    """写一个偏好，**带冲突检测**（v6 §6.4 ③）。

    三种结局：

    | 现存 vs 新写入 | 结果 |
    |---|---|
    | 值相同 | 原样返回现存行，不产生新版本 |
    | 新的可信度**严格更高** | 旧行 `superseded_by` 指向新行，新行写入 |
    | 新的可信度**严格更低** | **拒绝写入**，返回冲突（留痕，不静默丢） |
    | 同档且值不同 | **升级人工**，不写入（FTS-2001） |
    """
    current = get_preference(session, namespace=namespace, key=key, at=at)
    conflict: MemoryConflict | None = None
    if current is not None:
        conflict = detect_conflict(
            key=f"{namespace}/{key}",
            existing_id=current.memory_id,
            existing_source=current.source,
            existing_value=current.value,
            incoming_source=source,
            incoming_value=dict(value),
        )
        if conflict is None:
            return WriteOutcome(row=current)
        if conflict.resolution in ("reject", "escalate"):
            logger.warning("程序记忆写入冲突", detail=conflict.describe())
            return WriteOutcome(row=None, conflict=conflict)

    memory_id = _memory_id(namespace, key, at)
    row = ProceduralMemory(
        memory_id=memory_id,
        namespace=namespace,
        key=key,
        value=json.loads(json.dumps(dict(value), ensure_ascii=False, sort_keys=True)),
        source=source,
        valid_from=at,
        valid_to=None,
        superseded_by=None,
    )
    session.add(row)
    session.flush()

    if current is not None:
        # 旧版本让位：`valid_to` 与 `superseded_by` 一起改 —— 只改一个会让
        # `is_active_at` 与「有没有后继」两套判据打架
        session.execute(
            update(ProceduralMemory)
            .where(ProceduralMemory.memory_id == current.memory_id)
            .values(valid_to=at, superseded_by=memory_id)
        )
        session.flush()

    if store is not None:
        remember(
            store,
            tenant_id=tenant_id,
            kind="procedural",
            key=f"{namespace}/{key}",
            value={"value": dict(value), "source": source, "valid_from": at.isoformat()},
        )
    return WriteOutcome(row=row, conflict=conflict)


def list_preferences(session: Session, *, prefix: str = "", at: datetime) -> list[ProceduralMemory]:
    """按 key 前缀检索（§6.2「key 前缀 + 语义」的前半）。"""
    stmt = select(ProceduralMemory).order_by(ProceduralMemory.namespace, ProceduralMemory.key)
    if prefix:
        stmt = stmt.where(ProceduralMemory.namespace.startswith(prefix))
    rows = list(session.scalars(stmt))
    keys = {(r.namespace, r.key) for r in rows}
    out: list[ProceduralMemory] = []
    for namespace, key in sorted(keys):
        current = latest_version([r for r in rows if (r.namespace, r.key) == (namespace, key)], at)
        if current.current is not None:
            out.append(current.current)
    return out


# ─────────────────────────────────────────────────────────────────────
# 蒸馏
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DistillationReport:
    """一次蒸馏的产物。**冲突要如实报出来**，不能悄悄跳过。"""

    written: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    conflicts: tuple[MemoryConflict, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        return (
            f"蒸馏出 {len(self.written)} 条偏好，"
            f"跳过 {len(self.skipped)} 条（支持度不足），"
            f"冲突 {len(self.conflicts)} 条"
        )


def distill(
    session: Session,
    *,
    at: datetime,
    min_support: int = DEFAULT_MIN_SUPPORT,
    store: BaseStore | None = None,
    tenant_id: str = "default",
) -> DistillationReport:
    """从情景记忆蒸馏程序记忆（v6 §6.2「从情景记忆定期蒸馏」）。

    **只提炼出现过 ≥ `min_support` 次的模式。** 一次不算习惯。

    蒸馏出来的偏好来源标 `排班确认记录`（它源自已批准的方案）或 `对话推断`
    （它源自用户的原话），两者在 §6.4 的可信度序里是第 2 与第 3 档 ——
    **永远压不过 PG 事实**，这正是我们想要的：偏好不该改写事实。
    """
    episodes = list(session.scalars(select(EpisodicMemory).order_by(EpisodicMemory.memory_id)))
    written: list[str] = []
    skipped: list[str] = []
    conflicts: list[MemoryConflict] = []

    # ① 松弛档偏好：已批准方案里出现最多的那一档
    tiers: dict[int, int] = {}
    for row in episodes:
        if row.kind != "approval":
            continue
        tier = int(row.content.get("relaxation_tier") or 0)
        tiers[tier] = tiers.get(tier, 0) + 1
    if tiers:
        best_tier, support = max(sorted(tiers.items()), key=lambda pair: (pair[1], -pair[0]))
        key = "preferred_tier"
        if support >= min_support:
            outcome = put_preference(
                session,
                namespace=NAMESPACE_RELAXATION,
                key=key,
                value={"tier": best_tier, "support": support},
                source=SOURCE_PLAN_CONFIRMED,
                at=at,
                store=store,
                tenant_id=tenant_id,
            )
            _absorb(outcome, f"{NAMESPACE_RELAXATION}/{key}", written, conflicts)
        else:
            skipped.append(f"{NAMESPACE_RELAXATION}/{key}（支持度 {support} < {min_support}）")

    # ② 表述映射：修订原话里反复出现的说法
    phrases: dict[str, int] = {}
    for row in episodes:
        if row.kind != "user_revision":
            continue
        for utterance in row.content.get("revision_utterances") or []:
            text = str(utterance).strip()
            if text:
                phrases[text] = phrases.get(text, 0) + 1
    for text, support in sorted(phrases.items()):
        key = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if support < min_support:
            skipped.append(f"{NAMESPACE_PHRASING}/{key}（支持度 {support} < {min_support}）")
            continue
        outcome = put_preference(
            session,
            namespace=NAMESPACE_PHRASING,
            key=key,
            value={"utterance": text, "support": support},
            source=SOURCE_CONVERSATION,
            at=at,
            store=store,
            tenant_id=tenant_id,
        )
        _absorb(outcome, f"{NAMESPACE_PHRASING}/{key}", written, conflicts)

    report = DistillationReport(
        written=tuple(written), skipped=tuple(skipped), conflicts=tuple(conflicts)
    )
    logger.info("程序记忆蒸馏完成", summary=report.summary())
    return report


def _absorb(
    outcome: WriteOutcome,
    label: str,
    written: list[str],
    conflicts: list[MemoryConflict],
) -> None:
    if outcome.written:
        written.append(label)
    if outcome.conflict is not None and not outcome.written:
        conflicts.append(outcome.conflict)


def preference_docs(rows: Sequence[ProceduralMemory]) -> list[str]:
    """把偏好转成给检索用的句子。"""
    return [
        f"偏好 {row.namespace}/{row.key}："
        + "、".join(f"{k}={row.value[k]}" for k in sorted(row.value))
        + f"（来源：{row.source}）"
        for row in rows
    ]


__all__ = [
    "DEFAULT_MIN_SUPPORT",
    "NAMESPACE_INSTRUCTOR",
    "NAMESPACE_PHRASING",
    "NAMESPACE_RELAXATION",
    "DistillationReport",
    "WriteOutcome",
    "distill",
    "get_preference",
    "list_preferences",
    "preference_docs",
    "preference_with_history",
    "put_preference",
]
