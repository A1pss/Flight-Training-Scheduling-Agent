"""**只服务于 M2-A 窗口的临时断言器**，不是 `backend/validator/`。

## 它为什么在 tests/ 而不在 backend/validator/

CLAUDE.md 铁律 2：独立校验器由另一个完全独立的窗口（M2-B）依据同一份 v6 §3.2
规格表**分别实现**，两套代码不共享任何约束表达逻辑。本窗口需要一个「这个解到底
合不合规」的工具来自测，但**不能**因此去写 `validator/`，也不能预判它的接口形状。

所以这里就是一个直白的 O(n²) 检查器，写在测试目录里，只被 M2-A 的测试引用。
M2-B 窗口不会看到它（它在 `tests/fixtures/` 下，不在 `backend/` 里），
M2-C 的三重独立验证也不会用它。

## 口径来源

一律直接读 `rules/ruleset_v1.3.yaml` 的参数与 PG 里的实体，不复用
`backend/solver/` 的任何函数（除了数据类型定义）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import time
from itertools import pairwise

from backend.core.ruleset import IDENTITY_STUDENT, Ruleset
from backend.schemas.plan import SchedulePlan, Sortie
from backend.solver.data import ProblemData


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.message}"


def _minutes(clock: time) -> int:
    return clock.hour * 60 + clock.minute


def _trainee(sortie: Sortie) -> str:
    for member in sortie.crew:
        if member.role != "教员":
            return member.person_id
    return sortie.crew[0].person_id


def check_plan(plan: SchedulePlan, data: ProblemData, ruleset: Ruleset) -> tuple[Violation, ...]:
    """把 14 条规则里能从方案本身查出来的部分逐条查一遍。"""
    out: list[Violation] = []
    out += _check_time_consistency(plan, data, ruleset)
    out += _check_availability(plan, data, ruleset)
    out += _check_crew(plan, data)
    out += _check_resources(plan, data)
    out += _check_aircraft(plan, data)
    out += _check_person_spacing(plan, data, ruleset)
    out += _check_density(plan, data, ruleset)
    out += _check_caps(plan, data, ruleset)
    out += _check_uniqueness(plan, data)
    out += _check_blocked_never_scheduled(plan)
    return tuple(out)


def _check_time_consistency(
    plan: SchedulePlan, data: ProblemData, ruleset: Ruleset
) -> list[Violation]:
    out: list[Violation] = []
    for s in plan.sorties:
        dur = data.missions[s.mission_id].duration_minutes
        if _minutes(s.landing) - _minutes(s.takeoff) != dur:
            out.append(Violation("C01", f"{s.sortie_id} 着陆 ≠ 起飞 + 时长({dur})"))
        if _minutes(s.takeoff) < _minutes(ruleset.window_start):
            out.append(Violation("C01", f"{s.sortie_id} 起飞早于训练窗"))
        if _minutes(s.landing) > _minutes(ruleset.window_end):
            out.append(Violation("C01", f"{s.sortie_id} 着陆晚于训练窗"))
        if not (plan.week_start <= s.date <= plan.week_end):
            out.append(Violation("C01", f"{s.sortie_id} 日期越出排班周"))
    return out


def _check_availability(plan: SchedulePlan, data: ProblemData, ruleset: Ruleset) -> list[Violation]:
    """约束2。**成熟飞行员按 S-11 判定，不按字面**（授权改写，不是漏判）。"""
    out: list[Violation] = []
    for s in plan.sorties:
        for member in s.crew:
            person = data.persons[member.person_id]
            if s.date in person.unavailable:
                out.append(Violation("C02", f"{s.sortie_id} {member.person_id} 当日不可用"))
            qual = person.qual(data.missions[s.mission_id].mission_class)
            if qual is None:
                out.append(Violation("C04", f"{s.sortie_id} {member.person_id} 无类别资质"))
                continue
            expired = not qual.valid_on(s.date, expiry_inclusive=ruleset.expiry_inclusive)
            if expired and not (s.is_recurrent and member.role == "复训"):
                out.append(Violation("C02", f"{s.sortie_id} {member.person_id} 资质已过期"))
    return out


def _check_crew(plan: SchedulePlan, data: ProblemData) -> list[Violation]:
    """约束3/5 的编成部分：按 §3.1.1 判定式重算期望人数再比对。"""
    out: list[Violation] = []
    for s in plan.sorties:
        mission = data.missions[s.mission_id]
        trainee = data.persons[_trainee(s)]
        expect_dual = mission.dual_required and trainee.identity == IDENTITY_STUDENT
        expect_size = 2 if expect_dual else 1
        if len(s.crew) != expect_size:
            out.append(
                Violation("C05", f"{s.sortie_id} 机组人数 {len(s.crew)} ≠ 期望 {expect_size}")
            )
        if expect_dual and sorted(m.role for m in s.crew) != sorted(["学员", "教员"]):
            out.append(Violation("C03", f"{s.sortie_id} 带飞架次角色不是 1 教员 + 1 学员"))
        if len(s.crew) > data.aircraft[s.aircraft_id].seats:
            out.append(Violation("C05", f"{s.sortie_id} 机组人数超过座位数"))
        for member in s.crew:
            if (
                data.aircraft[s.aircraft_id].aircraft_type
                not in data.persons[member.person_id].aircraft_types
            ):
                out.append(Violation("C05", f"{s.sortie_id} {member.person_id} 无该机型资质"))
    return out


def _check_resources(plan: SchedulePlan, data: ProblemData) -> list[Violation]:
    """约束6：机号在册 + 机型适配 + **空域同时段容量**（扫描线）。"""
    out: list[Violation] = []
    for s in plan.sorties:
        ac = data.aircraft.get(s.aircraft_id)
        if ac is None:
            out.append(Violation("C06", f"{s.sortie_id} 机号不在册"))
            continue
        mission = data.missions[s.mission_id]
        if ac.aircraft_type not in mission.aircraft_types:
            out.append(Violation("C06", f"{s.sortie_id} 机型不适配该课目"))
        if s.mission_id not in ac.missions:
            out.append(Violation("C06", f"{s.sortie_id} 该机不适配该课目"))
        if s.airspace_id != mission.airspace_id:
            out.append(Violation("C06", f"{s.sortie_id} 空域与课目绑定不符"))

    events: dict[str, list[tuple[int, int, int]]] = {}
    for s in plan.sorties:
        key = s.airspace_id
        day = data.day_index(s.date)
        events.setdefault(key, []).append((day, _minutes(s.takeoff), +1))
        events[key].append((day, _minutes(s.landing), -1))
    for airspace_id, evs in events.items():
        cap = data.capacity_of(airspace_id)
        concurrent = 0
        # 同刻先减后加（着陆与起飞同刻不算并发）
        for _day, _minute, delta in sorted(evs, key=lambda e: (e[0], e[1], e[2])):
            concurrent += delta
            if concurrent > cap:
                out.append(Violation("C06", f"空域 {airspace_id} 并发 {concurrent} > 容量 {cap}"))
    return out


def _check_aircraft(plan: SchedulePlan, data: ProblemData) -> list[Violation]:
    """约束7：同机相邻架次周转（S-06 着陆→起飞）+ 维护窗重叠。"""
    out: list[Violation] = []
    by_key: dict[tuple[str, int], list[Sortie]] = {}
    for s in plan.sorties:
        by_key.setdefault((s.aircraft_id, data.day_index(s.date)), []).append(s)
    for (aircraft_id, _day), sorties in by_key.items():
        turnaround = data.aircraft[aircraft_id].turnaround_minutes
        ordered = sorted(sorties, key=lambda s: _minutes(s.takeoff))
        for prev, nxt in pairwise(ordered):
            gap = _minutes(nxt.takeoff) - _minutes(prev.landing)
            if gap < turnaround:
                out.append(
                    Violation(
                        "C07",
                        f"{aircraft_id} {prev.sortie_id}→{nxt.sortie_id} 周转 {gap} < {turnaround}",
                    )
                )
        for s in sorties:
            for window in data.aircraft[aircraft_id].maintenance:
                span = window.minute_span(s.date, data.window_start)
                if span and span[0] < _minutes(s.landing) and _minutes(s.takeoff) < span[1]:
                    out.append(Violation("C07", f"{s.sortie_id} 与维护时段重叠"))
    return out


def _check_person_spacing(
    plan: SchedulePlan, data: ProblemData, ruleset: Ruleset
) -> list[Violation]:
    """约束4（不重叠）+ 约束8（≥10 分钟；连续 2 架次后 ≥30 分钟，S-07 仅同日）。"""
    out: list[Violation] = []
    by_key: dict[tuple[str, int], list[Sortie]] = {}
    for s in plan.sorties:
        for member in s.crew:
            by_key.setdefault((member.person_id, data.day_index(s.date)), []).append(s)
    for (person_id, _day), sorties in by_key.items():
        ordered = sorted(sorties, key=lambda s: _minutes(s.takeoff))
        for i, (prev, nxt) in enumerate(pairwise(ordered)):
            gap = _minutes(nxt.takeoff) - _minutes(prev.landing)
            if gap < 0:
                out.append(Violation("C04", f"{person_id} 架次重叠"))
            if gap < ruleset.min_gap_minutes:
                out.append(Violation("C08", f"{person_id} 间隔 {gap} < {ruleset.min_gap_minutes}"))
            if i + 1 >= ruleset.rest_after_n and gap < ruleset.rest_minutes:
                out.append(
                    Violation(
                        "C08",
                        f"{person_id} 第 {i + 2} 架次前休息 {gap} < {ruleset.rest_minutes}",
                    )
                )
    return out


def _check_density(plan: SchedulePlan, data: ProblemData, ruleset: Ruleset) -> list[Violation]:
    """约束9（S-04 + S-05 + D-2）。

    - 20 分钟滑窗 ≤2 次：**按 (日, 跑道) 分组**
    - 任意两次起飞间隔 ≥7 分钟：**按日全场统一，跨跑道也算**
    """
    out: list[Violation] = []
    for s in plan.sorties:
        allowed = data.allowed_runways(data.aircraft[s.aircraft_id].aircraft_type)
        if s.runway_id not in allowed:
            out.append(Violation("C09", f"{s.sortie_id} 跑道 {s.runway_id} 不服务该机型"))

    per_runway: dict[tuple[int, str], list[int]] = {}
    per_day: dict[int, list[tuple[int, str]]] = {}
    for s in plan.sorties:
        day = data.day_index(s.date)
        per_runway.setdefault((day, s.runway_id), []).append(_minutes(s.takeoff))
        per_day.setdefault(day, []).append((_minutes(s.takeoff), s.sortie_id))

    window = ruleset.density_window_minutes
    for (day, runway_id), times in per_runway.items():
        for anchor in sorted(times):
            inside = [t for t in times if anchor <= t < anchor + window]  # 半开 [t, t+20)
            if len(inside) > ruleset.density_window_cap:
                out.append(
                    Violation(
                        "C09",
                        f"day{day} {runway_id} 窗口 [{anchor},{anchor + window}) "
                        f"起飞 {len(inside)} > {ruleset.density_window_cap}",
                    )
                )
    for day, entries in per_day.items():
        ordered = sorted(entries)
        for (t1, id1), (t2, id2) in pairwise(ordered):
            if t2 - t1 < ruleset.separation_minutes:
                out.append(
                    Violation(
                        "C09",
                        f"day{day} {id1}→{id2} 全场起飞间隔 {t2 - t1} "
                        f"< {ruleset.separation_minutes}",
                    )
                )
    return out


def _check_caps(plan: SchedulePlan, data: ProblemData, ruleset: Ruleset) -> list[Violation]:
    """约束10 / 11 / 12。"""
    out: list[Violation] = []
    minutes: dict[tuple[str, int], int] = {}
    per_person_day: dict[tuple[str, int], int] = {}
    per_person: dict[str, int] = {}
    per_aircraft_day: dict[tuple[str, int], int] = {}
    for s in plan.sorties:
        day = data.day_index(s.date)
        dur = data.missions[s.mission_id].duration_minutes
        per_aircraft_day[s.aircraft_id, day] = per_aircraft_day.get((s.aircraft_id, day), 0) + 1
        for member in s.crew:
            minutes[member.person_id, day] = minutes.get((member.person_id, day), 0) + dur
            per_person_day[member.person_id, day] = (
                per_person_day.get((member.person_id, day), 0) + 1
            )
            per_person[member.person_id] = per_person.get(member.person_id, 0) + 1
    for (person_id, day), total in minutes.items():
        cap = ruleset.daily_minute_cap(data.persons[person_id].identity)
        if total > cap:
            out.append(Violation("C10", f"{person_id} day{day} 时长 {total} > {cap}"))
    for (person_id, day), count in per_person_day.items():
        if count > ruleset.daily_sorties_per_person:
            out.append(
                Violation(
                    "C12", f"{person_id} day{day} 架次 {count} > {ruleset.daily_sorties_per_person}"
                )
            )
    for person_id, count in per_person.items():
        cap = ruleset.weekly_sortie_cap(data.persons[person_id].identity)
        if count > cap:
            out.append(Violation("C11", f"{person_id} 周架次 {count} > {cap}"))
    for (aircraft_id, day), count in per_aircraft_day.items():
        if count > ruleset.daily_sorties_per_aircraft:
            out.append(
                Violation(
                    "C12",
                    f"{aircraft_id} day{day} 架次 {count} > {ruleset.daily_sorties_per_aircraft}",
                )
            )
    return out


def _check_uniqueness(plan: SchedulePlan, data: ProblemData) -> list[Violation]:
    """约束14：`Σ ≤ ceil(7 / freq_days)` per (人, 课目)，且无完全重复记录。"""
    import math

    out: list[Violation] = []
    counts: dict[tuple[str, str], int] = {}
    per_day: dict[tuple[str, str, int], int] = {}
    for s in plan.sorties:
        key = (_trainee(s), s.mission_id)
        counts[key] = counts.get(key, 0) + 1
        dkey = (*key, data.day_index(s.date))
        per_day[dkey] = per_day.get(dkey, 0) + 1
    for (person_id, mission_id), count in counts.items():
        cap = math.ceil(7 / data.missions[mission_id].freq_days)
        if count > cap:
            out.append(Violation("C14", f"{person_id} {mission_id} 次数 {count} > {cap}"))
    for (person_id, mission_id, day), count in per_day.items():
        if count > 1:
            out.append(Violation("C14", f"{person_id} {mission_id} day{day} 重复 {count} 次"))
    return out


def _check_blocked_never_scheduled(plan: SchedulePlan) -> list[Violation]:
    """约束13 的后半句：先修未满足的组合在方案中出现次数必须为 0。"""
    blocked = {(b.person_id, b.mission_id) for b in plan.blocked_items}
    out: list[Violation] = []
    for s in plan.sorties:
        for member in s.crew:
            if (member.person_id, s.mission_id) in blocked:
                out.append(Violation("C13", f"{s.sortie_id} 排了被 BLOCKED 的 {s.mission_id}"))
    return out


def format_violations(violations: Iterable[Violation]) -> str:
    return "\n".join(str(v) for v in violations) or "（无违规）"


__all__ = ["Violation", "check_plan", "format_violations"]
