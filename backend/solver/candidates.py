"""候选枚举与静态预筛（v6 §3.1）。

`Candidate = (mission_id, day, crew, aircraft_id)`。静态预筛这一步就消灭了
约束2/4/5/6 的绝大部分空间——剩下的每个组合对应一个布尔变量 `x[c]`。

## 机组编成判定式（v6 §3.1.1，**这条 v5.2 时期是错的**）

```
需带飞 = (mission.带飞 == 是) ∧ (person.身份 == 学员)
```

| 执行人身份 | mission.带飞 | 机组 | 人数 |
|---|---|---|---|
| 学员 | 是（B~H 类） | 1 教员 + 1 学员 | 2 |
| 学员 | **否（A-1/A-2）** | 1 学员 | **1**（D-1 裁定：学员 A 类单飞） |
| 成熟飞行员 | 任意 | 1 人 | 1（到期资质时 `is_recurrent=True`） |
| 教员 | 任意 | **不生成受训候选**（S-09） | — |

## 两条容易写错的预筛

1. **S-11 例外（v6 §3.1.2）**：对**成熟飞行员**，`day > qual.expiry` 的候选
   **不剔除**，改标 `is_recurrent=True`。学员与教员仍按约束2 字面剔除
   （到期日当日保留，次日起剔除）。
2. **先修未达标 → 不生成候选**，并记入 `blocked_items`（v6 §3.6：BLOCKED 不是
   INFEASIBLE，方案照出、状态照样可以 OPTIMAL，但阻塞项必须 100% 披露）。
   先修判定只调 :func:`backend.retrieval.prereq_cte.evaluate_prereq`。

## 预筛顺序是有语义的

「没有该类别资质」的检查排在「先修未达标」**之前**——否则学员会因为 D/E/G/H
类课目（既无类别资质、机型又是 JL-9，v6 §1.4.1 的双重排除）冒出一堆假的阻塞项。
基准周的阻塞项恰好 7 条（v6 §1.4.2），多一条就是这个顺序被改坏了。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from backend.core.ruleset import (
    IDENTITY_INSTRUCTOR,
    IDENTITY_STUDENT,
    LEVEL_INSTRUCTOR,
    Ruleset,
    Semantics,
)
from backend.nodes.compile_spec import blocked_reason_text, recurrent_since_for
from backend.retrieval.prereq_cte import evaluate_prereq
from backend.schemas.intent import ConstraintSpec
from backend.schemas.plan import BlockedItem
from backend.solver.data import WEEK_DAYS, ProblemData

#: 候选被静态预筛剔除的原因码。这些码是 §3.9 归因的输入——「为什么这个组合
#: 一个候选都没有」是不可行诊断里最常问的问题，答案必须在枚举当时就记下来，
#: 事后从空集合是反推不出来的。
DROP_NO_CLASS_QUAL = "NO_CLASS_QUAL"
DROP_QUAL_EXPIRED = "QUAL_EXPIRED"
DROP_PREREQ_UNMET = "PREREQ_UNMET"
DROP_NO_AIRCRAFT_TYPE = "NO_AIRCRAFT_TYPE"
DROP_NO_CAPABLE_AIRCRAFT = "NO_CAPABLE_AIRCRAFT"
DROP_AIRSPACE_CLOSED = "AIRSPACE_CLOSED"
DROP_PERSON_UNAVAILABLE = "PERSON_UNAVAILABLE"
DROP_AIRCRAFT_MAINTENANCE = "AIRCRAFT_MAINTENANCE"
DROP_NO_RUNWAY = "NO_RUNWAY"
DROP_NO_INSTRUCTOR = "NO_INSTRUCTOR"
DROP_SEATS = "SEATS"
DROP_OUT_OF_SCOPE = "OUT_OF_SCOPE"

#: 归因时的人类可读措辞
DROP_LABELS: Mapping[str, str] = {
    DROP_NO_CLASS_QUAL: "无该课目类别资质（约束4）",
    DROP_QUAL_EXPIRED: "资质已过复训到期日（约束2）",
    DROP_PREREQ_UNMET: "先修未达标（约束13，S-01）",
    DROP_NO_AIRCRAFT_TYPE: "人员机型资质与课目适配机型无交集（约束5/6）",
    DROP_NO_CAPABLE_AIRCRAFT: "在册机队里没有既适配该课目又匹配机型的飞机（约束6）",
    DROP_AIRSPACE_CLOSED: "该课目绑定的空域容量为 0（约束6，空域关闭）",
    DROP_PERSON_UNAVAILABLE: "人员当日不可用（约束2）",
    DROP_AIRCRAFT_MAINTENANCE: "飞机当日维护占满训练窗（约束7）",
    DROP_NO_RUNWAY: "该机型没有可用跑道（约束9，跑道关闭）",
    DROP_NO_INSTRUCTOR: "当日没有可用的带飞教员（约束3/4）",
    DROP_SEATS: "机组人数超过座位数（约束5）",
    DROP_OUT_OF_SCOPE: "不在本次 SolveIntent 的范围内",
}

#: 时隙键 = (受训人, 课目, 天)。**一个时隙最多产出一个架次**（约束14：候选集按
#: (person, mission, day) 唯一），这条性质是 §3.2 里人员侧约束能按「架次」而不是
#: 按「候选」建模的依据。
SlotKey = tuple[str, str, int]


@dataclass(frozen=True)
class Candidate:
    """一个候选架次。`instructor_id is None` 即单飞/复训。"""

    mission_id: str
    day: int
    trainee_id: str
    instructor_id: str | None
    aircraft_id: str
    is_recurrent: bool = False

    @property
    def slot(self) -> SlotKey:
        return (self.trainee_id, self.mission_id, self.day)

    @property
    def crew_ids(self) -> tuple[str, ...]:
        if self.instructor_id is None:
            return (self.trainee_id,)
        return (self.instructor_id, self.trainee_id)

    @property
    def dual(self) -> bool:
        return self.instructor_id is not None

    @property
    def key(self) -> str:
        """稳定的变量名后缀。**变量名进 CP-SAT 模型，必须逐字节可复现。**"""
        return f"{self.mission_id}|d{self.day}|{self.trainee_id}|{self.instructor_id or '-'}|{self.aircraft_id}"

    def sort_key(self) -> tuple[str, int, str, str, str]:
        return (
            self.mission_id,
            self.day,
            self.trainee_id,
            self.instructor_id or "",
            self.aircraft_id,
        )


@dataclass(frozen=True)
class DropReason:
    """(人员, 课目) 组合在预筛里被剔除的记录。`days` 为空表示整周都被剔除。"""

    person_id: str
    mission_id: str
    code: str
    detail: str
    days: tuple[int, ...] = ()

    @property
    def label(self) -> str:
        return DROP_LABELS.get(self.code, self.code)


@dataclass(frozen=True)
class Requirement:
    """一条「必须安排」的要求，统一了约束3、约束13 与 S-11 复训三种来源。

    语义恒为：**`Σ x[c] ≥ min_count`，c 取遍 scope 内落在 `days` 里的候选**。
    把三种来源压成同一个形状，是为了让目标函数（§3.7 阶段1 的完成度）与欠账
    结算（`TrainingDebt`）只需要处理一种对象。
    """

    req_id: str
    rule_id: int
    kind: str
    person_id: str
    days: tuple[int, ...]
    mission_id: str | None = None
    mission_class: str | None = None
    min_count: int = 1
    weight: float = 1.0
    is_debt: bool = False
    debt_count: int = 0
    note: str = ""

    def matches(self, cand: Candidate, mission_class_of: Mapping[str, str]) -> bool:
        if cand.trainee_id != self.person_id or cand.day not in self.days:
            return False
        if self.mission_id is not None:
            return cand.mission_id == self.mission_id
        if self.mission_class is not None:
            return mission_class_of[cand.mission_id] == self.mission_class
        return True


@dataclass(frozen=True)
class DebtBasis:
    """欠账结算的口径：本周「应排几次」是怎么算出来的（附录 B `TrainingDebt.required`）。"""

    person_id: str
    mission_id: str
    required: int
    debt_count: int
    is_debt: bool
    req_ids: tuple[str, ...]


@dataclass(frozen=True)
class CandidateSet:
    """预筛产物。`candidates` 的顺序是确定的（`Candidate.sort_key`）。"""

    candidates: tuple[Candidate, ...]
    blocked_items: tuple[BlockedItem, ...]
    requirements: tuple[Requirement, ...]
    debt_basis: tuple[DebtBasis, ...]
    drops: tuple[DropReason, ...]
    slots: Mapping[SlotKey, tuple[int, ...]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.candidates)

    @property
    def drop_counts(self) -> dict[str, int]:
        acc: dict[str, int] = {}
        for d in self.drops:
            acc[d.code] = acc.get(d.code, 0) + 1
        return dict(sorted(acc.items()))

    def drops_for(self, person_id: str, mission_id: str | None = None) -> tuple[DropReason, ...]:
        return tuple(
            d
            for d in self.drops
            if d.person_id == person_id and (mission_id is None or d.mission_id == mission_id)
        )

    def requirement(self, req_id: str) -> Requirement:
        for req in self.requirements:
            if req.req_id == req_id:
                return req
        raise KeyError(req_id)

    def implied_min_sorties(self, mission_class_of: Mapping[str, str]) -> int:
        """本周**至少**要排多少个架次 —— 由要求集直接算出的下界。

        用途是给 CP-SAT 一条冗余但线性的下界（`Σ present ≥ 这个数`）。它由约束3 与
        约束13 逻辑蕴含，**不改变可行集**；作用是让 LP 松弛一上手就拿到正确下界，
        不必靠 core-guided 搜索一格一格往上顶（基准周实测 12.3s → 秒级）。

        算法：按 (人, 课目类别) 分桶 —— 一个候选只有一个受训人、一个课目、
        因而只属于一个桶，桶之间**互不相交**，所以桶内下界可以直接相加。
        桶内取「该类各课目的最少次数之和」与「该类整体的每周必飞下限」的较大者
        （前者已经蕴含后者时不重复计数）。
        """
        by_bucket: dict[tuple[str, str], int] = {}
        for basis in self.debt_basis:
            key = (basis.person_id, mission_class_of[basis.mission_id])
            by_bucket[key] = by_bucket.get(key, 0) + basis.required
        for req in self.requirements:
            if req.mission_class is None:
                continue
            key = (req.person_id, req.mission_class)
            by_bucket[key] = max(by_bucket.get(key, 0), req.min_count)
        return sum(by_bucket.values())


# ─────────────────────────────────────────────────────────────────────
# 预筛
# ─────────────────────────────────────────────────────────────────────
def _in_scope(value: str, scope: Sequence[str] | str) -> bool:
    return scope == "ALL" or value in scope


def dual_required_for(mission_dual: bool, identity: str, semantics: Semantics) -> bool:
    """§3.1.1 判定式：`需带飞 = (mission.带飞 == 是) ∧ (person.身份 == 学员)`。"""
    if not mission_dual:
        return False
    if semantics.s08_students_only:
        return identity == IDENTITY_STUDENT
    return True


def _eligible_instructors(
    data: ProblemData,
    *,
    mission_class: str,
    aircraft_type: str,
    day: date,
    expiry_inclusive: bool,
    exclude: str,
) -> tuple[str, ...]:
    """当日可担任带飞教员岗的人（S-09：只有身份为「教员」者）。

    刘斌（成熟飞行员）**不在此列**——让他承担带飞教员岗属于 v6 §3.9.3 方案④，
    定级 R0「不可自动执行」，需要先做资质变更。
    """
    out: list[str] = []
    for pid, person in sorted(data.persons.items()):
        if pid == exclude or person.identity != IDENTITY_INSTRUCTOR:
            continue
        if aircraft_type not in person.aircraft_types or day in person.unavailable:
            continue
        qual = person.qual(mission_class)
        if qual is None or qual.level != LEVEL_INSTRUCTOR:
            continue
        if not qual.valid_on(day, expiry_inclusive=expiry_inclusive):
            continue
        out.append(pid)
    return tuple(out)


def enumerate_candidates(
    data: ProblemData,
    spec: ConstraintSpec,
    *,
    ruleset: Ruleset,
    semantics: Semantics,
) -> CandidateSet:
    """候选枚举 + 静态预筛 + 阻塞项 + 约束3/13/S-11 的要求集。"""
    mission_ids = sorted(data.missions)
    expiry_inclusive = ruleset.expiry_inclusive

    candidates: list[Candidate] = []
    blocked: list[BlockedItem] = []
    drops: list[DropReason] = []
    prereq_ok: set[tuple[str, str]] = set()

    for person_id, person in sorted(data.persons.items()):
        # S-09：教员不作为受训人生成候选，只在带飞候选里占教员岗
        if semantics.s09_instructors_exempt and person.identity == IDENTITY_INSTRUCTOR:
            continue
        if not _in_scope(person_id, spec.scope_persons):
            continue

        for mission_id in mission_ids:
            mission = data.missions[mission_id]
            if not _in_scope(mission_id, spec.scope_missions):
                continue

            qual = person.qual(mission.mission_class)
            if qual is None:
                drops.append(
                    DropReason(
                        person_id,
                        mission_id,
                        DROP_NO_CLASS_QUAL,
                        f"{person.name} 无 {mission.mission_class} 类资质",
                    )
                )
                continue

            met, missing = evaluate_prereq(mission.prereqs, person.completed, mission_ids)
            if not met:
                drops.append(
                    DropReason(
                        person_id,
                        mission_id,
                        DROP_PREREQ_UNMET,
                        blocked_reason_text(missing),
                    )
                )
                progress = data.progress_of(person_id, mission_id)
                if progress is None or not progress.completed:
                    blocked.append(
                        BlockedItem(
                            person_id=person_id,
                            mission_id=mission_id,
                            reason=blocked_reason_text(missing),
                            missing_prereqs=list(missing),
                        )
                    )
                continue
            prereq_ok.add((person_id, mission_id))

            usable_types = person.aircraft_types & mission.aircraft_types
            if not usable_types:
                drops.append(
                    DropReason(
                        person_id,
                        mission_id,
                        DROP_NO_AIRCRAFT_TYPE,
                        f"人员机型 {sorted(person.aircraft_types)} ∩ 课目机型 "
                        f"{sorted(mission.aircraft_types)} = ∅",
                    )
                )
                continue

            fleet = [
                ac
                for _, ac in sorted(data.aircraft.items())
                if ac.aircraft_type in usable_types and mission_id in ac.missions
            ]
            if not fleet:
                drops.append(
                    DropReason(
                        person_id,
                        mission_id,
                        DROP_NO_CAPABLE_AIRCRAFT,
                        f"机型 {sorted(usable_types)} 中没有适配 {mission_id} 的在册飞机",
                    )
                )
                continue

            if data.capacity_of(mission.airspace_id) <= 0:
                drops.append(
                    DropReason(
                        person_id,
                        mission_id,
                        DROP_AIRSPACE_CLOSED,
                        f"空域 {mission.airspace_id} 容量为 0",
                    )
                )
                continue

            dual = dual_required_for(mission.dual_required, person.identity, semantics)
            recurrent_from = recurrent_since_for(
                identity=person.identity, expiry=qual.expiry, semantics=semantics
            )

            unavailable_days: list[int] = []
            expired_days: list[int] = []
            maintenance_days: list[int] = []
            no_aircraft_days: list[int] = []
            no_instructor_days: list[int] = []
            seat_blocked = False
            runway_blocked = False

            for day in data.days:
                when = data.date_of(day)
                if when in person.unavailable:
                    unavailable_days.append(day)
                    continue

                is_recurrent = False
                if qual.expired_on(when, expiry_inclusive=expiry_inclusive):
                    if recurrent_from is not None and when >= recurrent_from:
                        is_recurrent = True  # S-11：不剔除，改标复训
                    else:
                        expired_days.append(day)
                        continue

                day_had_aircraft = False
                day_had_instructor = False
                # 逐日记下「这架为什么用不了」，好让归因说得准 ——
                # 全都归成「维护」会在跑道关闭的场景里报出一个不存在的维护窗
                day_maintenance = False
                day_no_runway = False
                for ac in fleet:
                    if any(
                        w.blocks_whole_window(when, data.window_start, data.window_end)
                        for w in ac.maintenance
                    ):
                        day_maintenance = True
                        continue
                    if not data.allowed_runways(ac.aircraft_type):
                        runway_blocked = True
                        day_no_runway = True
                        continue
                    crew_size = 2 if dual else 1
                    if crew_size > ac.seats:
                        seat_blocked = True
                        continue
                    day_had_aircraft = True

                    if not dual:
                        candidates.append(
                            Candidate(
                                mission_id=mission_id,
                                day=day,
                                trainee_id=person_id,
                                instructor_id=None,
                                aircraft_id=ac.aircraft_id,
                                is_recurrent=is_recurrent,
                            )
                        )
                        continue

                    instructors = _eligible_instructors(
                        data,
                        mission_class=mission.mission_class,
                        aircraft_type=ac.aircraft_type,
                        day=when,
                        expiry_inclusive=expiry_inclusive,
                        exclude=person_id,
                    )
                    if instructors:
                        day_had_instructor = True
                    for ins in instructors:
                        candidates.append(
                            Candidate(
                                mission_id=mission_id,
                                day=day,
                                trainee_id=person_id,
                                instructor_id=ins,
                                aircraft_id=ac.aircraft_id,
                                is_recurrent=is_recurrent,
                            )
                        )

                if day_had_aircraft and dual and not day_had_instructor:
                    no_instructor_days.append(day)
                elif not day_had_aircraft and day_maintenance:
                    maintenance_days.append(day)
                elif not day_had_aircraft and not day_no_runway:
                    # 既不是维护也不是跑道 —— 机队里压根没有能飞它的飞机
                    no_aircraft_days.append(day)

            for code, days_hit, detail in (
                (DROP_PERSON_UNAVAILABLE, unavailable_days, f"{person.name} 当日不可用"),
                (
                    DROP_QUAL_EXPIRED,
                    expired_days,
                    f"{mission.mission_class} 类资质到期日 {qual.expiry}",
                ),
                (DROP_AIRCRAFT_MAINTENANCE, maintenance_days, "当日适配机全部处于维护中"),
                (DROP_NO_CAPABLE_AIRCRAFT, no_aircraft_days, "当日没有任何可用的适配飞机"),
                (DROP_NO_INSTRUCTOR, no_instructor_days, "当日无可用带飞教员"),
            ):
                if days_hit:
                    drops.append(DropReason(person_id, mission_id, code, detail, tuple(days_hit)))
            if seat_blocked:
                drops.append(DropReason(person_id, mission_id, DROP_SEATS, "机组人数超过座位数"))
            if runway_blocked:
                drops.append(DropReason(person_id, mission_id, DROP_NO_RUNWAY, "该机型无可用跑道"))

    candidates.sort(key=Candidate.sort_key)
    ordered = tuple(candidates)

    slots: dict[SlotKey, list[int]] = {}
    for idx, cand in enumerate(ordered):
        slots.setdefault(cand.slot, []).append(idx)

    requirements, debt_basis = build_requirements(
        data,
        spec,
        semantics=semantics,
        ruleset=ruleset,
        prereq_ok=prereq_ok,
    )

    return CandidateSet(
        candidates=ordered,
        blocked_items=tuple(sorted(blocked, key=lambda b: (b.person_id, b.mission_id))),
        requirements=requirements,
        debt_basis=debt_basis,
        drops=tuple(drops),
        slots={k: tuple(v) for k, v in sorted(slots.items())},
    )


# ─────────────────────────────────────────────────────────────────────
# 约束3 / 约束13 / S-11 的要求集（v6 §3.5）
# ─────────────────────────────────────────────────────────────────────
def frequency_deadline(
    *, freq_days: int, week_start: date, last_done: date | None, semantics: Semantics
) -> tuple[int, bool]:
    """跨周锚点（v6 §3.5.3，B.4 + D-4 + D-5/S-12）→ `(deadline, is_debt)`。

    - **D-4**：统一取通式 `deadline = max(0, freq_days − gap)`。
      `SPEC_DECISIONS §B.4` 第二分支的 `−1` 是笔误，业务方 2026-08-07 裁定取通式。
    - **S-12/D-5**：`last_done_date is None` → `deadline = freq_days − 1`，
      **且不计欠账**。⚠️ **绝不能写 `gap = 999`** —— 那会让所有未完成课目的
      deadline 归 0、全压在周一，基准周下张勇一人周一需 4 架次，直接违反约束12，
      是**数据初始化造成的假不可行**（CLAUDE.md §11 明列的反模式）。

    返回的 `deadline` 是「本周第几天之前必须飞第一次」。它 **> 6 时本周不构成
    任何约束**（G/H 类 freq_days=14 就落在这一支），调用方据此决定是否下约束。
    """
    if last_done is None:
        return freq_days - 1, semantics.s12_count_as_debt
    gap = max(0, (week_start - last_done).days)
    return max(0, freq_days - gap), gap >= freq_days


def sliding_windows(freq_days: int, week_days: int = WEEK_DAYS) -> tuple[tuple[int, ...], ...]:
    """周内所有长度为 `freq_days` 的完整窗口（v6 §3.5.2）。

    `freq_days > week_days` 时周内不存在完整窗口，返回空——本周是否需要安排
    完全由跨周锚点决定。
    """
    return tuple(tuple(range(s, s + freq_days)) for s in range(0, week_days - freq_days + 1))


def min_hitting_count(day_sets: Sequence[tuple[int, ...]]) -> int:
    """满足全部窗口所需的最少架次数（区间点覆盖，贪心即最优）。

    这是附录 B `TrainingDebt.required` 的口径：「按 freq_days 滑窗推出的本周
    应排次数」。A 类（F=3）→ 2，B~F 类（F=7）→ 1。
    """
    if not day_sets:
        return 0
    remaining = sorted(day_sets, key=lambda s: (max(s), min(s)))
    picked: list[int] = []
    for window in remaining:
        if any(p in window for p in picked):
            continue
        picked.append(max(window))
    return len(picked)


def build_requirements(
    data: ProblemData,
    spec: ConstraintSpec,
    *,
    semantics: Semantics,
    ruleset: Ruleset,
    prereq_ok: set[tuple[str, str]],
) -> tuple[tuple[Requirement, ...], tuple[DebtBasis, ...]]:
    """把约束3、约束13、S-11 复训压成统一的 :class:`Requirement` 列表。

    三者**并存取交集**（v6 §3.5.4）：约束3 管「保持熟练度」（全部学员，不论完成
    状态，S-13），约束13 管「推进进度」（仅未完成且先修满足，S-03）。
    """
    from backend.solver.objective import BASE_MISSION_WEIGHT, DEBT_FACTOR

    reqs: list[Requirement] = []
    basis: list[DebtBasis] = []
    all_days = tuple(data.days)

    for person_id, person in sorted(data.persons.items()):
        if not _in_scope(person_id, spec.scope_persons):
            continue

        # ── 约束13：进度推进（仅学员；成熟飞行员走 S-11） ────────────
        if person.identity == IDENTITY_STUDENT:
            for mission_id in sorted(data.missions):
                if not _in_scope(mission_id, spec.scope_missions):
                    continue
                if (person_id, mission_id) not in prereq_ok:
                    continue  # 先修未满足 → 已记入 blocked_items，不生成候选也不下约束
                progress = data.progress_of(person_id, mission_id)
                if progress is None:
                    continue
                if semantics.s03_incomplete_only and progress.completed:
                    continue  # S-03：已完成课目不受约束13
                mission = data.missions[mission_id]
                weight = BASE_MISSION_WEIGHT * (1 + DEBT_FACTOR * progress.debt_count)
                windows = sliding_windows(mission.freq_days)
                day_sets: list[tuple[int, ...]] = []
                for idx, window in enumerate(windows):
                    reqs.append(
                        Requirement(
                            req_id=f"C13|{person_id}|{mission_id}|w{idx}",
                            rule_id=13,
                            kind="FREQ_WINDOW",
                            person_id=person_id,
                            mission_id=mission_id,
                            days=window,
                            weight=weight,
                            debt_count=progress.debt_count,
                            note=f"任意连续 {mission.freq_days} 天窗口内 ≥1 次",
                        )
                    )
                    day_sets.append(window)
                deadline, is_debt = frequency_deadline(
                    freq_days=mission.freq_days,
                    week_start=data.week_start,
                    last_done=progress.last_done_date,
                    semantics=semantics,
                )
                if deadline <= all_days[-1]:
                    window = tuple(range(0, deadline + 1))
                    reqs.append(
                        Requirement(
                            req_id=f"C13|{person_id}|{mission_id}|deadline",
                            rule_id=13,
                            kind="FREQ_DEADLINE",
                            person_id=person_id,
                            mission_id=mission_id,
                            days=window,
                            weight=weight,
                            is_debt=is_debt,
                            debt_count=progress.debt_count,
                            note=(
                                f"跨周锚点：first_exec_day ≤ {deadline}"
                                f"（last_done_date={progress.last_done_date or 'NULL → S-12'}）"
                            ),
                        )
                    )
                    day_sets.append(window)
                if day_sets:
                    basis.append(
                        DebtBasis(
                            person_id=person_id,
                            mission_id=mission_id,
                            required=min_hitting_count(day_sets),
                            debt_count=progress.debt_count,
                            is_debt=is_debt,
                            req_ids=tuple(
                                r.req_id
                                for r in reqs
                                if r.person_id == person_id and r.mission_id == mission_id
                            ),
                        )
                    )

            # ── 约束3：每周必飞（S-02 类整体 + S-13 全部学员） ────────
            #
            # S-13 的例外（2026-08-12 裁定，v6 Z-9）：**本周每一天都不可用的学员
            # 不计入约束3**。要求一个整周不在的人「每周至少飞 1 次」在语义上不成立，
            # 而不加这条时，任何一名学员整周请假都会让整周判 INFEASIBLE
            # （M2-C 的 200 场景实测，SP-ABS-05~08）。
            #
            # ⚠️ 判据只看 `person.unavailable`，**不看有没有可行候选**。只要还有
            # 一天可用，约束3 照常下 —— 那天排不上是资源问题，必须如实判不可行。
            week_dates = {data.date_of(d) for d in all_days}
            fully_unavailable = (
                semantics.s13_exclude_unavailable and week_dates <= person.unavailable
            )
            for mission_class in data.weekly_required_classes:
                if fully_unavailable:
                    continue
                targets = [
                    mid
                    for mid in data.missions_of_class(mission_class)
                    if data.missions[mid].weekly_required and _in_scope(mid, spec.scope_missions)
                ]
                if not targets:
                    continue
                if semantics.s02_class_level:
                    reqs.append(
                        Requirement(
                            req_id=f"C03|{person_id}|{mission_class}",
                            rule_id=3,
                            kind="WEEKLY_CLASS",
                            person_id=person_id,
                            mission_class=mission_class,
                            days=all_days,
                            min_count=ruleset.weekly_class_min,
                            weight=BASE_MISSION_WEIGHT,
                            note=f"{mission_class} 类整体每周 ≥{ruleset.weekly_class_min} 次（S-02）",
                        )
                    )
                else:
                    for mid in targets:
                        reqs.append(
                            Requirement(
                                req_id=f"C03|{person_id}|{mid}",
                                rule_id=3,
                                kind="WEEKLY_MISSION",
                                person_id=person_id,
                                mission_id=mid,
                                days=all_days,
                                min_count=ruleset.weekly_class_min,
                                weight=BASE_MISSION_WEIGHT,
                                note="每周必飞（S-02 = per_mission）",
                            )
                        )

        # ── S-11：成熟飞行员到期资质的复训（v6 §1.2.4 / §3.2 约束13） ──
        if not semantics.s11_enabled or person.identity not in semantics.s11_identities:
            continue
        for mission_class in sorted({m.mission_class for m in data.missions.values()}):
            qual = person.qual(mission_class)
            since = recurrent_since_for(
                identity=person.identity,
                expiry=qual.expiry if qual else None,
                semantics=semantics,
            )
            if since is None:
                continue
            progress_rows = [
                data.progress_of(person_id, mid) for mid in data.missions_of_class(mission_class)
            ]
            last_done_dates = [p.last_done_date for p in progress_rows if p and p.last_done_date]
            anchor = max(last_done_dates) if last_done_dates else None
            start = max(since, anchor + timedelta(days=1)) if anchor else since
            deadline_date = start + timedelta(days=semantics.s11_window_days - 1)
            if deadline_date > data.week_end or deadline_date < data.week_start:
                continue  # 窗口跨出本周（基准周即此情形）→ 本周不强制，只落锚点
            lo = max(0, data.day_index(start))
            hi = data.day_index(deadline_date)
            reqs.append(
                Requirement(
                    req_id=f"S11|{person_id}|{mission_class}",
                    rule_id=13,
                    kind="RECURRENT",
                    person_id=person_id,
                    mission_class=mission_class,
                    days=tuple(range(lo, hi + 1)),
                    weight=BASE_MISSION_WEIGHT,
                    note=(
                        f"S-11 复训：{mission_class} 类自 {start} 起 "
                        f"{semantics.s11_window_days} 天滑窗内 ≥1 次"
                    ),
                )
            )

    return tuple(reqs), tuple(basis)


__all__ = [
    "DROP_AIRCRAFT_MAINTENANCE",
    "DROP_AIRSPACE_CLOSED",
    "DROP_LABELS",
    "DROP_NO_AIRCRAFT_TYPE",
    "DROP_NO_CAPABLE_AIRCRAFT",
    "DROP_NO_CLASS_QUAL",
    "DROP_NO_INSTRUCTOR",
    "DROP_NO_RUNWAY",
    "DROP_OUT_OF_SCOPE",
    "DROP_PERSON_UNAVAILABLE",
    "DROP_PREREQ_UNMET",
    "DROP_QUAL_EXPIRED",
    "DROP_SEATS",
    "Candidate",
    "CandidateSet",
    "DebtBasis",
    "DropReason",
    "Requirement",
    "SlotKey",
    "build_requirements",
    "dual_required_for",
    "enumerate_candidates",
    "frequency_deadline",
    "min_hitting_count",
    "sliding_windows",
]
