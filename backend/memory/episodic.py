"""情景记忆：历次排班会话、用户修改与驳回、当时的冲突与所选松弛档、审批记录。

存储 **PG + Chroma（摘要向量）**，写入时机 **每次 HITL 决策后**（v6 §6.2）。

## PG 存权威内容，Chroma 只存摘要向量

这与 §6.1 的「PG 是事实唯一真源，Chroma 只是索引」一脉相承。一条情景记忆的
`content`（JSONB）里放的是完整载荷：那一轮的方案指纹、冲突集、用户原话、
选了哪一档松弛；Chroma 里只有一句 `summary` 的向量，命中后回 PG 取权威内容。

## 遗忘：三个训练周期后归档到冷表

> 情景记忆超过 3 个训练周期后归档到冷表，仍可检索但**不参与默认召回**，
> 避免噪声稀释精度。（v6 §6.4）

「冷表」在本实现里是同一张表上的 `archived` 位，不是另一张物理表。理由：
情景记忆的量级（每次排班几条）远达不到需要分表的规模，而分表会让
「仍可检索」这件事平白多一次 UNION。**语义完全一致**：
`Corpus.filter(include_archived=False)` 是默认召回，`True` 才带上它们。

⚠️ **「3 个训练周期」的周期长度取当前快照里最长的 `cycle_weeks`**
（:func:`retention_cycle_weeks`）。v6 §6.4 只说「3 个训练周期」，而周期按课目
类别有 12 / 16 / 20 周三种（§6.3.3）。取最长的是**保守**选择：归档的代价是
「本该被想起来的事没被想起来」，比多留一阵子贵得多。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, cast

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.core.config import Settings, get_settings
from backend.core.logging import get_logger
from backend.memory.collections import COLLECTION_EPISODIC
from backend.memory.temporal import DEFAULT_RETENTION_CYCLES, active_at, archive_horizon
from backend.models.entities import Mission
from backend.models.memory import EPISODIC_KINDS, EpisodicMemory

if TYPE_CHECKING:
    from sqlalchemy import CursorResult

logger = get_logger(__name__)

#: 没有课目数据时的兜底周期长度。**只在快照里一门课都没有时用到** ——
#: 那种情况下归档策略本来就无从谈起，给一个显式的常量好过让调用方崩在
#: `max(())` 上。选 20 周与 G/H 类一致（最长的那档，最保守）。
FALLBACK_CYCLE_WEEKS: Final[int] = 20


@dataclass(frozen=True)
class EpisodeRecord:
    """一条待写入的情景记忆。"""

    session_id: str
    kind: str
    summary: str
    content: Mapping[str, Any]
    occurred_at: datetime

    def memory_id(self) -> str:
        """内容寻址的 id：**同一件事写两次是同一行**（幂等）。

        重放同一条轨迹不该在记忆库里长出第二条一模一样的记录 —— 那会让
        「上次用户驳回过几次」这个数随重放次数增长。
        """
        payload = json.dumps(
            {
                "session_id": self.session_id,
                "kind": self.kind,
                "summary": self.summary,
                "content": _canonical(self.content),
                "occurred_at": self.occurred_at.replace(microsecond=0).isoformat(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return "epi_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _canonical(value: Any) -> Any:
    """JSON 可序列化的规范形态（字典按键排序由 `json.dumps` 负责）。"""
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def record_episode(
    session: Session,
    record: EpisodeRecord,
    *,
    valid_to: datetime | None = None,
) -> EpisodicMemory:
    """写一条情景记忆。**幂等**：同一件事重复写返回同一行。"""
    if record.kind not in EPISODIC_KINDS:
        raise ValueError(f"未登记的情景记忆类型 {record.kind!r}，合法取值：{EPISODIC_KINDS}")
    memory_id = record.memory_id()
    existing = session.get(EpisodicMemory, memory_id)
    if existing is not None:
        return existing
    row = EpisodicMemory(
        memory_id=memory_id,
        session_id=record.session_id,
        kind=record.kind,
        summary=record.summary,
        content=dict(_canonical(record.content)),
        valid_from=record.occurred_at,
        valid_to=valid_to,
        superseded_by=None,
        archived=False,
        chroma_doc_id=f"epi:{memory_id}",
    )
    session.add(row)
    session.flush()
    logger.info("情景记忆已写入", memory_id=memory_id, kind=record.kind)
    return row


def record_hitl_decision(
    session: Session,
    *,
    session_id: str,
    decision: str,
    user_id: str,
    role: str,
    comment: str,
    occurred_at: datetime,
    plan_id: str | None = None,
    content_sha256: str | None = None,
    relaxation_tier: int = 0,
    conflicts: Sequence[str] = (),
    revision_utterances: Sequence[str] = (),
) -> EpisodicMemory:
    """**每次 HITL 决策后**写入（v6 §6.2 的「写入时机」那一格）。

    三种决策各自成一类：`approval`（批准）/ `user_revision`（要求修订）/
    `user_rejection`（驳回）。当时的冲突与所选松弛档一并落库 —— 下次遇到
    类似冲突时，「上次这种情况选了哪一档」是有用的经历。
    """
    kind = {
        "APPROVE": "approval",
        "REVISE": "user_revision",
        "REJECT": "user_rejection",
    }.get(decision.upper())
    if kind is None:
        raise ValueError(f"未知的人工决策 {decision!r}，必须是 APPROVE / REVISE / REJECT")

    tier_note = f"，松弛档 Tier {relaxation_tier}" if relaxation_tier else ""
    conflict_note = f"，冲突 {'、'.join(conflicts)}" if conflicts else ""
    said = f"，原话「{comment}」" if comment.strip() else ""
    summary = (
        f"{occurred_at:%Y-%m-%d} {user_id}（{role}）对方案 {plan_id or '（未归档）'} "
        f"作出 {decision.upper()} 决策{tier_note}{conflict_note}{said}"
    )
    return record_episode(
        session,
        EpisodeRecord(
            session_id=session_id,
            kind=kind,
            summary=summary,
            content={
                "decision": decision.upper(),
                "user_id": user_id,
                "role": role,
                "comment": comment,
                "plan_id": plan_id,
                "content_sha256": content_sha256,
                "relaxation_tier": relaxation_tier,
                "conflicts": list(conflicts),
                "revision_utterances": list(revision_utterances),
            },
            occurred_at=occurred_at,
        ),
    )


def search_episodes(
    session: Session,
    *,
    at: datetime,
    session_id: str | None = None,
    kinds: Sequence[str] = (),
    include_archived: bool = False,
) -> list[EpisodicMemory]:
    """按会话/类型取情景记忆，**默认加时间过滤、默认排除归档**（§6.4）。"""
    stmt = select(EpisodicMemory)
    if session_id:
        stmt = stmt.where(EpisodicMemory.session_id == session_id)
    if kinds:
        stmt = stmt.where(EpisodicMemory.kind.in_(list(kinds)))
    if not include_archived:
        stmt = stmt.where(EpisodicMemory.archived.is_(False))
    rows = list(session.scalars(stmt.order_by(EpisodicMemory.memory_id)))
    return active_at(rows, at)


def retention_cycle_weeks(session: Session, snapshot_id: str) -> int:
    """「一个训练周期」有多长 —— 取快照里**最长**的 `cycle_weeks`。

    保守选择，理由见模块开头。快照里一门课都没有时返回
    :data:`FALLBACK_CYCLE_WEEKS`。
    """
    weeks = list(
        session.scalars(select(Mission.cycle_weeks).where(Mission.snapshot_id == snapshot_id))
    )
    return max(weeks) if weeks else FALLBACK_CYCLE_WEEKS


def archive_stale_episodes(
    session: Session,
    *,
    now: datetime,
    cycle_weeks: int,
    cycles: int | None = None,
    settings: Settings | None = None,
) -> int:
    """把超过 N 个训练周期的情景记忆归档到冷表。返回归档条数。

    **归档不是删除**：`archived=True` 的条目仍可检索
    （`search_episodes(include_archived=True)`），只是不参与默认召回。
    """
    cfg = settings or get_settings()
    horizon = archive_horizon(
        now,
        cycle_weeks=cycle_weeks,
        cycles=cycles if cycles is not None else cfg.EPISODIC_RETENTION_CYCLES,
    )
    result = session.execute(
        update(EpisodicMemory)
        .where(EpisodicMemory.valid_from < horizon, EpisodicMemory.archived.is_(False))
        .values(archived=True)
    )
    # `Result` 的通用签名上没有 rowcount，DML 实际返回的是 `CursorResult`
    count = int(cast("CursorResult[Any]", result).rowcount or 0)
    if count:
        logger.info(
            "情景记忆归档",
            count=count,
            horizon=horizon.isoformat(),
            cycle_weeks=cycle_weeks,
            cycles=cycles if cycles is not None else cfg.EPISODIC_RETENTION_CYCLES,
        )
    return count


def chroma_payload(row: EpisodicMemory) -> dict[str, Any]:
    """该条记忆写进 Chroma 时的 metadata（契约见 `memory/chroma.py`）。"""
    return {
        "memory_id": row.memory_id,
        "session_id": row.session_id,
        "kind": row.kind,
        "valid_from": row.valid_from.isoformat(),
        "archived": bool(row.archived),
    }


__all__ = [
    "COLLECTION_EPISODIC",
    "DEFAULT_RETENTION_CYCLES",
    "FALLBACK_CYCLE_WEEKS",
    "EpisodeRecord",
    "archive_stale_episodes",
    "chroma_payload",
    "record_episode",
    "record_hitl_decision",
    "retention_cycle_weeks",
    "search_episodes",
]
