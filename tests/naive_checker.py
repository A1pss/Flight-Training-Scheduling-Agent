"""第三方独立校验器（v6 §12.3 度量方式第 2 条）—— pandas 暴力实现。

## 这份代码存在的唯一理由

v6 §12.3 要求「生成计划准确率 = 100%」由**三重独立验证**度量：

1. 主校验器 `backend/validator/checks.py`（M2-B 交付）
2. **本文件**：用 pandas 写一版 naive checker，O(n²) 暴力实现，只求正确不求性能
3. 人工抽检

第 2 条的价值全在「独立」两个字上。所以本文件的编写口径是**硬性**的：

- **依据只有三样**：`docs/…v6.md` §3.2 的 14 条规格表（含 §3.3 / §3.4 / §3.5 /
  §3.1.1 的展开）、`rules/ruleset_v1.3.yaml` + `rules/semantics.yaml`、
  以及 `data/origin/rules.pdf` 的 14 条原文。
- **`backend/validator/` 下的 `checks.py` / `schema.py` / `workbook.py` 自始至终
  没有打开过**（见 `tests/unit/test_naive_checker_independence.py` 的护栏，以及
  `reports/M2C_收工报告.md` §2 的诚实交代）。本文件只 import
  `backend.validator.context`，那是**事实视图**（ORM 行 → dataclass），不含任何
  一条规则的判定逻辑 —— 与 `backend.core.ruleset`（YAML → 对象）同一性质。
- **不 import `backend.solver`**，也不 import 主校验器的任何判定函数。

## 与主校验器的三处口径差异（**刻意的**，不是漏实现）

1. **本文件只判 HARD。** 主校验器会把「已松弛且已在 `plan.debts` 里披露」的缺口
   降级为 SOFT（v6 §3.10：松弛只发生在 R1/R2 且欠账 100% 显式披露）。对拍时
   SOFT 不参与比对（M2-B 收工报告 §8「SOFT 不算分歧」），所以这里干脆不实现
   降级，判出来的全是 HARD。**Tier 0 下两者等价**，而 200 场景全是 Tier 0。
2. **不产出 `checked_items`。** 那是主校验器给前端看的计数（v6 §4.2 脚注），
   与判定无关。
3. **不做格式三层校验**（闸门2/3）。本文件只重算闸门1 的 14 条。

## 复杂度

一律用最笨的写法：两两配对、逐窗口枚举、逐分钟前缀和。**任何一处「优化」都是在
往主校验器的实现形态上靠，那正是本文件要避免的**。基准周 14 架次，O(n²) 完全够用。
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time

import pandas as pd

from backend.core.ruleset import (
    IDENTITY_INSTRUCTOR,
    IDENTITY_STUDENT,
)
from backend.schemas.plan import SchedulePlan, Sortie
from backend.validator.context import ValidationContext

#: 星期名（附录 B `Weekday`），按 `date.weekday()` 下标取
WEEKDAY_NAMES: tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 14 条规则的编号与标题（v6 §3.2）
RULE_TITLES: dict[str, str] = {
    "C01": "时间一致性",
    "C02": "人员可用性",
    "C03": "角色配置",
    "C04": "资质匹配与岗位互斥",
    "C05": "机型与机组编成",
    "C06": "资源有效性与容量",
    "C07": "飞机排期冲突与周转时间",
    "C08": "人员冲突与休息",
    "C09": "起降密度限制",
    "C10": "每日飞行时长上限",
    "C11": "每周架次上限",
    "C12": "每日架次上限",
    "C13": "任务完成度",
    "C14": "任务唯一性",
}

#: 单人架次的合法角色（rules.pdf 约束5「单飞/复训架次机组人数为 1」）
SOLO_ROLES: frozenset[str] = frozenset({"单飞", "复训"})


# ─────────────────────────────────────────────────────────────────────
# 结果对象
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class NaiveViolation:
    """一条违规。字段刻意与 `backend.schemas.validation.Violation` 同名同义，
    好让对拍时可以逐字段比。"""

    rule_id: str
    subjects: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class NaiveReport:
    """14 条的暴力校验结果。"""

    violations: tuple[NaiveViolation, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.violations

    def violated_rules(self) -> frozenset[str]:
        return frozenset(v.rule_id for v in self.violations)

    def by_rule(self, rule_id: str) -> tuple[NaiveViolation, ...]:
        return tuple(v for v in self.violations if v.rule_id == rule_id)

    def summary(self) -> str:
        if self.passed:
            return "naive: 14 条全过"
        counts = {rid: len(self.by_rule(rid)) for rid in sorted(self.violated_rules())}
        return "naive: " + "，".join(f"{rid}×{n}" for rid, n in counts.items())


class _Acc:
    """违规累加器 —— 只是为了让 14 个 `check_*` 函数写起来短一点。"""

    def __init__(self) -> None:
        self.items: list[NaiveViolation] = []

    def add(self, rule_id: str, subjects: Iterable[str], detail: str) -> None:
        self.items.append(NaiveViolation(rule_id=rule_id, subjects=tuple(subjects), detail=detail))


# ─────────────────────────────────────────────────────────────────────
# 方案 → DataFrame（本文件唯一的一次「预处理」）
# ─────────────────────────────────────────────────────────────────────
def _minutes(clock: time) -> int:
    return clock.hour * 60 + clock.minute


def _trainee_of(sortie: Sortie) -> str:
    """受训人 = 机组里角色不是「教员」的那个人（带飞取学员，单人取本人）。"""
    for member in sortie.crew:
        if member.role != "教员":
            return member.person_id
    return sortie.crew[0].person_id


def sortie_frame(plan: SchedulePlan, ctx: ValidationContext) -> pd.DataFrame:
    """一行一架次。派生列只做「换算」，不做任何判定。"""
    rows: list[dict[str, object]] = []
    for s in plan.sorties:
        mission = ctx.missions.get(s.mission_id)
        aircraft = ctx.aircraft.get(s.aircraft_id)
        rows.append(
            {
                "sortie_id": s.sortie_id,
                "date": s.date,
                "weekday": s.weekday,
                "day": (s.date - ctx.week_start).days,
                "takeoff": _minutes(s.takeoff),
                "landing": _minutes(s.landing),
                "mission_id": s.mission_id,
                "mission_class": mission.mission_class if mission else None,
                "duration": mission.duration_minutes if mission else None,
                "airspace_id": s.airspace_id,
                "aircraft_id": s.aircraft_id,
                "aircraft_type": aircraft.aircraft_type if aircraft else None,
                "runway_id": s.runway_id,
                "is_recurrent": s.is_recurrent,
                "trainee_id": _trainee_of(s),
                "crew_size": len(s.crew),
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "sortie_id",
            "date",
            "weekday",
            "day",
            "takeoff",
            "landing",
            "mission_id",
            "mission_class",
            "duration",
            "airspace_id",
            "aircraft_id",
            "aircraft_type",
            "runway_id",
            "is_recurrent",
            "trainee_id",
            "crew_size",
        ],
    )


def crew_frame(plan: SchedulePlan, ctx: ValidationContext) -> pd.DataFrame:
    """一行一个 (架次, 人员, 角色)。"""
    rows: list[dict[str, object]] = []
    for s in plan.sorties:
        mission = ctx.missions.get(s.mission_id)
        for member in s.crew:
            person = ctx.persons.get(member.person_id)
            rows.append(
                {
                    "sortie_id": s.sortie_id,
                    "date": s.date,
                    "day": (s.date - ctx.week_start).days,
                    "takeoff": _minutes(s.takeoff),
                    "landing": _minutes(s.landing),
                    "person_id": member.person_id,
                    "name": member.name,
                    "role": member.role,
                    "identity": person.identity if person else None,
                    "mission_id": s.mission_id,
                    "mission_class": mission.mission_class if mission else None,
                    "duration": mission.duration_minutes if mission else None,
                    "aircraft_id": s.aircraft_id,
                    "is_recurrent": s.is_recurrent,
                }
            )
    return pd.DataFrame(
        rows,
        columns=[
            "sortie_id",
            "date",
            "day",
            "takeoff",
            "landing",
            "person_id",
            "name",
            "role",
            "identity",
            "mission_id",
            "mission_class",
            "duration",
            "aircraft_id",
            "is_recurrent",
        ],
    )


# ─────────────────────────────────────────────────────────────────────
# 约束1 时间一致性（rules.pdf 约束1）
# ─────────────────────────────────────────────────────────────────────
def check_c01(sf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> None:
    win_lo = _minutes(ctx.ruleset.window_start)
    win_hi = _minutes(ctx.ruleset.window_end)
    for row in sf.itertuples(index=False):
        sid = str(row.sortie_id)
        # ⚠️ 课目/飞机一律从 `ctx` 查，**不读 DataFrame 的派生列** —— 列里混进
        # `None` 时 pandas 会转成 `NaN`，而 `NaN is not None` 为真、任何比较又都
        # 为假，静默产出假违规。这个坑在 M2-C 的注入用例上真的踩到过一次。
        mission = ctx.missions.get(str(row.mission_id))
        if mission is not None and row.landing - row.takeoff != mission.duration_minutes:
            acc.add(
                "C01",
                [sid, str(row.mission_id)],
                f"{sid} 时长 {row.landing - row.takeoff} 分钟 ≠ "
                f"{row.mission_id} 标准时长 {mission.duration_minutes} 分钟",
            )
        if not ctx.ruleset.cross_day_allowed and row.landing <= row.takeoff:
            acc.add("C01", [sid], f"{sid} 着陆不晚于起飞（禁止跨日）")
        if row.takeoff < win_lo or row.landing > win_hi:
            acc.add(
                "C01",
                [sid],
                f"{sid} 超出训练窗 {ctx.ruleset.window_start:%H:%M}-{ctx.ruleset.window_end:%H:%M}",
            )
        expected_weekday = WEEKDAY_NAMES[row.date.weekday()]
        if row.weekday != expected_weekday:
            acc.add(
                "C01",
                [sid],
                f"{sid} 星期列 {row.weekday} 与日期 {row.date} 的 {expected_weekday} 不符",
            )


# ─────────────────────────────────────────────────────────────────────
# 约束2 人员可用性（rules.pdf 约束2 + S-11）
# ─────────────────────────────────────────────────────────────────────
def check_c02(cf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> tuple[str, ...]:
    notes: list[str] = []
    s11_on = ctx.semantics.s11_enabled
    if s11_on:
        notes.append(
            "S-11：成熟飞行员到期资质转复训（自到期次日起按 7 天滑窗强制安排），"
            "系对 rules.pdf 约束2 字面语义的**业务方授权改写**，非校验器漏判。"
        )
    for row in cf.itertuples(index=False):
        sid = str(row.sortie_id)
        person = ctx.persons.get(str(row.person_id))
        if person is None:
            acc.add("C02", [sid, str(row.person_id)], f"{row.person_id} 不在人员表中")
            continue
        if row.date in person.unavailable_dates:
            acc.add(
                "C02",
                [sid, person.person_id],
                f"{person.name}({person.person_id}) 在 {row.date} 不可用，仍被排在 {sid}",
            )
        mission = ctx.missions.get(str(row.mission_id))
        if mission is None:
            continue
        qual = person.qualification_of(mission.mission_class)
        if qual is None or qual.expiry_date is None:
            continue
        expired = (
            row.date > qual.expiry_date
            if ctx.ruleset.expiry_inclusive
            else row.date >= qual.expiry_date
        )
        if not expired:
            continue
        if s11_on and person.identity in ctx.semantics.s11_identities:
            # S-11：不算违规，但必须真的按复训安排
            if not row.is_recurrent or row.role != "复训":
                acc.add(
                    "C02",
                    [sid, person.person_id],
                    f"{person.name}({person.person_id}) {mission.mission_class} 类资质已于 "
                    f"{qual.expiry_date} 到期，{sid} 未按 S-11 标记为复训架次",
                )
            else:
                notes.append(
                    f"S-11 生效实例：{person.name}({person.person_id}) {mission.mission_class} 类于 "
                    f"{qual.expiry_date} 到期，{sid}({row.date}) 按复训安排。"
                )
        else:
            acc.add(
                "C02",
                [sid, person.person_id],
                f"{person.name}({person.person_id}) {mission.mission_class} 类资质已于 "
                f"{qual.expiry_date} 到期，{sid}({row.date}) 仍安排该类课目",
            )
    return tuple(notes)


# ─────────────────────────────────────────────────────────────────────
# 约束3 角色配置（rules.pdf 约束3 + §3.1.1 + S-02/S-13）
# ─────────────────────────────────────────────────────────────────────
def _expected_crew_size(ctx: ValidationContext, mission_id: str, trainee_id: str) -> int | None:
    """§3.1.1 判定式：`需带飞 = (mission.带飞 == 是) ∧ (person.身份 == 学员)`。"""
    mission = ctx.missions.get(mission_id)
    person = ctx.persons.get(trainee_id)
    if mission is None or person is None:
        return None
    return 2 if (mission.dual_required and person.identity == IDENTITY_STUDENT) else 1


def check_c03(plan: SchedulePlan, sf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> None:
    for sortie in plan.sorties:
        sid = sortie.sortie_id
        roles = [m.role for m in sortie.crew]
        if len(sortie.crew) == 2:
            if sorted(roles) != sorted(["教员", "学员"]):
                acc.add("C03", [sid], f"{sid} 双人架次角色应为 1 教员 + 1 学员，实际 {roles}")
        elif len(sortie.crew) == 1:
            if roles[0] not in SOLO_ROLES:
                acc.add("C03", [sid], f"{sid} 单人架次角色应为 单飞/复训，实际 {roles[0]}")
        else:
            acc.add("C03", [sid], f"{sid} 机组人数 {len(sortie.crew)} 不合法")
        # 岗位与身份自洽：教员岗只能由教员身份担任（rules.pdf 约束3「1 名教员」）
        for member in sortie.crew:
            person = ctx.persons.get(member.person_id)
            if person is None:
                continue
            if member.role == "教员" and person.identity != IDENTITY_INSTRUCTOR:
                acc.add(
                    "C03",
                    [sid, member.person_id],
                    f"{sid} 的教员岗由 {person.name}({person.person_id}) 担任，"
                    f"其身份为「{person.identity}」，不是教员",
                )
            if member.role == "学员" and person.identity != IDENTITY_STUDENT:
                acc.add(
                    "C03",
                    [sid, member.person_id],
                    f"{sid} 的学员岗由 {person.name}({person.person_id}) 担任，"
                    f"其身份为「{person.identity}」",
                )
            if member.role == "复训" and person.identity not in ctx.semantics.s11_identities:
                acc.add(
                    "C03",
                    [sid, member.person_id],
                    f"{sid} 标为复训架次，但 {person.name}({person.person_id}) "
                    f"身份「{person.identity}」不适用 S-11",
                )
        # 编成人数按判定式独立重算
        expected = _expected_crew_size(ctx, sortie.mission_id, _trainee_of(sortie))
        if expected is not None and len(sortie.crew) != expected:
            acc.add(
                "C03",
                [sid, _trainee_of(sortie)],
                f"{sid}（{sortie.mission_id} 带飞="
                f"{'是' if ctx.missions[sortie.mission_id].dual_required else '否'}）"
                f"按 §3.1.1 应为 {expected} 人机组，实际 {len(sortie.crew)} 人",
            )

    # S-02 + S-13：每周必飞类别，A 类整体合并计数，对全部学员生效
    weekly_classes = set(ctx.weekly_required_classes())
    if not weekly_classes:
        return
    minimum = ctx.ruleset.weekly_class_min
    for person in ctx.students():
        if sf.empty:
            count = 0
        else:
            hit = sf[
                (sf["trainee_id"] == person.person_id) & (sf["mission_class"].isin(weekly_classes))
            ]
            count = len(hit)
        if count < minimum:
            acc.add(
                "C03",
                [person.person_id],
                f"{person.name}({person.person_id}) 本周 "
                f"{'/'.join(sorted(weekly_classes))} 类架次 {count} 次 < 每周必飞 {minimum} 次",
            )


# ─────────────────────────────────────────────────────────────────────
# 约束4 资质匹配与岗位互斥（rules.pdf 约束4 + S-09）
# ─────────────────────────────────────────────────────────────────────
def check_c04(cf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> None:
    for row in cf.itertuples(index=False):
        sid = str(row.sortie_id)
        person = ctx.persons.get(str(row.person_id))
        mission = ctx.missions.get(str(row.mission_id))
        if person is None or mission is None:
            continue
        if person.qualification_of(mission.mission_class) is None:
            acc.add(
                "C04",
                [sid, person.person_id],
                f"{person.name}({person.person_id}) 不持 {mission.mission_class} 类资质，"
                f"却被排在 {sid}（{row.mission_id}）",
            )
        if row.role == "教员" and person.identity != IDENTITY_INSTRUCTOR:
            acc.add(
                "C04",
                [sid, person.person_id],
                f"岗位互斥：{person.name}({person.person_id}) 身份为「{person.identity}」，"
                f"占据了 {sid} 的教员岗",
            )
        if row.role != "教员" and person.identity == IDENTITY_INSTRUCTOR:
            acc.add(
                "C04",
                [sid, person.person_id],
                f"S-09：教员 {person.name}({person.person_id}) 不得作为受训人出现（{sid}）",
            )
    # 同一人同一时刻只能在一个架次上 —— 两两配对，半开区间 [takeoff, landing)
    for person_id, group in _group_rows(cf, ["person_id"]):
        rows = list(group)
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if a["date"] != b["date"]:
                    continue
                if a["takeoff"] < b["landing"] and b["takeoff"] < a["landing"]:
                    acc.add(
                        "C04",
                        [*sorted([str(a["sortie_id"]), str(b["sortie_id"])]), str(person_id[0])],
                        f"{person_id[0]} 在 {a['date']} 的 {a['sortie_id']} 与 "
                        f"{b['sortie_id']} 时间重叠",
                    )


# ─────────────────────────────────────────────────────────────────────
# 约束5 机型与机组编成（rules.pdf 约束5）
# ─────────────────────────────────────────────────────────────────────
def check_c05(plan: SchedulePlan, ctx: ValidationContext, acc: _Acc) -> None:
    for sortie in plan.sorties:
        sid = sortie.sortie_id
        aircraft = ctx.aircraft.get(sortie.aircraft_id)
        if aircraft is not None:
            for member in sortie.crew:
                person = ctx.persons.get(member.person_id)
                if person is None:
                    continue
                if aircraft.aircraft_type not in person.aircraft_types:
                    acc.add(
                        "C05",
                        [sid, member.person_id],
                        f"{person.name}({person.person_id}) 不持 {aircraft.aircraft_type} "
                        f"机型资质，却被排在 {sid}（{sortie.aircraft_id}）",
                    )
            if len(sortie.crew) > aircraft.seats:
                acc.add(
                    "C05",
                    [sid, sortie.aircraft_id],
                    f"{sid} 机组 {len(sortie.crew)} 人 > {sortie.aircraft_id} 座位数 "
                    f"{aircraft.seats}",
                )
        expected = _expected_crew_size(ctx, sortie.mission_id, _trainee_of(sortie))
        if expected is not None and len(sortie.crew) != expected:
            acc.add(
                "C05",
                [sid, _trainee_of(sortie)],
                f"{sid} 机组编成应为 {expected} 人（{sortie.mission_id}），"
                f"实际 {len(sortie.crew)} 人",
            )


# ─────────────────────────────────────────────────────────────────────
# 约束6 资源有效性与容量（rules.pdf 约束6 + S-10 + §3.4）
# ─────────────────────────────────────────────────────────────────────
def check_c06(sf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> None:
    for row in sf.itertuples(index=False):
        sid = str(row.sortie_id)
        aircraft = ctx.aircraft.get(str(row.aircraft_id))
        mission = ctx.missions.get(str(row.mission_id))
        if aircraft is None:
            acc.add("C06", [sid, str(row.aircraft_id)], f"{row.aircraft_id} 不在在册机号列表中")
            continue
        if mission is None:
            acc.add("C06", [sid, str(row.mission_id)], f"{row.mission_id} 不在课目表中")
            continue
        if aircraft.aircraft_type not in mission.aircraft_types:
            acc.add(
                "C06",
                [sid, aircraft.aircraft_id],
                f"{aircraft.aircraft_id}（{aircraft.aircraft_type}）机型不适配 "
                f"{mission.mission_id}（要求 {'/'.join(sorted(mission.aircraft_types))}）",
            )
        if mission.mission_id not in aircraft.capable_missions:
            acc.add(
                "C06",
                [sid, aircraft.aircraft_id],
                f"{aircraft.aircraft_id} 的适配课目列表不含 {mission.mission_id}",
            )
        if row.airspace_id != mission.airspace_id:
            acc.add(
                "C06",
                [sid, str(row.airspace_id)],
                f"{sid} 空域 {row.airspace_id} ≠ {mission.mission_id} 绑定空域 "
                f"{mission.airspace_id}",
            )
        if str(row.airspace_id) not in ctx.airspaces:
            acc.add("C06", [sid, str(row.airspace_id)], f"{row.airspace_id} 不在空域表中")

    # 空域同时段容量：逐分钟数并发（最笨的写法）。着陆当刻不再占用（半开区间）
    for key, group in _group_rows(sf, ["airspace_id", "date"]):
        airspace_id, day = str(key[0]), key[1]
        space = ctx.airspaces.get(airspace_id)
        if space is None:
            continue
        rows = list(group)
        lo = min(int(r["takeoff"]) for r in rows)
        hi = max(int(r["landing"]) for r in rows)
        for minute in range(lo, hi + 1):
            concurrent = [
                str(r["sortie_id"]) for r in rows if int(r["takeoff"]) <= minute < int(r["landing"])
            ]
            if len(concurrent) > space.capacity:
                acc.add(
                    "C06",
                    [airspace_id, str(day), *sorted(concurrent)],
                    f"空域 {airspace_id} 于 {day} {minute // 60:02d}:{minute % 60:02d} "
                    f"并发 {len(concurrent)} 架次 > 容量 {space.capacity}"
                    f"（{'、'.join(sorted(concurrent))}）",
                )
                break  # 同一空域同一天只报一次，避免逐分钟刷屏


# ─────────────────────────────────────────────────────────────────────
# 约束7 飞机排期冲突与周转时间（rules.pdf 约束7 + S-06）
# ─────────────────────────────────────────────────────────────────────
def check_c07(sf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> None:
    for key, group in _group_rows(sf, ["aircraft_id"]):
        aircraft_id = str(key[0])
        aircraft = ctx.aircraft.get(aircraft_id)
        if aircraft is None:
            continue
        rows = list(group)
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                first, second = rows[i], rows[j]
                if _abs_minutes(second) < _abs_minutes(first):
                    first, second = second, first
                gap = _abs_minutes(second) - (
                    _abs_minutes(first) + int(first["landing"]) - int(first["takeoff"])
                )
                if gap < aircraft.turnaround_minutes:
                    acc.add(
                        "C07",
                        [
                            *sorted([str(first["sortie_id"]), str(second["sortie_id"])]),
                            aircraft_id,
                        ],
                        f"{aircraft_id}（{aircraft.aircraft_type}）{first['sortie_id']} 着陆 → "
                        f"{second['sortie_id']} 起飞间隔 {gap} 分钟 < 周转 "
                        f"{aircraft.turnaround_minutes} 分钟",
                    )
        for row in rows:
            for window in aircraft.maintenance:
                start = datetime.combine(
                    row["date"], time(int(row["takeoff"]) // 60, int(row["takeoff"]) % 60)
                )
                end = datetime.combine(
                    row["date"], time(int(row["landing"]) // 60, int(row["landing"]) % 60)
                )
                if start < window.end and window.start < end:
                    acc.add(
                        "C07",
                        [str(row["sortie_id"]), aircraft_id],
                        f"{aircraft_id} 的 {row['sortie_id']} 落在维护时段 "
                        f"{window.start:%Y-%m-%d %H:%M} ~ {window.end:%Y-%m-%d %H:%M} 内",
                    )


# ─────────────────────────────────────────────────────────────────────
# 约束8 人员冲突与休息（rules.pdf 约束8 + S-07）
# ─────────────────────────────────────────────────────────────────────
def check_c08(cf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> None:
    min_gap = ctx.ruleset.min_gap_minutes
    rest_after = ctx.ruleset.rest_after_n
    rest_min = ctx.ruleset.rest_minutes
    for key, group in _group_rows(cf, ["person_id", "date"]):
        person_id, day = str(key[0]), key[1]
        rows = sorted(group, key=lambda r: (int(r["takeoff"]), str(r["sortie_id"])))
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                gap = int(b["takeoff"]) - int(a["landing"])
                if gap < min_gap:
                    acc.add(
                        "C08",
                        [*sorted([str(a["sortie_id"]), str(b["sortie_id"])]), person_id],
                        f"{person_id} 在 {day} 的 {a['sortie_id']} 与 {b['sortie_id']} "
                        f"间隔 {gap} 分钟 < {min_gap} 分钟",
                    )
        for idx in range(rest_after, len(rows)):
            prev, cur = rows[idx - 1], rows[idx]
            gap = int(cur["takeoff"]) - int(prev["landing"])
            if gap < rest_min:
                acc.add(
                    "C08",
                    [str(prev["sortie_id"]), str(cur["sortie_id"]), person_id],
                    f"{person_id} 在 {day} 的第 {idx + 1} 架次 {cur['sortie_id']} 前休息 "
                    f"{gap} 分钟 < 连续 {rest_after} 架次后应休息的 {rest_min} 分钟",
                )


# ─────────────────────────────────────────────────────────────────────
# 约束9 起降密度（rules.pdf 约束9 + S-04/S-05/D-2）
# ─────────────────────────────────────────────────────────────────────
def check_c09(sf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> None:
    for row in sf.itertuples(index=False):
        sid = str(row.sortie_id)
        runway = ctx.runways.get(str(row.runway_id))
        if runway is None:
            acc.add("C09", [sid, str(row.runway_id)], f"{row.runway_id} 不在跑道表中")
            continue
        aircraft = ctx.aircraft.get(str(row.aircraft_id))
        if aircraft is not None and aircraft.aircraft_type not in runway.aircraft_types:
            acc.add(
                "C09",
                [sid, runway.runway_id],
                f"{sid} 的 {row.aircraft_id}（{aircraft.aircraft_type}）不能使用 "
                f"{runway.runway_id}（服务机型 {'/'.join(sorted(runway.aircraft_types))}）",
            )

    # ① 20 分钟滑动窗口 —— **按 (日, 跑道) 分组**（D-2 前半句）
    window = ctx.ruleset.density_window_minutes
    cap = ctx.ruleset.density_window_cap
    for key, group in _group_rows(sf, ["date", "runway_id"]):
        day, runway_id = key[0], str(key[1])
        rows = list(group)
        for anchor in rows:
            start = int(anchor["takeoff"])
            inside = [
                str(r["sortie_id"]) for r in rows if start <= int(r["takeoff"]) < start + window
            ]
            if len(inside) > cap:
                acc.add(
                    "C09",
                    [str(day), runway_id, *sorted(inside)],
                    f"{day} {runway_id} 在 [{start // 60:02d}:{start % 60:02d}, "
                    f"+{window}分钟) 内起飞 {len(inside)} 次 > {cap} 次"
                    f"（{'、'.join(sorted(inside))}）",
                )

    # ② 7 分钟间隔 —— **按日全场统一，不分跑道**（D-2 后半句）
    separation = ctx.ruleset.separation_minutes
    for key, group in _group_rows(sf, ["date"]):
        day = key[0]
        rows = list(group)
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                delta = abs(int(a["takeoff"]) - int(b["takeoff"]))
                if delta < separation:
                    acc.add(
                        "C09",
                        [str(day), *sorted([str(a["sortie_id"]), str(b["sortie_id"])])],
                        f"{day} 起飞间隔 {delta} 分钟 < {separation} 分钟（**全场口径**，"
                        f"{a['sortie_id']}@{a['runway_id']} 与 "
                        f"{b['sortie_id']}@{b['runway_id']}）",
                    )


# ─────────────────────────────────────────────────────────────────────
# 约束10~12 线性上限（rules.pdf 约束10/11/12）
# ─────────────────────────────────────────────────────────────────────
def check_c10(cf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> None:
    for key, group in _group_rows(cf, ["person_id", "date"]):
        person_id, day = str(key[0]), key[1]
        person = ctx.persons.get(person_id)
        if person is None:
            continue
        total = sum(int(r["landing"]) - int(r["takeoff"]) for r in group)
        cap = ctx.ruleset.daily_minute_cap(person.identity)
        if total > cap:
            acc.add(
                "C10",
                [person_id, str(day)],
                f"{person.name}({person_id}) 在 {day} 累计飞行 {total} 分钟 > 上限 {cap} 分钟",
            )


def check_c11(cf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> None:
    for key, group in _group_rows(cf, ["person_id"]):
        person_id = str(key[0])
        person = ctx.persons.get(person_id)
        if person is None:
            continue
        count = len(list(group))
        cap = ctx.ruleset.weekly_sortie_cap(person.identity)
        if count > cap:
            acc.add(
                "C11",
                [person_id],
                f"{person.name}({person_id}) 本周 {count} 架次 > 上限 {cap} 架次",
            )


def check_c12(sf: pd.DataFrame, cf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> None:
    per_person = ctx.ruleset.daily_sorties_per_person
    per_aircraft = ctx.ruleset.daily_sorties_per_aircraft
    for key, group in _group_rows(cf, ["person_id", "date"]):
        count = len(list(group))
        if count > per_person:
            acc.add(
                "C12",
                [str(key[0]), str(key[1])],
                f"{key[0]} 在 {key[1]} 有 {count} 架次 > 单人单日上限 {per_person}",
            )
    for key, group in _group_rows(sf, ["aircraft_id", "date"]):
        count = len(list(group))
        if count > per_aircraft:
            acc.add(
                "C12",
                [str(key[0]), str(key[1])],
                f"{key[0]} 在 {key[1]} 有 {count} 架次 > 单机单日上限 {per_aircraft}",
            )


# ─────────────────────────────────────────────────────────────────────
# 约束13 任务完成度（rules.pdf 约束13 + §3.5 + S-01/S-03/S-11/S-12 + D-4）
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Requirement:
    """一条本周必须满足的频率要求（naive 侧自己推的）。

    `mission_ids` 通常只有一门课目；**S-11 复训是整个类别一条要求**（业务方
    2026-08-12 裁定，见 :func:`frequency_requirements`），那时它是该类的全部课目。
    """

    person_id: str
    mission_ids: tuple[str, ...]
    label: str
    freq_days: int
    origin_day: int
    deadline: int | None
    windows: tuple[tuple[int, int], ...] = ()


def _window_bounds(
    *, origin: int, freq: int, last_done: date | None, week_start: date
) -> tuple[int | None, tuple[tuple[int, int], ...]]:
    """把 (起算日, 窗口长度, 跨周锚点) 折算成 (截止日, 周内窗口列表)。"""
    if last_done is None:
        deadline: int | None = origin + freq - 1  # S-12
    else:
        gap = (week_start - last_done).days
        deadline = max(origin, freq - gap)  # D-4 通式
    if deadline is not None and deadline > 6:
        deadline = None  # 本周不构成约束（G/H 类 freq=14 落在这里）
    windows = tuple((start, start + freq - 1) for start in range(origin, 7 - freq + 1))
    return deadline, windows


def frequency_requirements(ctx: ValidationContext) -> tuple[_Requirement, ...]:
    """按 §3.5 独立重算「本周必须排」的要求集合。

    管辖对象 = 学员 ∪「复训中的成熟飞行员」（S-09 + S-11）。

    ## S-11 的粒度是**类别**（业务方 2026-08-12 裁定）

    M2-C 的属性测试跑出 FTS-3003：一个到期类别里有多门课目时，求解器按「该类
    ≥1 次」下要求，而 v6 §3.2 约束13 那行的字面读法是「每门课各自 7 天滑窗」。
    v6 自己是矛盾的（§12.3 的验收断言写的是「C-1 **或** C-2」），业务方裁定取
    **类别**粒度 —— 与 S-02（A 类整体 ≥1 次）同构。这里照裁定实现：`is_recurrent`
    的行按 (人, 类别) 合成**一条**要求，飞该类任意一门都算数。
    """
    out: list[_Requirement] = []
    recurrent_rows: dict[tuple[str, str], list[str]] = {}
    recurrent_meta: dict[tuple[str, str], tuple[date | None, date | None]] = {}

    for (person_id, mission_id), progress in sorted(ctx.progress.items()):
        person = ctx.persons.get(person_id)
        mission = ctx.missions.get(mission_id)
        if person is None or mission is None:
            continue
        recurrent = bool(progress.is_recurrent) and ctx.semantics.s11_enabled
        governed = person.identity == IDENTITY_STUDENT or (
            recurrent and person.identity in ctx.semantics.s11_identities
        )
        if not governed:
            continue
        if not progress.prereq_met:
            continue  # 先修未满足由 `check_c13` 的第一段处理（出现次数必须为 0）
        if recurrent:
            key = (person_id, mission.mission_class)
            recurrent_rows.setdefault(key, []).append(mission_id)
            since, anchor = recurrent_meta.get(key, (None, None))
            if progress.recurrent_since is not None:
                since = min(since or progress.recurrent_since, progress.recurrent_since)
            if progress.last_done_date is not None:
                anchor = max(anchor or progress.last_done_date, progress.last_done_date)
            recurrent_meta[key] = (since, anchor)
            continue
        if progress.completed:
            continue  # S-03：已完成课目不受约束13
        origin = 0
        deadline, windows = _window_bounds(
            origin=origin,
            freq=mission.freq_days,
            last_done=progress.last_done_date,
            week_start=ctx.week_start,
        )
        if deadline is None and not windows:
            continue
        out.append(
            _Requirement(
                person_id=person_id,
                mission_ids=(mission_id,),
                label=mission_id,
                freq_days=mission.freq_days,
                origin_day=origin,
                deadline=deadline,
                windows=windows,
            )
        )

    for (person_id, mission_class), mission_ids in sorted(recurrent_rows.items()):
        since, anchor = recurrent_meta.get((person_id, mission_class), (None, None))
        freq = ctx.semantics.s11_window_days
        origin = max(0, (since - ctx.week_start).days if since else 0)
        if origin > 6:
            continue  # 复训周期本周还没开始
        deadline, windows = _window_bounds(
            origin=origin, freq=freq, last_done=anchor, week_start=ctx.week_start
        )
        if deadline is None and not windows:
            continue
        out.append(
            _Requirement(
                person_id=person_id,
                mission_ids=tuple(sorted(mission_ids)),
                label=f"{mission_class} 类复训（{'、'.join(sorted(mission_ids))} 任一即可）",
                freq_days=freq,
                origin_day=origin,
                deadline=deadline,
                windows=windows,
            )
        )
    return tuple(out)


def check_c13(plan: SchedulePlan, sf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> None:
    counts: dict[tuple[str, str], list[int]] = {}
    for row in sf.itertuples(index=False):
        counts.setdefault((str(row.trainee_id), str(row.mission_id)), []).append(int(row.day))

    # ① 先修未满足的课目，出现次数必须为 0
    for (person_id, mission_id), progress in sorted(ctx.progress.items()):
        if progress.prereq_met:
            continue
        days = counts.get((person_id, mission_id), [])
        if days:
            acc.add(
                "C13",
                [person_id, mission_id],
                f"{person_id} 的 {mission_id} 先修未满足"
                f"（{progress.blocked_reason or '先修未达标'}），却被安排了 {len(days)} 次",
            )

    # ② 频率滑窗（S-11 复训整类合并计数，见 `frequency_requirements`）
    for req in frequency_requirements(ctx):
        days = sorted({d for mid in req.mission_ids for d in counts.get((req.person_id, mid), [])})
        subjects = [req.person_id, *req.mission_ids]
        for start, end in req.windows:
            if not any(start <= d <= end for d in days):
                acc.add(
                    "C13",
                    subjects,
                    f"{req.person_id} 的 {req.label} 在周内窗口 "
                    f"[第{start}天, 第{end}天] 内一次都没安排"
                    f"（freq_days={req.freq_days}）",
                )
        if req.deadline is not None and not any(d <= req.deadline for d in days):
            acc.add(
                "C13",
                subjects,
                f"{req.person_id} 的 {req.label} 首次执行须不晚于第 {req.deadline} 天"
                f"（freq_days={req.freq_days}），实际安排在 {days or '本周未安排'}",
            )


def blocked_disclosure_gaps(plan: SchedulePlan, ctx: ValidationContext) -> tuple[str, ...]:
    """阻塞项披露率（v6 §0.3 第四条断言 / §12.3 BLOCKED 专项 ②）。

    **刻意不放进 `check_c13`**：v6 §3.2 约束13 的「校验器独立实现」列只要求
    「断言先修未满足的课目在方案中出现次数 = 0」，没有要求校验披露。披露是
    §0.3 单列的一条交付断言，由 BLOCKED 专项直接调用本函数校验 —— 放进 C13
    会让 naive 比主校验器多判一条，制造假分歧。
    """
    disclosed = {(b.person_id, b.mission_id) for b in plan.blocked_items}
    gaps: list[str] = []
    for (person_id, mission_id), progress in sorted(ctx.progress.items()):
        person = ctx.persons.get(person_id)
        if progress.prereq_met or person is None or person.identity != IDENTITY_STUDENT:
            continue
        if (person_id, mission_id) not in disclosed:
            gaps.append(f"{person_id} 的 {mission_id} 先修未满足但未出现在 blocked_items 中")
    return tuple(gaps)


# ─────────────────────────────────────────────────────────────────────
# 约束14 任务唯一性（rules.pdf 约束14 + req_max = ceil(7/freq_days)）
# ─────────────────────────────────────────────────────────────────────
def req_max_for(freq_days: int) -> int:
    """`req_max = ceil(7 / freq_days)`（v6 §3.2 约束14）。"""
    return max(1, math.ceil(7 / freq_days)) if freq_days > 0 else 1


def check_c14(plan: SchedulePlan, sf: pd.DataFrame, ctx: ValidationContext, acc: _Acc) -> None:
    seen: dict[tuple[object, ...], list[str]] = {}
    for sortie in plan.sorties:
        key = (
            sortie.date,
            sortie.takeoff,
            sortie.landing,
            sortie.mission_id,
            sortie.airspace_id,
            sortie.aircraft_id,
            sortie.runway_id,
            sortie.is_recurrent,
            tuple((m.person_id, m.role) for m in sortie.crew),
        )
        seen.setdefault(key, []).append(sortie.sortie_id)
    for ids in seen.values():
        if len(ids) > 1:
            acc.add("C14", sorted(ids), f"完全重复的架次记录：{'、'.join(sorted(ids))}")

    if sf.empty:
        return
    grouped = sf.groupby(["trainee_id", "mission_id"], sort=True).size()
    for (person_id, mission_id), count in grouped.items():
        mission = ctx.missions.get(str(mission_id))
        if mission is None:
            continue
        cap = req_max_for(mission.freq_days)
        if int(count) > cap:
            acc.add(
                "C14",
                [str(person_id), str(mission_id)],
                f"{person_id} 的 {mission_id} 本周安排 {int(count)} 次 > "
                f"req_max={cap}（ceil(7/{mission.freq_days})）",
            )


# ─────────────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────────────
def _abs_minutes(row: Mapping[str, object]) -> int:
    """把 (日期, 起飞分钟) 折算成周内绝对分钟数（跨日比较用）。"""
    day = row["date"]
    assert isinstance(day, date)
    return day.toordinal() * 24 * 60 + int(str(row["takeoff"]))


def _group_rows(
    frame: pd.DataFrame, keys: Sequence[str]
) -> list[tuple[tuple[object, ...], list[dict[str, object]]]]:
    """`groupby` → 纯 Python 字典列表。后续一律用最笨的两两循环。"""
    if frame.empty:
        return []
    out: list[tuple[tuple[object, ...], list[dict[str, object]]]] = []
    for key, group in frame.groupby(list(keys), sort=True):
        key_tuple = key if isinstance(key, tuple) else (key,)
        rows = [dict(r) for r in group.to_dict(orient="records")]
        rows.sort(key=lambda r: (int(str(r["takeoff"])), str(r["sortie_id"])))
        out.append((key_tuple, rows))
    return out


# ─────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────
def naive_check_all(plan: SchedulePlan, ctx: ValidationContext) -> NaiveReport:
    """14 条逐条重算。**不短路** —— 全部跑完再汇总。"""
    acc = _Acc()
    sf = sortie_frame(plan, ctx)
    cf = crew_frame(plan, ctx)

    check_c01(sf, ctx, acc)
    notes = check_c02(cf, ctx, acc)
    check_c03(plan, sf, ctx, acc)
    check_c04(cf, ctx, acc)
    check_c05(plan, ctx, acc)
    check_c06(sf, ctx, acc)
    check_c07(sf, ctx, acc)
    check_c08(cf, ctx, acc)
    check_c09(sf, ctx, acc)
    check_c10(cf, ctx, acc)
    check_c11(cf, ctx, acc)
    check_c12(sf, cf, ctx, acc)
    check_c13(plan, sf, ctx, acc)
    check_c14(plan, sf, ctx, acc)

    ordered = sorted(acc.items, key=lambda v: (v.rule_id, v.subjects, v.detail))
    return NaiveReport(violations=tuple(ordered), notes=notes)


@dataclass(frozen=True)
class CrossCheckResult:
    """主校验器 vs naive checker 的一次对拍。"""

    label: str
    main_passed: bool
    naive_passed: bool
    main_rules: frozenset[str] = frozenset()
    naive_rules: frozenset[str] = frozenset()
    detail: tuple[str, ...] = field(default_factory=tuple)

    @property
    def verdict_agrees(self) -> bool:
        return self.main_passed == self.naive_passed

    @property
    def rules_agree(self) -> bool:
        return self.main_rules == self.naive_rules

    @property
    def agrees(self) -> bool:
        return self.verdict_agrees and self.rules_agree

    def report(self) -> str:
        mark = "一致" if self.agrees else "★分歧"
        return (
            f"[{mark}] {self.label}: main_passed={self.main_passed} "
            f"naive_passed={self.naive_passed} "
            f"main={sorted(self.main_rules) or '—'} naive={sorted(self.naive_rules) or '—'}"
            + ("".join(f"\n      · {d}" for d in self.detail) if self.detail else "")
        )


def cross_check(
    label: str,
    plan: SchedulePlan,
    ctx: ValidationContext,
    *,
    hard_only: bool = True,
) -> CrossCheckResult:
    """跑主校验器与 naive checker，逐条比对判定。

    `hard_only=True`：只比 HARD 违规。SOFT 是「已松弛且已披露」的欠账，按 M2-B
    收工报告 §8 的约定不算分歧（naive 侧根本不实现降级，见模块文档）。
    """
    from backend.validator import run_all_checks  # 局部 import：本模块可独立使用

    report = run_all_checks(plan, ctx)
    naive = naive_check_all(plan, ctx)
    main_violations = [
        v for v in report.all_violations() if (not hard_only) or v.severity == "HARD"
    ]
    main_rules = frozenset(v.rule_id for v in main_violations)
    naive_rules = naive.violated_rules()
    detail: list[str] = []
    for rid in sorted(main_rules - naive_rules):
        for v in main_violations:
            if v.rule_id == rid:
                detail.append(f"仅主校验器命中 {rid}: {v.detail}")
    for rid in sorted(naive_rules - main_rules):
        for v in naive.by_rule(rid):
            detail.append(f"仅 naive 命中 {rid}: {v.detail}")
    return CrossCheckResult(
        label=label,
        main_passed=bool(report.all_passed) and not main_violations,
        naive_passed=naive.passed,
        main_rules=main_rules,
        naive_rules=naive_rules,
        detail=tuple(detail),
    )


__all__ = [
    "RULE_TITLES",
    "WEEKDAY_NAMES",
    "CrossCheckResult",
    "NaiveReport",
    "NaiveViolation",
    "blocked_disclosure_gaps",
    "check_c01",
    "check_c02",
    "check_c03",
    "check_c04",
    "check_c05",
    "check_c06",
    "check_c07",
    "check_c08",
    "check_c09",
    "check_c10",
    "check_c11",
    "check_c12",
    "check_c13",
    "check_c14",
    "crew_frame",
    "cross_check",
    "frequency_requirements",
    "naive_check_all",
    "req_max_for",
    "sortie_frame",
]
