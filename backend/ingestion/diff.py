"""Diff 层（v6 §5.1）：与当前 snapshot 对比，生成 ChangeSet（新增/修改/删除/冲突）。

Diff 的对象是**规范化后的实体字典**，不是 PDF 原文 —— 原文里换个空格就算变更
的话，人工确认门禁每次都要审一屏噪声。规范化由 :func:`normalize_facts` 负责，
它同时是 `snapshot.content_sha256` 的输入，所以「内容没变 → 哈希不变 → Diff
为空」三者天然一致（铁律 9）。

**首次摄取（库里没有 ACTIVE 快照）时全部实体都是 ADDED**，这不是特殊情况，
就是 Diff 的正常输出 —— 人工门禁照样要审一遍才落库。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from backend.ingestion.conflicts import Conflict
from backend.ingestion.questions import OpenQuestion
from backend.ingestion.schema import IngestedFacts

ChangeKind = Literal["ADDED", "MODIFIED", "REMOVED"]

#: 实体类别 → 主键字段名
ENTITY_KEYS = {
    "person": "person_id",
    "aircraft": "aircraft_id",
    "mission": "mission_id",
    "airspace": "airspace_id",
    "runway": "runway_id",
    "rule": "rule_id",
}


@dataclass(frozen=True)
class Change:
    """一条变更。`before` / `after` 是规范化后的实体字典。"""

    entity_type: str
    entity_id: str
    kind: ChangeKind
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    #: 具体变了哪些字段（MODIFIED 时非空）
    changed_fields: tuple[str, ...] = ()


@dataclass
class ChangeSet:
    """一次摄取相对当前快照的全部变更 + 待裁决冲突 + 待回答问题。

    `conflicts` 与 `questions` 都会阻断落库，但性质不同：冲突是「两个来源打架，
    选一个」，问题是「必需的值没人给，请直接给」（见
    :mod:`backend.ingestion.questions`）。
    """

    changes: list[Change] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    questions: list[OpenQuestion] = field(default_factory=list)
    base_snapshot_id: str | None = None

    @property
    def added(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "ADDED"]

    @property
    def modified(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "MODIFIED"]

    @property
    def removed(self) -> list[Change]:
        return [c for c in self.changes if c.kind == "REMOVED"]

    @property
    def blocking_conflicts(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.severity == "BLOCKING"]

    @property
    def is_empty(self) -> bool:
        return not self.changes

    @property
    def unanswered_questions(self) -> list[OpenQuestion]:
        return list(self.questions)

    def summary(self) -> dict[str, int]:
        return {
            "added": len(self.added),
            "modified": len(self.modified),
            "removed": len(self.removed),
            "conflicts": len(self.conflicts),
            "blocking_conflicts": len(self.blocking_conflicts),
            "open_questions": len(self.questions),
        }


def normalize_facts(facts: IngestedFacts) -> dict[str, dict[str, dict[str, Any]]]:
    """把事实规范化成 `{实体类别: {主键: 字段字典}}`。

    **一切集合都排序**：`sorted()` 过的列表 + `sort_keys=True` 的 JSON 是
    「同输入 → 同哈希」的前提。未固定的字典序进哈希是铁律 9 明列的 bug。
    """
    out: dict[str, dict[str, dict[str, Any]]] = {
        "person": {},
        "aircraft": {},
        "mission": {},
        "airspace": {},
        "runway": {},
        "rule": {},
    }

    for p in facts.persons:
        out["person"][p.person_id] = {
            "name": p.name,
            "identity": p.identity,
            "aircraft_types": sorted(p.aircraft_types),
            "completed_missions": sorted(p.completed_missions),
            "unavailable_dates": sorted(d.isoformat() for d in p.unavailable_dates),
            "qualifications": sorted(
                (
                    {
                        "mission_class": q.mission_class,
                        "level": q.level,
                        "expiry_date": q.expiry_date.isoformat() if q.expiry_date else None,
                    }
                    for q in p.qualifications
                ),
                key=lambda q: str(q["mission_class"]),
            ),
        }

    for a in facts.aircraft:
        out["aircraft"][a.aircraft_id] = {
            "aircraft_type": a.aircraft_type,
            "seats": a.seats,
            "daily_window_start": a.daily_window_start.strftime("%H:%M"),
            "daily_window_end": a.daily_window_end.strftime("%H:%M"),
            "turnaround_minutes": a.turnaround_minutes,
            "capable_missions": sorted(a.capable_missions),
            "maintenance": sorted(
                (
                    {
                        "start_ts": m.start_ts.isoformat(),
                        "end_ts": m.end_ts.isoformat(),
                        "kind": m.kind,
                        "all_day": m.all_day,
                    }
                    for m in a.maintenance
                ),
                key=lambda m: str(m["start_ts"]),
            ),
        }

    for m in facts.missions:
        out["mission"][m.mission_id] = {
            "name": m.name,
            "mission_class": m.mission_class,
            "kind": m.kind,
            "duration_minutes": m.duration_minutes,
            "cycle_weeks": m.cycle_weeks,
            "freq_days": m.freq_days,
            "weekly_required": m.weekly_required,
            "dual_required": m.dual_required,
            "prereqs": sorted(
                ({"prereq_ref": p.prereq_ref, "ref_kind": p.ref_kind} for p in m.prereqs),
                key=lambda p: str(p["prereq_ref"]),
            ),
            "aircraft_types": sorted(m.aircraft_types),
            "airspace_name": m.airspace_name,
            # 文件里给了课程开始日期就进 Diff —— 改了日期必须走人工确认
            "cycle_start": m.cycle_start.isoformat() if m.cycle_start else None,
        }

    for s in facts.airspaces:
        out["airspace"][s.airspace_id] = {
            "name": s.name,
            "capacity": s.capacity,
            "bound_missions": sorted(s.bound_missions),
        }

    for r in facts.runways:
        out["runway"][r.runway_id] = {
            "name": r.name,
            "aircraft_types": sorted(r.aircraft_types),
        }

    for rule in facts.rules:
        out["rule"][str(rule.rule_id)] = {
            "title": rule.title,
            "hard_soft": rule.hard_soft,
            "text": rule.text,
        }

    return out


def content_sha256(normalized: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> str:
    """规范化事实的内容哈希。**不含时间戳、字典序固定**（铁律 9）。"""
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def diff_normalized(
    current: Mapping[str, Mapping[str, Mapping[str, Any]]],
    incoming: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> list[Change]:
    """对两份规范化事实做逐实体 diff。"""
    changes: list[Change] = []
    for entity_type in sorted(set(current) | set(incoming)):
        old = current.get(entity_type, {})
        new = incoming.get(entity_type, {})
        for entity_id in sorted(set(old) | set(new)):
            before = old.get(entity_id)
            after = new.get(entity_id)
            if before is None and after is not None:
                changes.append(Change(entity_type, entity_id, "ADDED", None, dict(after)))
            elif after is None and before is not None:
                changes.append(Change(entity_type, entity_id, "REMOVED", dict(before), None))
            elif before is not None and after is not None and before != after:
                fields = tuple(
                    sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
                )
                changes.append(
                    Change(
                        entity_type,
                        entity_id,
                        "MODIFIED",
                        dict(before),
                        dict(after),
                        changed_fields=fields,
                    )
                )
    return changes


def build_changeset(
    incoming: IngestedFacts,
    current: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    *,
    conflicts: Sequence[Conflict] = (),
    questions: Sequence[OpenQuestion] = (),
    base_snapshot_id: str | None = None,
) -> ChangeSet:
    """生成 ChangeSet。`current` 为 None 表示库里还没有基线快照。"""
    return ChangeSet(
        changes=diff_normalized(current or {}, normalize_facts(incoming)),
        conflicts=list(conflicts),
        questions=list(questions),
        base_snapshot_id=base_snapshot_id,
    )


__all__ = [
    "ENTITY_KEYS",
    "Change",
    "ChangeKind",
    "ChangeSet",
    "build_changeset",
    "content_sha256",
    "diff_normalized",
    "normalize_facts",
]
