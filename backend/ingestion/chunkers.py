"""自适应 Chunk 策略（v6 §5.3）—— 五种，全部实现。

| 文档类型 | 切分单元 | 大小 | Overlap | 元数据 |
|---|---|---|---|---|
| 规则条文 | **单条约束**，禁止拆分 | 不限 | 0 | `rule_id, hard_soft, ruleset_version` |
| 人员/飞机/课目表 | **单行记录 → 自然语言摘要句** | ~120 字 | 0 | `entity_type, entity_id, snapshot_id, field_map` |
| 情况文件 | 语义段落 | 300~500 字 | 80 | `doc_id, page, section, event_date` |
| 历史排班报告 | 按周 + 按小节 | 400 字 | 100 | `week, plan_version, status` |
| 会议纪要/通知 | 递归字符切分 | 400 字 | 80 | `doc_id, page` |

**「表格行 → 自然语言摘要」是检索质量的关键技巧**：表格直接切分会丢表头对应
关系，`missionA-1` 这一格单独拿出来毫无语义。摘要句让语义命中率高得多，同时
`field_map` 元数据保留回指 —— 命中后能一键跳回 PG 取**权威值**，向量库里的
摘要句永远只是索引，不是真源。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from backend.ingestion.schema import (
    IngestedAircraft,
    IngestedAirspace,
    IngestedFacts,
    IngestedMission,
    IngestedPerson,
    IngestedRule,
    IngestedRunway,
)
from backend.memory.collections import (
    COLLECTION_ENTITIES,
    COLLECTION_REPORTS,
    COLLECTION_RULES,
    COLLECTION_SITUATIONS,
)

#: 各策略的尺寸参数，与 §5.3 表格逐格一致
SITUATION_CHUNK_MIN: Final[int] = 300
SITUATION_CHUNK_MAX: Final[int] = 500
SITUATION_OVERLAP: Final[int] = 80
REPORT_CHUNK_SIZE: Final[int] = 400
REPORT_OVERLAP: Final[int] = 100
GENERIC_CHUNK_SIZE: Final[int] = 400
GENERIC_OVERLAP: Final[int] = 80


@dataclass(frozen=True)
class Chunk:
    """一个待向量化的片段。`metadata` 直接进 Chroma。"""

    chunk_id: str
    collection: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# 策略 1：规则条文 —— 单条约束，禁止拆分
# ─────────────────────────────────────────────────────────────────────
def chunk_rules(rules: Sequence[IngestedRule], *, ruleset_version: str) -> list[Chunk]:
    """一条约束 = 一个 chunk，**不设长度上限、不 overlap**。

    半条规则的检索结果是危险的 —— 「连续飞行 2 架次后」和「第 3 架次前休息不
    少于 30 分钟」分开召回，会让解释报告说出完全错误的话。
    """
    return [
        Chunk(
            chunk_id=f"rule:{rule.rule_id}",
            collection=COLLECTION_RULES,
            text=rule.text,
            metadata={
                "rule_id": rule.rule_id,
                "hard_soft": rule.hard_soft,
                "ruleset_version": ruleset_version,
                "title": rule.title,
            },
        )
        for rule in rules
    ]


# ─────────────────────────────────────────────────────────────────────
# 策略 2：表格行 → 自然语言摘要句 + field_map
# ─────────────────────────────────────────────────────────────────────
def _group_quals_by_level(person: IngestedPerson) -> str:
    """把类别资质按等级归并：`A 类单飞资质、B/C/F 类带飞资质`。"""
    by_level: dict[str, list[str]] = {}
    for qual in person.qualifications:
        by_level.setdefault(qual.level, []).append(qual.mission_class)
    parts: list[str] = []
    for level in ("教员", "单飞", "带飞"):
        classes = by_level.get(level)
        if classes:
            parts.append(f"{'/'.join(sorted(classes))} 类{level}资质")
    return "、".join(parts)


def person_summary(person: IngestedPerson) -> str:
    """v6 §5.3 给的那句模板。

    「何超（P08），学员，机型资质 JL-8，已完成 missionA-1，A 类单飞资质、
    B/C/F 类带飞资质，无不可用日期」
    """
    completed = (
        "、".join(person.completed_missions) if person.completed_missions else "无已完成课目"
    )
    completed_part = f"已完成 {completed}" if person.completed_missions else "无已完成课目"
    unavailable = (
        "无不可用日期"
        if not person.unavailable_dates
        else "不可用日期 " + "、".join(d.isoformat() for d in person.unavailable_dates)
    )
    expiry = [
        f"{q.mission_class} 类资质 {q.expiry_date.isoformat()} 到期"
        for q in person.qualifications
        if q.expiry_date
    ]
    tail = f"，{'、'.join(expiry)}" if expiry else ""
    return (
        f"{person.name}（{person.person_id}），{person.identity}，"
        f"机型资质 {'、'.join(person.aircraft_types)}，{completed_part}，"
        f"{_group_quals_by_level(person)}，{unavailable}{tail}"
    )


def aircraft_summary(aircraft: IngestedAircraft) -> str:
    maintenance = (
        "无维护计划"
        if not aircraft.maintenance
        else "、".join(
            f"{m.start_ts.date().isoformat()}{'全天' if m.all_day else ''}{m.kind}"
            for m in aircraft.maintenance
        )
    )
    return (
        f"{aircraft.aircraft_id}（{aircraft.aircraft_type}），{aircraft.seats} 座，"
        f"每日可用窗 {aircraft.daily_window_start.strftime('%H:%M')}-"
        f"{aircraft.daily_window_end.strftime('%H:%M')}，"
        f"周转 {aircraft.turnaround_minutes} 分钟，"
        f"适配课目 {'、'.join(aircraft.capable_missions)}，{maintenance}"
    )


def mission_summary(mission: IngestedMission) -> str:
    prereq = (
        "无先修"
        if not mission.prereqs
        else "先修 " + "、".join(p.prereq_ref for p in mission.prereqs)
    )
    weekly = "，每周必飞" if mission.weekly_required else ""
    return (
        f"{mission.mission_id}（{mission.name}），{mission.kind}，"
        f"时长 {mission.duration_minutes} 分钟，"
        f"{mission.cycle_weeks} 周周期、每 {mission.freq_days} 天至少 1 次{weekly}，"
        f"{prereq}，{'需带飞' if mission.dual_required else '不需带飞（可单飞）'}，"
        f"机型 {'/'.join(mission.aircraft_types)}，空域 {mission.airspace_name}"
    )


def airspace_summary(airspace: IngestedAirspace) -> str:
    return (
        f"{airspace.airspace_id}（{airspace.name}），同时段容量 {airspace.capacity}，"
        f"绑定课目 {'、'.join(airspace.bound_missions)}"
    )


def runway_summary(runway: IngestedRunway) -> str:
    return f"{runway.runway_id}（{runway.name}），服务机型 {'、'.join(runway.aircraft_types)}"


def chunk_entities(facts: IngestedFacts, *, snapshot_id: str) -> list[Chunk]:
    """每行实体一个 chunk，文本是摘要句，元数据带 `field_map` 回指 PG。"""
    chunks: list[Chunk] = []

    for person in facts.persons:
        chunks.append(
            Chunk(
                chunk_id=f"person:{person.person_id}:{snapshot_id}",
                collection=COLLECTION_ENTITIES,
                text=person_summary(person),
                metadata={
                    "entity_type": "person",
                    "entity_id": person.person_id,
                    "snapshot_id": snapshot_id,
                    "field_map": {
                        "table": "persons",
                        "pk": {"person_id": person.person_id, "snapshot_id": snapshot_id},
                        "name": person.name,
                        "identity": person.identity,
                        "aircraft_types": list(person.aircraft_types),
                        "completed_missions": list(person.completed_missions),
                        "qualifications": [
                            {
                                "mission_class": q.mission_class,
                                "level": q.level,
                                "expiry_date": q.expiry_date.isoformat() if q.expiry_date else None,
                            }
                            for q in person.qualifications
                        ],
                        "unavailable_dates": [d.isoformat() for d in person.unavailable_dates],
                    },
                },
            )
        )

    for aircraft in facts.aircraft:
        chunks.append(
            Chunk(
                chunk_id=f"aircraft:{aircraft.aircraft_id}:{snapshot_id}",
                collection=COLLECTION_ENTITIES,
                text=aircraft_summary(aircraft),
                metadata={
                    "entity_type": "aircraft",
                    "entity_id": aircraft.aircraft_id,
                    "snapshot_id": snapshot_id,
                    "field_map": {
                        "table": "aircraft",
                        "pk": {"aircraft_id": aircraft.aircraft_id, "snapshot_id": snapshot_id},
                        "aircraft_type": aircraft.aircraft_type,
                        "seats": aircraft.seats,
                        "turnaround_minutes": aircraft.turnaround_minutes,
                        "capable_missions": list(aircraft.capable_missions),
                    },
                },
            )
        )

    for mission in facts.missions:
        chunks.append(
            Chunk(
                chunk_id=f"mission:{mission.mission_id}:{snapshot_id}",
                collection=COLLECTION_ENTITIES,
                text=mission_summary(mission),
                metadata={
                    "entity_type": "mission",
                    "entity_id": mission.mission_id,
                    "snapshot_id": snapshot_id,
                    "field_map": {
                        "table": "missions",
                        "pk": {"mission_id": mission.mission_id, "snapshot_id": snapshot_id},
                        "mission_class": mission.mission_class,
                        "duration_minutes": mission.duration_minutes,
                        "freq_days": mission.freq_days,
                        "cycle_weeks": mission.cycle_weeks,
                        "weekly_required": mission.weekly_required,
                        "dual_required": mission.dual_required,
                        "prereqs": [
                            {"prereq_ref": p.prereq_ref, "ref_kind": p.ref_kind}
                            for p in mission.prereqs
                        ],
                        "airspace_name": mission.airspace_name,
                    },
                },
            )
        )

    for airspace in facts.airspaces:
        chunks.append(
            Chunk(
                chunk_id=f"airspace:{airspace.airspace_id}:{snapshot_id}",
                collection=COLLECTION_ENTITIES,
                text=airspace_summary(airspace),
                metadata={
                    "entity_type": "airspace",
                    "entity_id": airspace.airspace_id,
                    "snapshot_id": snapshot_id,
                    "field_map": {
                        "table": "airspaces",
                        "pk": {"airspace_id": airspace.airspace_id, "snapshot_id": snapshot_id},
                        "name": airspace.name,
                        "capacity": airspace.capacity,
                        "bound_missions": list(airspace.bound_missions),
                    },
                },
            )
        )

    for runway in facts.runways:
        chunks.append(
            Chunk(
                chunk_id=f"runway:{runway.runway_id}:{snapshot_id}",
                collection=COLLECTION_ENTITIES,
                text=runway_summary(runway),
                metadata={
                    "entity_type": "runway",
                    "entity_id": runway.runway_id,
                    "snapshot_id": snapshot_id,
                    "field_map": {
                        "table": "runways",
                        "pk": {"runway_id": runway.runway_id, "snapshot_id": snapshot_id},
                        "name": runway.name,
                        "aircraft_types": list(runway.aircraft_types),
                    },
                },
            )
        )

    return chunks


# ─────────────────────────────────────────────────────────────────────
# 策略 3：情况文件 —— 语义段落（中文句号/换行 + 长度上限）
# ─────────────────────────────────────────────────────────────────────
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？])|\n+")
_DATE_IN_TEXT_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _pack_sentences(text: str, *, size_min: int, size_max: int, overlap: int) -> list[str]:
    """按句子边界打包成 [size_min, size_max] 的段落，段间保留 `overlap` 字重叠。"""
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s and s.strip()]
    if not sentences:
        return []

    packed: list[str] = []
    buffer = ""
    for sentence in sentences:
        if buffer and len(buffer) + len(sentence) > size_max:
            packed.append(buffer)
            buffer = (buffer[-overlap:] if overlap else "") + sentence
        else:
            buffer = f"{buffer}{sentence}" if buffer else sentence
    if buffer:
        # 尾段太短就并回上一段，避免产出一堆几个字的碎片
        if packed and len(buffer) < size_min and len(packed[-1]) + len(buffer) <= size_max * 2:
            packed[-1] = f"{packed[-1]}{buffer[overlap:] if overlap else buffer}"
        else:
            packed.append(buffer)
    return packed


def chunk_situation(
    text: str, *, doc_id: str, page: int = 1, section: str = "", event_date: str | None = None
) -> list[Chunk]:
    """情况文件（自由文本）：语义段落 300~500 字，overlap 80 字。"""
    paragraphs = _pack_sentences(
        text, size_min=SITUATION_CHUNK_MIN, size_max=SITUATION_CHUNK_MAX, overlap=SITUATION_OVERLAP
    )
    chunks: list[Chunk] = []
    for index, paragraph in enumerate(paragraphs):
        found = _DATE_IN_TEXT_RE.search(paragraph)
        chunks.append(
            Chunk(
                chunk_id=f"situation:{doc_id}:{page}:{index}",
                collection=COLLECTION_SITUATIONS,
                text=paragraph,
                metadata={
                    "doc_id": doc_id,
                    "page": page,
                    "section": section,
                    "event_date": event_date or (found.group(0) if found else ""),
                },
            )
        )
    return chunks


# ─────────────────────────────────────────────────────────────────────
# 策略 4：历史排班报告 —— 按周 + 按小节，400 字 / overlap 100
# ─────────────────────────────────────────────────────────────────────
_SECTION_HEADING_RE = re.compile(r"^(#{1,6}\s*.+|[一二三四五六七八九十]+、.+|区块\s*\d+.*)$", re.M)


def chunk_report(text: str, *, week: str, plan_version: int, status: str) -> list[Chunk]:
    """历史排班报告：先按小节切，小节内再按 400 字 / overlap 100 打包。"""
    headings = list(_SECTION_HEADING_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    if headings:
        if headings[0].start() > 0:
            sections.append(("前言", text[: headings[0].start()].strip()))
        for i, match in enumerate(headings):
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            sections.append((match.group(0).strip(), text[match.end() : end].strip()))
    else:
        sections.append(("全文", text.strip()))

    chunks: list[Chunk] = []
    for section_name, body in sections:
        if not body:
            continue
        for index, piece in enumerate(
            _pack_sentences(
                body,
                size_min=REPORT_CHUNK_SIZE // 2,
                size_max=REPORT_CHUNK_SIZE,
                overlap=REPORT_OVERLAP,
            )
        ):
            chunks.append(
                Chunk(
                    chunk_id=f"report:{week}:v{plan_version}:{section_name}:{index}",
                    collection=COLLECTION_REPORTS,
                    text=piece,
                    metadata={
                        "week": week,
                        "plan_version": plan_version,
                        "status": status,
                        "section": section_name,
                    },
                )
            )
    return chunks


# ─────────────────────────────────────────────────────────────────────
# 策略 5：会议纪要 / 通知 —— 递归字符切分（兜底）
# ─────────────────────────────────────────────────────────────────────
#: 递归切分的分隔符梯度：先段落、再句、再逗号、最后硬切
_RECURSIVE_SEPARATORS: Final[tuple[str, ...]] = ("\n\n", "\n", "。", "；", ";", "，", ",", " ", "")


def recursive_split(text: str, *, size: int, overlap: int) -> list[str]:
    """递归字符切分：从粗到细尝试分隔符，直到每片不超过 `size`。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    for separator in _RECURSIVE_SEPARATORS:
        if separator == "":
            break
        parts = text.split(separator)
        if len(parts) == 1:
            continue
        pieces: list[str] = []
        buffer = ""
        for part in parts:
            candidate = f"{buffer}{separator}{part}" if buffer else part
            if len(candidate) > size and buffer:
                pieces.append(buffer)
                buffer = (buffer[-overlap:] if overlap else "") + part
            else:
                buffer = candidate
        if buffer:
            pieces.append(buffer)
        # 还有超长片段就对它继续递归
        result: list[str] = []
        for piece in pieces:
            result.extend(
                recursive_split(piece, size=size, overlap=overlap) if len(piece) > size else [piece]
            )
        return result

    # 所有分隔符都用尽，硬切
    step = max(1, size - overlap)
    return [text[i : i + size] for i in range(0, len(text), step)]


def chunk_generic(text: str, *, doc_id: str, page: int = 1) -> list[Chunk]:
    """会议纪要 / 通知：递归字符切分，400 字 / overlap 80。"""
    return [
        Chunk(
            chunk_id=f"doc:{doc_id}:{page}:{index}",
            collection=COLLECTION_SITUATIONS,
            text=piece,
            metadata={"doc_id": doc_id, "page": page},
        )
        for index, piece in enumerate(
            recursive_split(text, size=GENERIC_CHUNK_SIZE, overlap=GENERIC_OVERLAP)
        )
    ]


__all__ = [
    "COLLECTION_ENTITIES",
    "COLLECTION_REPORTS",
    "COLLECTION_RULES",
    "COLLECTION_SITUATIONS",
    "GENERIC_CHUNK_SIZE",
    "GENERIC_OVERLAP",
    "REPORT_CHUNK_SIZE",
    "REPORT_OVERLAP",
    "SITUATION_CHUNK_MAX",
    "SITUATION_CHUNK_MIN",
    "SITUATION_OVERLAP",
    "Chunk",
    "aircraft_summary",
    "airspace_summary",
    "chunk_entities",
    "chunk_generic",
    "chunk_report",
    "chunk_rules",
    "chunk_situation",
    "mission_summary",
    "person_summary",
    "recursive_split",
    "runway_summary",
]
