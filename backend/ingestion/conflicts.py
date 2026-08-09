"""已知数据冲突清单与裁定映射（v6 §5.5 的 X1~X4，逐条实现）。

> 摄取管线必须**主动检出**下表冲突并上报人工确认环节，由确认步骤按「裁定」列
> 选定。**检出是强制的，静默采用任一侧都算 bug。**

| # | 冲突 | 来源 A | 来源 B | 裁定 | 错误码 |
|---|---|---|---|---|---|
| X1 | 刘斌 C 类到期日 | 总表 2026-01-07 | 明细表 2026-02-07 | **取 2026-01-07** | FTS-2001 |
| X2 | 课目编号变体 | `missionC1` | `missionC-1` | 归一化 | 修复层处理，不上报 |
| X3 | 机组编成口径 | 带飞列 | 类别资质等级 | 必须一致，否则阻断 | FTS-2001 |
| X4 | 发布日期晚于基准周 | 2026-01-26 | 基准周 01-05~11 | **忽略** | WARN，不阻断 |

三种处置在代码里是三个不同的东西，不要混：

- **X1** → 产出 `Conflict`（`BLOCKING`），进 ChangeSet，等人工确认。裁定值由
  :data:`ADJUDICATIONS` 提供，**由确认步骤应用，不由 parser 应用**。
- **X2** → 修复层的职责，这里只做一次**事后核验**（还有没有漏网的变体形态），
  核验失败说明修复层退化了，抛 FTS-1003。
- **X3** → 直接阻断（`FATAL`），不给人工「确认一下就过」的选项 —— 机组编成
  口径不一致意味着两份源文件对同一件事的说法互相矛盾，只能改数据。
- **X4** → `WARN`，记一条，**不据此推导任何业务逻辑**。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Literal

from backend.core.errors import DataConflictError, ErrorCode, IngestionError
from backend.ingestion.repair import MISSION_ID_FULL_RE
from backend.ingestion.schema import IngestedFacts, IngestedMission, IngestedPerson

#: 冲突的处置级别
Severity = Literal["WARN", "BLOCKING", "FATAL"]

#: 基准周（v6 §1.2.3）。X4 用它判断发布日期是否晚于基准周。
BASELINE_WEEK_START: Final[date] = date(2026, 1, 5)
BASELINE_WEEK_END: Final[date] = date(2026, 1, 11)


@dataclass(frozen=True)
class Conflict:
    """一条检出的冲突。"""

    conflict_id: str
    kind: str
    severity: Severity
    message: str
    source_a: str
    value_a: str
    source_b: str
    value_b: str
    #: 已有裁定时给出建议取值（人工确认环节据此预选），未裁定则为 None
    adjudicated_value: str | None = None
    adjudication_note: str = ""
    error_code: ErrorCode | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def requires_human_gate(self) -> bool:
        return self.severity == "BLOCKING"


@dataclass(frozen=True)
class Adjudication:
    """v6 §5.5「裁定」列的机器可读形态。"""

    conflict_kind: str
    value: str
    note: str


#: §5.5 裁定表。**只有已裁定的冲突才有条目**；未裁定的冲突走人工，不预选。
ADJUDICATIONS: Final[dict[str, Adjudication]] = {
    "X1_刘斌C类到期日": Adjudication(
        conflict_kind="X1_刘斌C类到期日",
        value="2026-01-07",
        note="取 personnel.pdf 总表值；课目级明细的 2026-02-07 视为笔误（v6 §1.2.1 / SPEC_DECISIONS §C.1）",
    ),
}

#: 发布日期正则
_PUBLISH_DATE_RE = re.compile(r"发布日期\s*[:：]\s*(\d{4}-\d{2}-\d{2})")
#: 总表「复训到期」：`仪表等级(C类):2026-01-07`
_RECURRENT_DUE_RE = re.compile(r"[（(]([A-H])类[）)]\s*[:：]\s*(\d{4}-\d{2}-\d{2})")
#: X2 事后核验：缺连字符的课目编号变体
_MISSION_VARIANT_RE = re.compile(r"\bmission[A-H]\d\b")


# ─────────────────────────────────────────────────────────────────────
# X1 —— 同一数据源内部的值冲突（总表 vs 明细表）
# ─────────────────────────────────────────────────────────────────────
def detect_x1_expiry_conflicts(persons: Sequence[IngestedPerson]) -> list[Conflict]:
    """检出「总表复训到期」与「课目级明细到期日」的不一致。

    本函数**不挑一边**。它把两侧取值原样记进 `Conflict`，裁定值只作为
    `adjudicated_value` 建议给人工确认环节。
    """
    conflicts: list[Conflict] = []
    for person in persons:
        if not person.recurrent_due_raw:
            continue
        match = _RECURRENT_DUE_RE.search(person.recurrent_due_raw)
        if not match:
            continue
        mission_class, summary_date = match.group(1), match.group(2)

        detail = next((q for q in person.qualifications if q.mission_class == mission_class), None)
        detail_date = detail.expiry_date.isoformat() if detail and detail.expiry_date else None

        if detail_date == summary_date:
            continue

        kind = f"X1_{person.name}{mission_class}类到期日"
        # 基准数据上这条就是 §5.5 表里那条已裁定的 X1
        if person.person_id == "P04" and mission_class == "C":
            kind = "X1_刘斌C类到期日"
        adjudication = ADJUDICATIONS.get(kind)

        conflicts.append(
            Conflict(
                conflict_id=f"{person.person_id}:{mission_class}:expiry",
                kind=kind,
                severity="BLOCKING",
                message=(
                    f"{person.name}({person.person_id}) {mission_class} 类到期日在同一份 "
                    f"personnel.pdf 内自相矛盾：总表 {summary_date}，"
                    f"课目级明细 {detail_date or '(未给出)'}"
                ),
                source_a="personnel.pdf 一、人员资质总表「复训到期」",
                value_a=summary_date,
                source_b="personnel.pdf 二、课目级资质明细「到期日」",
                value_b=detail_date or "",
                adjudicated_value=adjudication.value if adjudication else None,
                adjudication_note=adjudication.note if adjudication else "",
                error_code=ErrorCode.DATA_INTEGRITY_OR_CONFLICT,
                details={
                    "person_id": person.person_id,
                    "mission_class": mission_class,
                    "summary_value": summary_date,
                    "detail_value": detail_date,
                },
            )
        )
    return conflicts


def apply_x1_resolution(person: IngestedPerson, mission_class: str, value: date) -> IngestedPerson:
    """把人工确认后的到期日写回人员记录。"""
    updated = tuple(
        q.model_copy(update={"expiry_date": value}) if q.mission_class == mission_class else q
        for q in person.qualifications
    )
    return person.model_copy(update={"qualifications": updated})


# ─────────────────────────────────────────────────────────────────────
# X2 —— 课目编号变体（修复层处理，此处只做事后核验）
# ─────────────────────────────────────────────────────────────────────
def verify_x2_no_variants(facts: IngestedFacts) -> None:
    """核验修复层确实把 `missionC1` 这类变体都归一化掉了。

    这不是「再修一次」——修复层退化时必须**响**，而不是被这里悄悄兜住。
    发现残留即抛 FTS-1003。
    """
    offenders: list[dict[str, str]] = []

    def _scan(owner: str, tokens: Iterable[str]) -> None:
        for token in tokens:
            if _MISSION_VARIANT_RE.fullmatch(token) or not MISSION_ID_FULL_RE.fullmatch(token):
                offenders.append({"owner": owner, "token": token})

    for aircraft in facts.aircraft:
        _scan(f"aircraft:{aircraft.aircraft_id}", aircraft.capable_missions)
    for person in facts.persons:
        _scan(f"person:{person.person_id}", person.completed_missions)
    for airspace in facts.airspaces:
        _scan(f"airspace:{airspace.airspace_id}", airspace.bound_missions)
    for mission in facts.missions:
        _scan(f"mission:{mission.mission_id}", [mission.mission_id])

    if offenders:
        raise IngestionError(
            f"修复层未归一化的课目编号变体：{offenders}",
            details={"offenders": offenders},
            suggestions=[
                "检查 repair.TOKEN_PATTERNS 第 5 条（missionC1 → missionC-1）是否仍在生效",
                "检查 repair_text() 的步骤顺序：去连字符必须发生在 TOKEN_PATTERNS 之前",
            ],
        )


# ─────────────────────────────────────────────────────────────────────
# X3 —— 机组编成口径（missions 带飞列 vs personnel 类别资质等级）
# ─────────────────────────────────────────────────────────────────────
def expected_qualification_level(identity: str, mission: IngestedMission) -> str:
    """§3.1.1 判定式推出的应有等级。

    `需带飞 = (mission.带飞 == 是) ∧ (person.身份 == 学员)`

    - 教员 → 「教员」（不生成受训候选，只占带飞教员岗）
    - 成熟飞行员 → 「单飞」
    - 学员 + 带飞=是 → 「带飞」
    - 学员 + 带飞=否（A-1/A-2，D-1）→ 「单飞」
    """
    if identity == "教员":
        return "教员"
    if identity == "成熟飞行员":
        return "单飞"
    return "带飞" if mission.dual_required else "单飞"


def detect_x3_crew_composition(
    persons: Sequence[IngestedPerson], missions: Sequence[IngestedMission]
) -> list[Conflict]:
    """机组编成一致性检查（§3.1.1 / §5.5 X3）。

    这条断言正是 2026-08-07 发现 `missions.pdf` 与 `personnel.pdf` 打架的那个
    检查（当时 A 类带飞列为「否」但学员资质等级写「带飞」）。把它固化进管线，
    同类冲突下次会自己冒出来，而不是被人肉发现。
    """
    by_class: dict[str, list[IngestedMission]] = {}
    for mission in missions:
        by_class.setdefault(mission.mission_class, []).append(mission)

    conflicts: list[Conflict] = []
    for person in persons:
        for qual in person.qualifications:
            class_missions = by_class.get(qual.mission_class, [])
            if not class_missions:
                continue
            expected = {expected_qualification_level(person.identity, m) for m in class_missions}
            if len(expected) > 1:
                # 同一类别里「带飞」列取值不一致，本身就是源数据打架
                conflicts.append(
                    Conflict(
                        conflict_id=f"{person.person_id}:{qual.mission_class}:crew-split",
                        kind="X3_机组编成口径",
                        severity="FATAL",
                        message=(
                            f"{qual.mission_class} 类内部的「带飞」列取值不一致，"
                            f"无法推出统一的机组编成：{sorted(expected)}"
                        ),
                        source_a="missions.pdf「带飞」列",
                        value_a=", ".join(
                            f"{m.mission_id}={'是' if m.dual_required else '否'}"
                            for m in class_missions
                        ),
                        source_b="§3.1.1 判定式",
                        value_b=", ".join(sorted(expected)),
                        error_code=ErrorCode.DATA_INTEGRITY_OR_CONFLICT,
                        details={"mission_class": qual.mission_class},
                    )
                )
                continue

            want = expected.pop()
            if qual.level != want:
                conflicts.append(
                    Conflict(
                        conflict_id=f"{person.person_id}:{qual.mission_class}:crew",
                        kind="X3_机组编成口径",
                        severity="FATAL",
                        message=(
                            f"{person.name}({person.person_id}) 身份「{person.identity}」在 "
                            f"{qual.mission_class} 类的资质等级写「{qual.level}」，"
                            f"但按 §3.1.1 判定式应为「{want}」"
                            f"（该类课目带飞列="
                            f"{'是' if class_missions[0].dual_required else '否'}）"
                        ),
                        source_a="personnel.pdf 课目级资质明细「等级」",
                        value_a=qual.level,
                        source_b="missions.pdf「带飞」列 + §3.1.1 判定式",
                        value_b=want,
                        error_code=ErrorCode.DATA_INTEGRITY_OR_CONFLICT,
                        details={
                            "person_id": person.person_id,
                            "identity": person.identity,
                            "mission_class": qual.mission_class,
                            "actual_level": qual.level,
                            "expected_level": want,
                            "dual_required": class_missions[0].dual_required,
                        },
                    )
                )
    return conflicts


# ─────────────────────────────────────────────────────────────────────
# X4 —— 发布日期晚于基准周（WARN，不阻断）
# ─────────────────────────────────────────────────────────────────────
def detect_x4_publish_dates(documents: Sequence[tuple[str, str]]) -> list[Conflict]:
    """检出发布日期晚于基准周的源文件。

    `documents` 是 (文件名, 全文) 的序列。

    ⚠️ **只记 WARN，不阻断，更不要据此推导任何业务逻辑**（v6 §1.2.3）：这是
    合成数据的时间戳瑕疵，不是「数据来自未来所以基准周作废」。
    """
    conflicts: list[Conflict] = []
    for filename, text in documents:
        match = _PUBLISH_DATE_RE.search(text)
        if not match:
            continue
        published = date.fromisoformat(match.group(1))
        if published <= BASELINE_WEEK_END:
            continue
        conflicts.append(
            Conflict(
                conflict_id=f"{filename}:publish-date",
                kind="X4_发布日期晚于基准周",
                severity="WARN",
                message=(
                    f"{filename} 的发布日期 {published} 晚于基准周 "
                    f"{BASELINE_WEEK_START}~{BASELINE_WEEK_END}；"
                    f"属合成数据的时间戳瑕疵，忽略即可，不得据此推导任何业务逻辑"
                ),
                source_a=f"{filename} 发布日期",
                value_a=published.isoformat(),
                source_b="基准周（v6 §1.2.3）",
                value_b=f"{BASELINE_WEEK_START}~{BASELINE_WEEK_END}",
                adjudicated_value=None,
                adjudication_note="忽略（v6 §5.5 X4）",
                details={"filename": filename, "published": published.isoformat()},
            )
        )
    return conflicts


# ─────────────────────────────────────────────────────────────────────
# 汇总
# ─────────────────────────────────────────────────────────────────────
def detect_all(facts: IngestedFacts, documents: Sequence[tuple[str, str]] = ()) -> list[Conflict]:
    """跑完 X1~X4 的全部检出逻辑。

    X2 走核验（发现残留直接抛 FTS-1003），X3 的 FATAL 由调用方在校验层统一
    抛出 —— 这里只负责**检出并如实返回**，处置策略集中在一个地方决定。
    """
    verify_x2_no_variants(facts)
    conflicts: list[Conflict] = []
    conflicts.extend(detect_x1_expiry_conflicts(facts.persons))
    conflicts.extend(detect_x3_crew_composition(facts.persons, facts.missions))
    conflicts.extend(detect_x4_publish_dates(documents))
    return conflicts


def raise_on_fatal(conflicts: Sequence[Conflict]) -> None:
    """有 FATAL 冲突就抛 FTS-2001 阻断入库。"""
    fatal = [c for c in conflicts if c.severity == "FATAL"]
    if not fatal:
        return
    raise DataConflictError(
        "机组编成一致性断言失败，" + "；".join(c.message for c in fatal),
        details={
            "conflicts": [
                {
                    "kind": c.kind,
                    "conflict_id": c.conflict_id,
                    "source_a": c.source_a,
                    "value_a": c.value_a,
                    "source_b": c.source_b,
                    "value_b": c.value_b,
                    **c.details,
                }
                for c in fatal
            ]
        },
        suggestions=[
            "两份源文件对同一件事说法矛盾，只能改数据，不能靠人工确认放行",
            "参考 v6 §3.1.1 判定式：需带飞 = (mission.带飞==是) ∧ (身份==学员)",
        ],
    )


__all__ = [
    "ADJUDICATIONS",
    "BASELINE_WEEK_END",
    "BASELINE_WEEK_START",
    "Adjudication",
    "Conflict",
    "Severity",
    "apply_x1_resolution",
    "detect_all",
    "detect_x1_expiry_conflicts",
    "detect_x3_crew_composition",
    "detect_x4_publish_dates",
    "expected_qualification_level",
    "raise_on_fatal",
    "verify_x2_no_variants",
]
