"""`aircraft.pdf` → :class:`IngestedAircraft` + :class:`IngestedAirspace`。

两张表：

- **一、飞机资源明细** —— 机号 / 机型 / 座位 / 每日可用窗 / 周转(分) /
  维护计划 / 适配课目。⚠️ **AC73 是 JL-8 不是 JL-9**；JL-9 只有 AC84、AC95。
- **二、空域/航线资源与容量**（跨第 1、2 页，RNG 单独在第 2 页）——
  空域编号 / 名称 / 同时段容量 / 绑定课目。容量按 S-10 是**硬约束**。

「适配课目」列是 §5.5 X2 的现场：原始字节里 `missionC-` 与 `1` 分居两行，
修复层断词拼接后先变成 `missionC1`，再由 TOKEN_PATTERNS 第 5 条还原为
`missionC-1`。不修就是外键失配。
"""

from __future__ import annotations

import re
from datetime import datetime, time

from backend.core.errors import IngestionError
from backend.ingestion.adapters import ExtractedDocument
from backend.ingestion.parsers.tables import collect_tables, require_header, row_to_mapping
from backend.ingestion.repair import extract_mission_tokens, is_null_token
from backend.ingestion.schema import IngestedAircraft, IngestedAirspace, IngestedMaintenance

AIRCRAFT_SIGNATURE = ("机号", "机型", "座位", "每日可用窗", "周转(分)", "维护计划", "适配课目")
AIRSPACE_SIGNATURE = ("空域编号", "名称", "同时段容量", "绑定课目")

#: 每日可用窗：`06:00-18:00`
_WINDOW_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-~—]\s*(\d{1,2}:\d{2})")
#: 维护计划：`2026-01-09 00:00-23:59 定检维护(全天)`
_MAINTENANCE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s*(\d{1,2}:\d{2})\s*[-~—]\s*(\d{1,2}:\d{2})\s*(?P<kind>[^()（）]*)"
)


def _parse_time(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def parse_maintenance(aircraft_id: str, cell: str) -> tuple[IngestedMaintenance, ...]:
    """解析「维护计划」列。空值返回空元组，非空但解析不出来则阻断。"""
    if is_null_token(cell):
        return ()
    matches = list(_MAINTENANCE_RE.finditer(cell))
    if not matches:
        raise IngestionError(
            f"{aircraft_id} 的维护计划无法解析：{cell!r}",
            details={"aircraft_id": aircraft_id, "cell": cell},
        )
    entries: list[IngestedMaintenance] = []
    for m in matches:
        day = m.group(1)
        start = datetime.fromisoformat(f"{day}T{m.group(2)}:00")
        end_time = m.group(3)
        # `23:59` 是「全天」的表达；按闭区间理解会漏掉最后 60 秒，统一收到当日 24:00
        all_day = m.group(2) == "00:00" and end_time == "23:59"
        end = (
            datetime.fromisoformat(f"{day}T00:00:00").replace(hour=23, minute=59, second=59)
            if all_day
            else datetime.fromisoformat(f"{day}T{end_time}:00")
        )
        kind = (m.group("kind") or "").strip() or "定检维护"
        entries.append(
            IngestedMaintenance(
                aircraft_id=aircraft_id, start_ts=start, end_ts=end, kind=kind, all_day=all_day
            )
        )
    return tuple(entries)


def parse_aircraft_document(
    doc: ExtractedDocument,
) -> tuple[tuple[IngestedAircraft, ...], tuple[IngestedAirspace, ...]]:
    """`aircraft.pdf` 主入口，返回 (飞机, 空域)。"""
    aircraft_header = require_header(doc, AIRCRAFT_SIGNATURE)
    airspace_header = require_header(doc, AIRSPACE_SIGNATURE)

    fleet: list[IngestedAircraft] = []
    for row in collect_tables(doc, AIRCRAFT_SIGNATURE):
        rec = row_to_mapping(aircraft_header, row)
        aircraft_id = rec["机号"].strip()
        if not aircraft_id or aircraft_id == "机号":
            continue

        window = _WINDOW_RE.search(rec["每日可用窗"])
        if not window:
            raise IngestionError(
                f"{aircraft_id} 的每日可用窗无法解析：{rec['每日可用窗']!r}",
                details={"aircraft_id": aircraft_id, "cell": rec["每日可用窗"]},
            )

        fleet.append(
            IngestedAircraft(
                aircraft_id=aircraft_id,
                aircraft_type=rec["机型"].strip(),  # type: ignore[arg-type]
                seats=int(rec["座位"].strip()),
                daily_window_start=_parse_time(window.group(1)),
                daily_window_end=_parse_time(window.group(2)),
                turnaround_minutes=int(rec["周转(分)"].strip()),
                capable_missions=tuple(extract_mission_tokens(rec["适配课目"])),
                maintenance=parse_maintenance(aircraft_id, rec["维护计划"]),
            )
        )

    airspaces: list[IngestedAirspace] = []
    for row in collect_tables(doc, AIRSPACE_SIGNATURE):
        rec = row_to_mapping(airspace_header, row)
        airspace_id = rec["空域编号"].strip()
        if not airspace_id or airspace_id == "空域编号":
            continue
        airspaces.append(
            IngestedAirspace(
                airspace_id=airspace_id,
                name=rec["名称"].strip(),
                capacity=int(rec["同时段容量"].strip()),
                bound_missions=tuple(extract_mission_tokens(rec["绑定课目"])),
            )
        )

    if not fleet:
        raise IngestionError("飞机资源明细未抽出任何记录", details={"path": str(doc.path)})
    if not airspaces:
        raise IngestionError("空域资源表未抽出任何记录", details={"path": str(doc.path)})
    return tuple(fleet), tuple(airspaces)


__all__ = [
    "AIRCRAFT_SIGNATURE",
    "AIRSPACE_SIGNATURE",
    "parse_aircraft_document",
    "parse_maintenance",
]
