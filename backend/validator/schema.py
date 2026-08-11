"""格式校验第 1~2 层（v6 §4.3）。

```
闸门2  Pydantic 契约  →  ① Schema 层：类型 / HH:MM / 枚举 / 编号格式
                        ② 业务完整性层：外键存在性 + 三表交叉一致性
```

## 编号格式不在这里重抄一遍

`^P\\d+$` / `^AC\\d+$` / `^mission[A-Z]-\\d+$` / `^RWY-\\d+$` / `^S\\d{6}$` 全部由
`backend.schemas.plan` 的 `PERSON_ID_PATTERN` 等常量定义，本模块**直接 import**。
散在两处迟早漂，而「摄取通过、求解通过、组装 SchedulePlan 才 ValidationError」
正是 2026-08-11 那次口径不一致的后果（v6 附录 B 脚注）。

按 v6 §5.1.1（业务方 2026-08-11 裁定 Z-4）：编号**只固定前缀、不限位数**；
**`airspace_id` 不是枚举**，只校验非空 + 外键存在性（换机场就换空域编号）；
`role` / `weekday` / `runway_model` 保持枚举 —— 它们是规格。

## 三表交叉一致性为什么单独一层

Sheet 1（按日）/ Sheet 2（按人）/ Sheet 3（按机）是**同一份数据的三种投影**，
这是最易出错处（v6 §4.3）。本模块把 `SchedulePlan` 投影成三张表再逐字段比对；
`validator/workbook.py` 回读 xlsx 后复用同一套比对逻辑，于是「Excel 写串了一行」
与「投影代码写错了」都会在这里现形。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import time
from typing import Any

from pydantic import ValidationError

from backend.schemas.plan import SchedulePlan, Sortie
from backend.validator.context import ValidationContext

#: Sheet 2 / Sheet 3 的二级分组顺序（v6 §10.2 / §10.3）
WEEKDAY_ORDER: tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# ─────────────────────────────────────────────────────────────────────
# ① Schema 层
# ─────────────────────────────────────────────────────────────────────
def validate_plan_schema(payload: Mapping[str, Any]) -> tuple[SchedulePlan | None, list[str]]:
    """把外部载荷（JSON / dict）校验成 `SchedulePlan`。

    返回 `(plan, errors)`：任一字段不合契约时 `plan` 为 None、`errors` 逐条给出
    `loc: msg` 形态的可读原因。**不抛异常** —— 闸门2 要把全部问题一次报完，
    而不是在第一条上中断。
    """
    try:
        return SchedulePlan.model_validate(payload), []
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        ]
        return None, errors


# ─────────────────────────────────────────────────────────────────────
# ② 业务完整性层 —— 外键
# ─────────────────────────────────────────────────────────────────────
def check_referential_integrity(plan: SchedulePlan, ctx: ValidationContext) -> list[str]:
    """外键存在性：人员 / 课目 / 飞机 / 空域 / 跑道都必须在快照里存在。"""
    errors: list[str] = []
    for s in sorted(plan.sorties, key=lambda s: s.sortie_id):
        if s.mission_id not in ctx.missions:
            errors.append(f"{s.sortie_id}.mission_id={s.mission_id} 不在册")
        if s.aircraft_id not in ctx.aircraft:
            errors.append(f"{s.sortie_id}.aircraft_id={s.aircraft_id} 不在册")
        if s.airspace_id not in ctx.airspaces:
            errors.append(f"{s.sortie_id}.airspace_id={s.airspace_id} 不在册")
        if s.runway_id not in ctx.runways:
            errors.append(f"{s.sortie_id}.runway_id={s.runway_id} 不在册")
        for c in s.crew:
            if c.person_id not in ctx.persons:
                errors.append(f"{s.sortie_id}.crew.person_id={c.person_id} 不在册")
            elif ctx.persons[c.person_id].name != c.name:
                errors.append(
                    f"{s.sortie_id}.crew[{c.person_id}].name={c.name} "
                    f"与人员表的 {ctx.persons[c.person_id].name} 不一致"
                )
        mission = ctx.missions.get(s.mission_id)
        if mission is not None and mission.name != s.mission_name:
            errors.append(
                f"{s.sortie_id}.mission_name={s.mission_name} 与课目表的 {mission.name} 不一致"
            )
    for item in plan.blocked_items:
        if item.person_id not in ctx.persons:
            errors.append(f"blocked_items.person_id={item.person_id} 不在册")
        if item.mission_id not in ctx.missions:
            errors.append(f"blocked_items.mission_id={item.mission_id} 不在册")
    for debt in plan.debts:
        if debt.person_id not in ctx.persons:
            errors.append(f"debts.person_id={debt.person_id} 不在册")
        if debt.mission_id not in ctx.missions:
            errors.append(f"debts.mission_id={debt.mission_id} 不在册")
    return errors


# ─────────────────────────────────────────────────────────────────────
# ② 业务完整性层 —— 三表交叉一致性
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SortieRow:
    """一个架次在某一张表里的投影行。"""

    sortie_id: str
    date: date_type
    weekday: str
    takeoff: time
    landing: time
    mission_id: str
    mission_name: str
    aircraft_id: str
    crew: tuple[tuple[str, str], ...]  # (person_id, role)，按 person_id 排序

    def shared(self) -> tuple[object, ...]:
        """三张表都必须一致的字段。"""
        return (
            self.date,
            self.weekday,
            self.takeoff,
            self.landing,
            self.mission_id,
            self.mission_name,
            self.aircraft_id,
            self.crew,
        )


def row_of(sortie: Sortie) -> SortieRow:
    return SortieRow(
        sortie_id=sortie.sortie_id,
        date=sortie.date,
        weekday=sortie.weekday,
        takeoff=sortie.takeoff,
        landing=sortie.landing,
        mission_id=sortie.mission_id,
        mission_name=sortie.mission_name,
        aircraft_id=sortie.aircraft_id,
        crew=tuple(sorted((c.person_id, c.role) for c in sortie.crew)),
    )


@dataclass(frozen=True)
class ThreeTableProjection:
    """Sheet 1/2/3 —— 同一份数据的三种投影（v6 §10.1~§10.3）。"""

    by_day: Mapping[date_type, tuple[SortieRow, ...]] = field(default_factory=dict)
    by_person: Mapping[str, tuple[SortieRow, ...]] = field(default_factory=dict)
    by_aircraft: Mapping[str, tuple[SortieRow, ...]] = field(default_factory=dict)


def project_plan(plan: SchedulePlan) -> ThreeTableProjection:
    """`SchedulePlan` → 三张表的投影（组内按起飞时刻升序）。"""
    rows = [row_of(s) for s in plan.sorties]
    by_day: dict[date_type, list[SortieRow]] = {}
    by_person: dict[str, list[SortieRow]] = {}
    by_aircraft: dict[str, list[SortieRow]] = {}
    for r in rows:
        by_day.setdefault(r.date, []).append(r)
        by_aircraft.setdefault(r.aircraft_id, []).append(r)
        for person_id, _ in r.crew:
            by_person.setdefault(person_id, []).append(r)

    def _sorted(group: dict[Any, list[SortieRow]]) -> dict[Any, tuple[SortieRow, ...]]:
        return {
            k: tuple(sorted(v, key=lambda r: (r.date, r.takeoff, r.sortie_id)))
            for k, v in sorted(group.items(), key=lambda kv: str(kv[0]))
        }

    return ThreeTableProjection(
        by_day=_sorted(by_day),
        by_person=_sorted(by_person),
        by_aircraft=_sorted(by_aircraft),
    )


def check_cross_table_consistency(proj: ThreeTableProjection) -> list[str]:
    """同一 sortie 在分日表 / 人员表 / 飞机表中必须完全一致。

    比对四件事：① 三张表覆盖同一批架次号；② 共有字段逐字段相等；
    ③ 分组键与行内容自洽（人员表分到谁名下、飞机表分到哪架机）；
    ④ 人员表里该架次的出现次数 == 机组人数。
    """
    errors: list[str] = []
    day_rows = {r.sortie_id: r for rows in proj.by_day.values() for r in rows}
    ac_rows = {r.sortie_id: r for rows in proj.by_aircraft.values() for r in rows}
    person_rows: dict[str, list[tuple[str, SortieRow]]] = {}
    for person_id, rows in proj.by_person.items():
        for r in rows:
            person_rows.setdefault(r.sortie_id, []).append((person_id, r))

    ids_day, ids_ac, ids_person = set(day_rows), set(ac_rows), set(person_rows)
    for sid in sorted(ids_day - ids_ac):
        errors.append(f"{sid} 出现在分日表但不在飞机表")
    for sid in sorted(ids_ac - ids_day):
        errors.append(f"{sid} 出现在飞机表但不在分日表")
    for sid in sorted(ids_day - ids_person):
        errors.append(f"{sid} 出现在分日表但不在人员表")
    for sid in sorted(ids_person - ids_day):
        errors.append(f"{sid} 出现在人员表但不在分日表")

    # 分组键自洽
    for day, rows in proj.by_day.items():
        for r in rows:
            if r.date != day:
                errors.append(f"{r.sortie_id} 被分在分日表 {day} 组，但行内日期为 {r.date}")
    for ac_id, rows in proj.by_aircraft.items():
        for r in rows:
            if r.aircraft_id != ac_id:
                errors.append(
                    f"{r.sortie_id} 被分在飞机表 {ac_id} 组，但行内机号为 {r.aircraft_id}"
                )
    for person_id, rows in proj.by_person.items():
        for r in rows:
            if person_id not in {pid for pid, _ in r.crew}:
                errors.append(
                    f"{r.sortie_id} 被分在人员表 {person_id} 名下，但其机组为 {list(r.crew)}"
                )

    # 共有字段逐字段相等
    for sid in sorted(ids_day & ids_ac):
        if day_rows[sid].shared() != ac_rows[sid].shared():
            errors.append(
                f"{sid} 在分日表与飞机表中的字段不一致：{_first_diff(day_rows[sid], ac_rows[sid])}"
            )
    for sid in sorted(ids_day & ids_person):
        for person_id, r in person_rows[sid]:
            if day_rows[sid].shared() != r.shared():
                errors.append(
                    f"{sid} 在分日表与人员表（{person_id}）中的字段不一致："
                    f"{_first_diff(day_rows[sid], r)}"
                )
        if len(person_rows[sid]) != len(day_rows[sid].crew):
            errors.append(
                f"{sid} 在人员表中出现 {len(person_rows[sid])} 次，"
                f"与机组人数 {len(day_rows[sid].crew)} 不符"
            )
    return errors


_SHARED_FIELDS = (
    "date",
    "weekday",
    "takeoff",
    "landing",
    "mission_id",
    "mission_name",
    "aircraft_id",
    "crew",
)


def _first_diff(a: SortieRow, b: SortieRow) -> str:
    diffs = [
        f"{name}={getattr(a, name)!r} vs {getattr(b, name)!r}"
        for name in _SHARED_FIELDS
        if getattr(a, name) != getattr(b, name)
    ]
    return "；".join(diffs) or "（无字段差异）"


# ─────────────────────────────────────────────────────────────────────
# 汇总
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FormatCheckReport:
    """闸门2 的完整结果（第 3 层在 `validator/workbook.py`）。"""

    schema_errors: tuple[str, ...] = ()
    integrity_errors: tuple[str, ...] = ()
    cross_table_errors: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not (self.schema_errors or self.integrity_errors or self.cross_table_errors)

    def all_errors(self) -> tuple[str, ...]:
        return self.schema_errors + self.integrity_errors + self.cross_table_errors


def verify_format(
    payload: Mapping[str, Any] | SchedulePlan,
    ctx: ValidationContext,
    *,
    projection: ThreeTableProjection | None = None,
) -> FormatCheckReport:
    """跑完格式校验前两层。

    `projection` 传入时用它做三表一致性比对（回读 xlsx 的场景），缺省则由
    `plan` 自己投影 —— 后者验证的是「投影逻辑自洽」，前者验证的是「产物与源一致」。
    """
    if isinstance(payload, SchedulePlan):
        plan: SchedulePlan | None = payload
        schema_errors: list[str] = []
    else:
        plan, schema_errors = validate_plan_schema(payload)
    if plan is None:
        return FormatCheckReport(schema_errors=tuple(schema_errors))
    return FormatCheckReport(
        schema_errors=tuple(schema_errors),
        integrity_errors=tuple(check_referential_integrity(plan, ctx)),
        cross_table_errors=tuple(check_cross_table_consistency(projection or project_plan(plan))),
    )


def sorties_of(proj: ThreeTableProjection) -> Sequence[SortieRow]:
    """分日表里的全部行（确定序），供回读比对使用。"""
    return sorted(
        (r for rows in proj.by_day.values() for r in rows),
        key=lambda r: (r.date, r.takeoff, r.sortie_id),
    )


__all__ = [
    "WEEKDAY_ORDER",
    "FormatCheckReport",
    "SortieRow",
    "ThreeTableProjection",
    "check_cross_table_consistency",
    "check_referential_integrity",
    "project_plan",
    "row_of",
    "sorties_of",
    "validate_plan_schema",
    "verify_format",
]
