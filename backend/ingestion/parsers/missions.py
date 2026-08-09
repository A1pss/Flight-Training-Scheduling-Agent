"""`missions.pdf` → :class:`IngestedMission`。

一张表：课目编号 / 名称 / 类型 / 时长(分) / 课程周期与频率要求 / 先修 /
带飞 / 机型 / 空域航线。

两个要点：

- **「课程周期与频率要求」要拆成三个字段**：`12周,每3天≥1次(每周必飞)` →
  `cycle_weeks=12`、`freq_days=3`、`weekly_required=True`。`freq_days` 是 B.4
  裁定的滑动窗口长度，A 类 3、B~F 类 7、G/H 类 14。
- **「带飞」列就是 D-1 的落点**：A-1/A-2 写「否」，所以学员 A 类**单飞**。
  这一列与 `personnel.pdf` 的类别资质等级必须一致（§5.5 X3），一致性断言在
  :mod:`backend.ingestion.validate`。
"""

from __future__ import annotations

import re
from datetime import date

from backend.core.errors import IngestionError
from backend.ingestion.adapters import ExtractedDocument
from backend.ingestion.parsers.tables import collect_tables, require_header, row_to_mapping
from backend.ingestion.repair import is_null_token, split_list
from backend.ingestion.schema import IngestedMission, IngestedPrereq

MISSION_SIGNATURE = (
    "课目编号",
    "名称",
    "类型",
    "时长(分)",
    "课程周期与频率要求",
    "先修",
    "带飞",
    "机型",
)

#: `12周,每3天≥1次(每周必飞)` / `20周,每14天≥1次`
_CYCLE_RE = re.compile(r"(\d+)\s*周")
_FREQ_RE = re.compile(r"每\s*(\d+)\s*天\s*[≥>=]+\s*1\s*次")
_WEEKLY_RE = re.compile(r"每周必飞")
#: 先修引用：类别（`A类`）或课目编号（`missionC-1`）
_CLASS_REF_RE = re.compile(r"^([A-H])类$")
_MISSION_REF_RE = re.compile(r"^mission[A-H]-\d$")
#: 空域列的表头在 PDF 里是「空域/航线」，NFKC 后不变
_AIRSPACE_COLUMNS = ("空域/航线", "空域航线", "空域")

#: **可选**的「课程开始日期」列名（`training_progress.cycle_start` 的来源）。
#:
#: 当前四份基准 PDF 都没有这一列 —— 这不是缺陷，是那批数据就没提供。列在这里的
#: 任一名字只要出现在课目表表头里，parser 就会读它、落库就会用它，**不需要改代码**，
#: 而且逐行读，各门课目的起点可以不同。
#: 整表都没有该列时，摄取会生成 `Q_cycle_start` 问题并由人工确认门禁**向用户提问**
#: （见 :mod:`backend.ingestion.questions`），**不设静默默认值**。
CYCLE_START_COLUMNS = (
    "课程开始日期",
    "课程起始日期",
    "周期起点",
    "周期开始日期",
    "开始日期",
    "起始日期",
)
_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")


def parse_frequency(mission_id: str, cell: str) -> tuple[int, int, bool]:
    """解析「课程周期与频率要求」→ (cycle_weeks, freq_days, weekly_required)。"""
    cycle = _CYCLE_RE.search(cell)
    freq = _FREQ_RE.search(cell)
    if not cycle or not freq:
        raise IngestionError(
            f"{mission_id} 的周期/频率无法解析：{cell!r}",
            details={"mission_id": mission_id, "cell": cell},
            suggestions=["期望形如「12周，每3天≥1次（每周必飞）」"],
        )
    return int(cycle.group(1)), int(freq.group(1)), bool(_WEEKLY_RE.search(cell))


def parse_prereqs(mission_id: str, cell: str) -> tuple[IngestedPrereq, ...]:
    """解析「先修」列。类别引用不在这里展开（S-01 的展开在 compile_spec）。"""
    refs = split_list(cell)
    prereqs: list[IngestedPrereq] = []
    for ref in refs:
        if _CLASS_REF_RE.match(ref):
            prereqs.append(IngestedPrereq(prereq_ref=ref, ref_kind="class"))
        elif _MISSION_REF_RE.match(ref):
            prereqs.append(IngestedPrereq(prereq_ref=ref, ref_kind="mission"))
        else:
            raise IngestionError(
                f"{mission_id} 的先修项 {ref!r} 既不是课目编号也不是类别",
                details={"mission_id": mission_id, "prereq_ref": ref, "cell": cell},
                suggestions=["合法形态：`missionC-1` 或 `A类`"],
            )
    return tuple(prereqs)


def parse_cycle_start(mission_id: str, cell: str) -> date | None:
    """解析「课程开始日期」单元格。空值返回 None；有值但解析不出来则阻断。

    接受 `2026-01-05` / `2026/01/05` / `2026年1月5日` 三种写法。
    **绝不猜**：写了个看不懂的东西就抛 FTS-1003，不静默当作「没填」——
    静默降级正是铁律 7 禁止的。
    """
    if is_null_token(cell):
        return None
    match = _DATE_RE.search(cell)
    if not match:
        raise IngestionError(
            f"{mission_id} 的课程开始日期无法解析：{cell!r}",
            details={"mission_id": mission_id, "cell": cell},
            suggestions=["期望形如 2026-01-05 / 2026/01/05 / 2026年1月5日"],
        )
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise IngestionError(
            f"{mission_id} 的课程开始日期不是合法日期：{cell!r}",
            details={"mission_id": mission_id, "cell": cell, "error": str(exc)},
        ) from exc


def _cycle_start_column(header: tuple[str, ...]) -> str | None:
    """找「课程开始日期」列。**找不到就返回 None，这是合法情形，不报错。**"""
    for name in CYCLE_START_COLUMNS:
        if name in header:
            return name
    return None


def _airspace_column(header: tuple[str, ...]) -> str:
    for name in _AIRSPACE_COLUMNS:
        if name in header:
            return name
    raise IngestionError(
        "课目表中未找到空域列",
        details={"header": list(header), "accepted": list(_AIRSPACE_COLUMNS)},
    )


def parse_missions_document(doc: ExtractedDocument) -> tuple[IngestedMission, ...]:
    """`missions.pdf` 主入口。"""
    header = require_header(doc, MISSION_SIGNATURE)
    airspace_col = _airspace_column(header)
    cycle_start_col = _cycle_start_column(header)  # 可选列，没有就是 None

    missions: list[IngestedMission] = []
    for row in collect_tables(doc, MISSION_SIGNATURE):
        rec = row_to_mapping(header, row)
        mission_id = rec["课目编号"].strip()
        if not mission_id or mission_id == "课目编号":
            continue

        cycle_weeks, freq_days, weekly_required = parse_frequency(
            mission_id, rec["课程周期与频率要求"]
        )
        dual_cell = rec["带飞"].strip()
        if dual_cell not in ("是", "否"):
            raise IngestionError(
                f"{mission_id} 的「带飞」列取值 {dual_cell!r} 不是「是」或「否」",
                details={"mission_id": mission_id, "cell": dual_cell},
            )

        missions.append(
            IngestedMission(
                mission_id=mission_id,
                name=rec["名称"].strip(),
                mission_class=mission_id[len("mission")],  # type: ignore[arg-type]
                kind=rec["类型"].strip(),
                duration_minutes=int(rec["时长(分)"].strip()),
                cycle_weeks=cycle_weeks,
                freq_days=freq_days,
                weekly_required=weekly_required,
                dual_required=dual_cell == "是",
                prereqs=()
                if is_null_token(rec["先修"])
                else parse_prereqs(mission_id, rec["先修"]),
                aircraft_types=tuple(split_list(rec["机型"], allow_slash=True)),  # type: ignore[arg-type]
                airspace_name=rec[airspace_col].strip(),
                frequency_text=rec["课程周期与频率要求"].strip(),
                cycle_start=(
                    parse_cycle_start(mission_id, rec[cycle_start_col]) if cycle_start_col else None
                ),
            )
        )

    if not missions:
        raise IngestionError("课目频率标准表未抽出任何记录", details={"path": str(doc.path)})
    return tuple(missions)


__all__ = [
    "CYCLE_START_COLUMNS",
    "MISSION_SIGNATURE",
    "parse_cycle_start",
    "parse_frequency",
    "parse_missions_document",
    "parse_prereqs",
]
