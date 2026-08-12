"""14 条规则的独立校验实现（v6 §3.2「校验器独立实现」列 + §4.2）。

**纯函数：不调 LLM，不依赖 OR-Tools，也不引用求解器与 `skills_loader` 的任何模块。**
本模块是 v6 §4.1 的闸门1：另一套代码，只读解，依 §3.2 规格表重算 14 条。

## 三条必须与求解侧对齐的口径（对不齐必然 FTS-3003，CRITICAL）

1. **空域并发（C06）**：同一时刻**先减后加** —— 一个架次 06:35 着陆、另一个
   06:35 起飞，**不算并发**。求解侧用半开区间 `[start, start+dur)`，两边口径
   不一致会在容量=1 的空域上直接产出「求解器判合规、校验器判违规」。
2. **起降密度（C09，D-2）**：20 分钟窗口按 **(日, 跑道)** 分组、半开 `[t, t+20)`；
   7 分钟间隔 **全场按日**，**不分跑道**。`rules.pdf` 约束9 原文只对前半句限定了
   「同一跑道」。把 7 分钟实现成按跑道分组是 v5.2 时期的错误理解。
3. **人员可用性（C02，S-11）**：对**成熟飞行员**按 S-11 判定而非 `rules.pdf`
   约束2 的字面语义 —— 到期资质转复训，**到期次日起继续飞不报违规**，并在报告里
   标注为「业务方授权改写」（`CheckResult.notes`，v6 §1.2.4 / §10.4 区块6）。

## `passed` 与 `severity` 的关系

`passed = 没有 HARD 违规`。SOFT 违规只出现在**被松弛阶梯显式放宽、且欠账已在
`plan.debts` 里如实披露**的场景（约束13 / 约束3，见 v6 §3.10）：这类缺口不该让
闸门1 拦下方案，但必须在报告里看得见。**未披露的欠账仍是 HARD** —— 「欠账 100%
显式披露」是 v6 §0.3 的可测断言之一。R0 的九条（1/2/4/5/6/7/8/9/14）恒为 HARD。

## `checked_items` 是刻意设计

前端展示「约束7 ✅ 已检查 47 项」比单纯打勾更有说服力，也能发现「检查了 0 项」
这种假通过（v6 §4.2 脚注）。因此每条 check 都**如实计数真实检查对象**，不写 0、
不写死常数。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import date, time, timedelta
from itertools import tee
from time import perf_counter
from typing import TypeVar

from backend.core.ruleset import (
    IDENTITY_INSTRUCTOR,
    IDENTITY_STUDENT,
    Ruleset,
    req_max_for,
)
from backend.schemas.plan import CrewMember, SchedulePlan, Sortie
from backend.schemas.validation import CheckResult, ValidationReport, Violation
from backend.validator.context import (
    WEEK_DAYS,
    PersonFacts,
    ProgressFacts,
    ValidationContext,
)

#: v6 §3.2 的 14 条规则名（Sheet 4 区块2 逐行照抄它）
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

#: 受训人角色（与「教员岗」相对）。教员岗只有 `教员` 一个取值。
TRAINEE_ROLES: frozenset[str] = frozenset({"学员", "单飞", "复训"})

#: S-11 授权改写声明（v6 §1.2.4 强制项，对应风险 R17）
S11_AUTHORIZED_REWRITE_NOTE = (
    "S-11：成熟飞行员到期资质转复训（自到期次日起按 7 天滑窗强制安排），"
    "系对 rules.pdf 约束2 字面语义的**业务方授权改写**（2026-08-06 裁定），"
    "非校验器漏判。"
)

_T = TypeVar("_T")
_K = TypeVar("_K")


# ─────────────────────────────────────────────────────────────────────
# 小工具
# ─────────────────────────────────────────────────────────────────────
def minutes_of(t: time) -> int:
    """`HH:MM` → 当日零点起的分钟数。"""
    return t.hour * 60 + t.minute


def fmt_minutes(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def group_by(items: Iterable[_T], key: Callable[[_T], _K]) -> dict[_K, list[_T]]:
    """分组，保持插入顺序。调用方负责先把输入排成确定序。"""
    acc: dict[_K, list[_T]] = {}
    for item in items:
        acc.setdefault(key(item), []).append(item)
    return acc


def pairwise(seq: Iterable[_T]) -> Iterator[tuple[_T, _T]]:
    a, b = tee(seq)
    next(b, None)
    return zip(a, b, strict=False)


def _ordered(sorties: Sequence[Sortie]) -> list[Sortie]:
    """确定序：按 (日期, 起飞, 架次号)。报告必须逐字节可复现（铁律 9）。"""
    return sorted(sorties, key=lambda s: (s.date, s.takeoff, s.sortie_id))


def trainee_of(sortie: Sortie) -> CrewMember | None:
    """架次的受训人 —— 机组里唯一一个非「教员」角色的成员。

    找不到（全是教员岗）或多于一个时返回 None：那本身就是编成违规，由 C03 报，
    其余 check 不再据此推导期望值（避免同一个错被算成三条不同的违规）。
    """
    candidates = [c for c in sortie.crew if c.role in TRAINEE_ROLES]
    return candidates[0] if len(candidates) == 1 else None


def expected_crew_size(*, dual_required: bool, trainee_identity: str) -> int:
    """§3.1.1 判定式：`需带飞 = (mission.带飞 == 是) ∧ (person.身份 == 学员)`。

    - 学员 + 带飞=是（B~H 类）→ 1 教员 + 1 学员 = **2**
    - 学员 + 带飞=否（A-1/A-2，D-1 裁定）→ 学员**单飞** = **1**
    - 成熟飞行员 → 单飞 / 复训 = **1**
    """
    return 2 if (dual_required and trainee_identity == IDENTITY_STUDENT) else 1


def relaxed_rule_ids(plan: SchedulePlan, ruleset: Ruleset) -> frozenset[int]:
    """本方案松弛档位放宽了哪几条（v6 §3.10 的松弛阶梯）。R0 恒不在其中。"""
    return frozenset(ruleset.ladder_step(plan.relaxation_tier).relaxes)


def _disclosed_debt(plan: SchedulePlan, person_id: str, mission_id: str) -> bool:
    """该 (人, 课目) 的缺口是否已在 `plan.debts` 里如实披露。"""
    return any(d.person_id == person_id and d.mission_id == mission_id for d in plan.debts)


def _finish(
    rule_id: str,
    started: float,
    checked: int,
    violations: Sequence[Violation],
    notes: Sequence[str] = (),
) -> CheckResult:
    hard = [v for v in violations if v.severity == "HARD"]
    return CheckResult(
        rule_id=rule_id,
        rule_title=RULE_TITLES[rule_id],
        passed=not hard,
        checked_items=checked,
        violations=list(violations),
        duration_ms=(perf_counter() - started) * 1000.0,
        notes=list(notes),
    )


def _v(
    rule_id: str,
    subjects: Sequence[str],
    detail: str,
    fix_hint: str | None = None,
    severity: str = "HARD",
) -> Violation:
    return Violation(
        rule_id=rule_id,
        severity="SOFT" if severity == "SOFT" else "HARD",
        subjects=list(subjects),
        detail=detail,
        fix_hint=fix_hint,
    )


# ─────────────────────────────────────────────────────────────────────
# 约束1 · 时间一致性
# ─────────────────────────────────────────────────────────────────────
WEEKDAY_LABELS: tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def check_c01(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """逐条重算 `land == takeoff + duration`，比对训练窗，检查 day 一致、不跨日。"""
    started = perf_counter()
    v: list[Violation] = []
    win_start = minutes_of(ctx.ruleset.window_start)
    win_end = minutes_of(ctx.ruleset.window_end)
    cross_day_allowed = ctx.ruleset.cross_day_allowed
    checked = 0

    for s in _ordered(plan.sorties):
        checked += 1
        t0, t1 = minutes_of(s.takeoff), minutes_of(s.landing)
        mission = ctx.missions.get(s.mission_id)
        if mission is None:
            v.append(
                _v("C01", [s.sortie_id, s.mission_id], f"课目 {s.mission_id} 不在册，无法重算时长")
            )
        elif t1 - t0 != mission.duration_minutes:
            v.append(
                _v(
                    "C01",
                    [s.sortie_id, s.mission_id],
                    f"{s.sortie_id} 时长 {t1 - t0} 分钟 ≠ {s.mission_id} 标准时长 "
                    f"{mission.duration_minutes} 分钟",
                    f"着陆时刻应为 {fmt_minutes(t0 + mission.duration_minutes)}",
                )
            )
        if not cross_day_allowed and t1 <= t0:
            v.append(
                _v(
                    "C01",
                    [s.sortie_id],
                    f"{s.sortie_id} 着陆 {s.landing} 不晚于起飞 {s.takeoff}（跨日）",
                )
            )
        if t0 < win_start or t1 > win_end:
            v.append(
                _v(
                    "C01",
                    [s.sortie_id],
                    f"{s.sortie_id} {s.takeoff}~{s.landing} 越出训练窗 "
                    f"{fmt_minutes(win_start)}-{fmt_minutes(win_end)}",
                )
            )
        expected_weekday = WEEKDAY_LABELS[s.date.weekday()]
        if s.weekday != expected_weekday:
            v.append(
                _v(
                    "C01",
                    [s.sortie_id],
                    f"{s.sortie_id} 星期列 {s.weekday} 与日期 {s.date} 不一致"
                    f"（应为 {expected_weekday}）",
                )
            )
    return _finish("C01", started, checked, v)


# ─────────────────────────────────────────────────────────────────────
# 约束2 · 人员可用性（含 S-11 授权改写）
# ─────────────────────────────────────────────────────────────────────
def check_c02(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """遍历架次×人员，查不可用表与资质到期表。

    **S-11 例外**：对成熟飞行员，到期资质不报违规，改为要求该架次标 `is_recurrent`
    且角色为「复训」；学员与教员仍按约束2 字面执行（到期日当日保留，次日起禁排）。
    """
    started = perf_counter()
    v: list[Violation] = []
    notes: list[str] = []
    checked = 0
    s11_on = ctx.semantics.s11_enabled
    s11_identities = set(ctx.semantics.s11_identities)
    if s11_on:
        notes.append(S11_AUTHORIZED_REWRITE_NOTE)

    for s in _ordered(plan.sorties):
        mission = ctx.missions.get(s.mission_id)
        for c in s.crew:
            checked += 1
            person = ctx.persons.get(c.person_id)
            if person is None:
                v.append(_v("C02", [s.sortie_id, c.person_id], f"人员 {c.person_id} 不在册"))
                continue
            if s.date in person.unavailable_dates:
                v.append(
                    _v(
                        "C02",
                        [s.sortie_id, c.person_id],
                        f"{person.name}({c.person_id}) 在 {s.date} 不可用，仍被排入 {s.sortie_id}",
                        f"改派其他人员或把 {s.sortie_id} 移出 {s.date}",
                    )
                )
            if mission is None:
                continue
            qual = person.qualification_of(mission.mission_class)
            if qual is None or qual.expiry_date is None:
                continue
            if s.date <= qual.expiry_date:
                continue  # 到期日**当日**仍可执行（expiry_inclusive）
            if s11_on and person.identity in s11_identities:
                # S-11：不是违规，是授权改写。但语义要求它必须是一个「复训架次」
                if not s.is_recurrent or c.role != "复训":
                    v.append(
                        _v(
                            "C02",
                            [s.sortie_id, c.person_id],
                            f"{person.name}({c.person_id}) 的 {mission.mission_class} 类资质已于 "
                            f"{qual.expiry_date} 到期，{s.date} 的 {s.sortie_id} 应按 S-11 标记为"
                            f"复训架次（is_recurrent=True 且角色为「复训」），实际 "
                            f"is_recurrent={s.is_recurrent} 角色={c.role}",
                            "把该架次标为复训，或改排未到期的课目",
                        )
                    )
                else:
                    notes.append(
                        f"S-11 生效实例：{person.name}({c.person_id}) {mission.mission_class} 类"
                        f"于 {qual.expiry_date} 到期，{s.sortie_id}({s.date}) 按复训安排。"
                    )
                continue
            v.append(
                _v(
                    "C02",
                    [s.sortie_id, c.person_id],
                    f"{person.name}({c.person_id}) 的 {mission.mission_class} 类资质 "
                    f"{qual.expiry_date} 到期，{s.date} 不得再执行 {s.mission_id}",
                    "先安排复训或改派持有效资质的人员",
                )
            )
    return _finish("C02", started, checked, v, notes)


# ─────────────────────────────────────────────────────────────────────
# 约束3 · 角色配置（S-02 + S-13 + §3.1.1）
# ─────────────────────────────────────────────────────────────────────
def _fully_unavailable(person: PersonFacts, ctx: ValidationContext) -> bool:
    """该人本周**每一天**都不可用（S-13 例外的判据，v6 `Z-9`）。

    刻意**不**问「他有没有可行候选」：候选为空可能是飞机全在修、空域关了、
    跑道关了 —— 那些是资源不足，必须如实判不可行。只有「人不在」才豁免。
    """
    week = {ctx.week_start + timedelta(days=i) for i in range(WEEK_DAYS)}
    return week <= set(person.unavailable_dates)


def check_c03(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """机组角色配置 + 每名学员每周至少 1 次「每周必飞」类课目。

    - 带飞架次（2 人）：教员数 == 1 且 学员数 == 1
    - 单飞/复训架次（1 人）：人数 == 1 且角色 ∈ {单飞, 复训}
    - 期望编成由 §3.1.1 判定式独立重算：**A-1/A-2 的带飞列为「否」→ 学员单飞，
      机组 1 人不带教员**（D-1）。见到带教员的 A 类架次即违规。
    - 每周必飞（S-02 类内合并计数 + S-13 对全部学员生效，不论完成状态）
    """
    started = perf_counter()
    v: list[Violation] = []
    checked = 0
    soft = 3 in relaxed_rule_ids(plan, ctx.ruleset)

    for s in _ordered(plan.sorties):
        checked += 1
        roles = [c.role for c in s.crew]
        instructors = [c for c in s.crew if c.role == "教员"]
        trainees = [c for c in s.crew if c.role in TRAINEE_ROLES]

        if len(s.crew) == 2:
            if len(instructors) != 1 or len([c for c in s.crew if c.role == "学员"]) != 1:
                v.append(
                    _v(
                        "C03",
                        [s.sortie_id],
                        f"{s.sortie_id} 双人架次机组必须为 1 教员 + 1 学员，实际角色 {roles}",
                    )
                )
        elif len(s.crew) == 1:
            if roles[0] not in ("单飞", "复训"):
                v.append(
                    _v(
                        "C03",
                        [s.sortie_id],
                        f"{s.sortie_id} 单人架次角色必须为 单飞/复训，实际 {roles[0]}",
                    )
                )
        else:
            v.append(
                _v(
                    "C03",
                    [s.sortie_id],
                    f"{s.sortie_id} 机组人数 {len(s.crew)} 非法（应为 1 或 2）",
                )
            )

        # 角色与身份必须自洽：刘斌（成熟飞行员）被标成「教员」带飞即在此拦下
        for c in s.crew:
            person = ctx.persons.get(c.person_id)
            if person is None:
                continue
            if c.role == "教员" and person.identity != IDENTITY_INSTRUCTOR:
                v.append(
                    _v(
                        "C03",
                        [s.sortie_id, c.person_id],
                        f"{person.name}({c.person_id}) 身份为「{person.identity}」，"
                        f"不得在 {s.sortie_id} 上担任「教员」角色",
                        "改由教员带飞，或按其真实身份改为单飞/复训架次",
                    )
                )
            if c.role == "学员" and person.identity != IDENTITY_STUDENT:
                v.append(
                    _v(
                        "C03",
                        [s.sortie_id, c.person_id],
                        f"{person.name}({c.person_id}) 身份为「{person.identity}」，"
                        f"不得在 {s.sortie_id} 上占据「学员」岗",
                    )
                )
            if c.role == "复训" and person.identity == IDENTITY_STUDENT:
                v.append(
                    _v(
                        "C03",
                        [s.sortie_id, c.person_id],
                        f"学员 {person.name}({c.person_id}) 不适用 S-11 复训（仅成熟飞行员适用）",
                    )
                )

        # 期望编成（§3.1.1 判定式）独立重算
        mission = ctx.missions.get(s.mission_id)
        trainee = trainees[0] if len(trainees) == 1 else None
        if mission is not None and trainee is not None:
            person = ctx.persons.get(trainee.person_id)
            if person is not None:
                want = expected_crew_size(
                    dual_required=mission.dual_required, trainee_identity=person.identity
                )
                if want != len(s.crew):
                    v.append(
                        _v(
                            "C03",
                            [s.sortie_id, trainee.person_id],
                            f"{s.sortie_id}（{s.mission_id} 带飞={'是' if mission.dual_required else '否'}"
                            f"，受训人身份={person.identity}）按 §3.1.1 判定式应为 {want} 人机组，"
                            f"实际 {len(s.crew)} 人：{[c.name for c in s.crew]}",
                            "带飞=否的课目由学员单飞，不得带教员"
                            if want == 1
                            else "该课目学员须由教员带飞",
                        )
                    )

    # 每周必飞（约束3 主体）：类别由课目表的 weekly_required 推出，不写死「A 类」
    for mission_class in ctx.weekly_required_classes():
        class_missions = set(ctx.missions_of_class(mission_class))
        for student in ctx.students():
            if student.qualification_of(mission_class) is None:
                # 不持该类资质的学员飞它本身违反约束4；要求他每周必飞会自相矛盾
                continue
            if ctx.semantics.s13_exclude_unavailable and _fully_unavailable(student, ctx):
                # S-13 的例外（2026-08-12 裁定，v6 Z-9）：本周一天都不可用的学员
                # 不计入约束3 —— 要求一个整周不在的人「每周至少飞 1 次」不成立。
                # **判据只看可用性**：还有一天可用就照常要求（见 `_fully_unavailable`）。
                continue
            checked += 1
            flown = sum(
                1
                for s in plan.sorties
                if s.mission_id in class_missions
                and any(
                    c.person_id == student.person_id and c.role in TRAINEE_ROLES for c in s.crew
                )
            )
            if flown < ctx.ruleset.weekly_class_min:
                v.append(
                    _v(
                        "C03",
                        [student.person_id, mission_class],
                        f"学员 {student.name}({student.person_id}) 本周 {mission_class} 类课目"
                        f"（{'/'.join(sorted(class_missions))} 合并计数）安排 {flown} 次，"
                        f"少于每周必飞下限 {ctx.ruleset.weekly_class_min} 次",
                        f"为其增排 1 次 {mission_class} 类课目",
                        severity="SOFT"
                        if soft
                        and any(_disclosed_debt(plan, student.person_id, m) for m in class_missions)
                        else "HARD",
                    )
                )
    return _finish("C03", started, checked, v)


# ─────────────────────────────────────────────────────────────────────
# 约束4 · 资质匹配与岗位互斥
# ─────────────────────────────────────────────────────────────────────
def check_c04(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """查类别资质；对每人做区间两两相交检测；断言学员未任教员岗、教员未受训。"""
    started = perf_counter()
    v: list[Violation] = []
    checked = 0

    for s in _ordered(plan.sorties):
        mission = ctx.missions.get(s.mission_id)
        for c in s.crew:
            checked += 1
            person = ctx.persons.get(c.person_id)
            if person is None:
                continue
            if mission is not None and person.qualification_of(mission.mission_class) is None:
                v.append(
                    _v(
                        "C04",
                        [s.sortie_id, c.person_id],
                        f"{person.name}({c.person_id}) 不持 {mission.mission_class} 类资质，"
                        f"不得执行 {s.mission_id}（{s.sortie_id}）",
                    )
                )
            if c.role == "教员" and person.identity != IDENTITY_INSTRUCTOR:
                v.append(
                    _v(
                        "C04",
                        [s.sortie_id, c.person_id],
                        f"岗位互斥：{person.name}({c.person_id}) 身份为「{person.identity}」，"
                        f"占据了 {s.sortie_id} 的教员岗",
                    )
                )
            if c.role in TRAINEE_ROLES and person.identity == IDENTITY_INSTRUCTOR:
                v.append(
                    _v(
                        "C04",
                        [s.sortie_id, c.person_id],
                        f"S-09：教员 {person.name}({c.person_id}) 不作为受训人排课目，"
                        f"却在 {s.sortie_id} 上占「{c.role}」岗",
                    )
                )

    # 同一人同刻只能在一个架次上：区间两两相交（半开 [takeoff, landing)）
    by_person: dict[str, list[Sortie]] = {}
    for s in _ordered(plan.sorties):
        for c in s.crew:
            by_person.setdefault(c.person_id, []).append(s)
    for person_id in sorted(by_person):
        sorties = by_person[person_id]
        for i, a in enumerate(sorties):
            for b in sorties[i + 1 :]:
                checked += 1
                if a.date != b.date:
                    continue
                if minutes_of(a.takeoff) < minutes_of(b.landing) and minutes_of(
                    b.takeoff
                ) < minutes_of(a.landing):
                    name = ctx.persons[person_id].name if person_id in ctx.persons else person_id
                    v.append(
                        _v(
                            "C04",
                            [person_id, a.sortie_id, b.sortie_id],
                            f"{name}({person_id}) 在 {a.date} 的 {a.sortie_id}"
                            f"（{a.takeoff}~{a.landing}）与 {b.sortie_id}"
                            f"（{b.takeoff}~{b.landing}）时间重叠",
                        )
                    )
    return _finish("C04", started, checked, v)


# ─────────────────────────────────────────────────────────────────────
# 约束5 · 机型与机组编成
# ─────────────────────────────────────────────────────────────────────
def check_c05(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """机型资质、机组人数、座位数 —— 期望人数按 §3.1.1 判定式重算再比对。"""
    started = perf_counter()
    v: list[Violation] = []
    checked = 0

    for s in _ordered(plan.sorties):
        checked += 1
        aircraft = ctx.aircraft.get(s.aircraft_id)
        if aircraft is None:
            v.append(
                _v(
                    "C05",
                    [s.sortie_id, s.aircraft_id],
                    f"机号 {s.aircraft_id} 不在册，无法核对机型资质",
                )
            )
        else:
            for c in s.crew:
                checked += 1
                person = ctx.persons.get(c.person_id)
                if person is None:
                    continue
                if aircraft.aircraft_type not in person.aircraft_types:
                    v.append(
                        _v(
                            "C05",
                            [s.sortie_id, c.person_id, s.aircraft_id],
                            f"{person.name}({c.person_id}) 不持 {aircraft.aircraft_type} 机型资质，"
                            f"不得执行 {s.sortie_id}（{s.aircraft_id}）",
                        )
                    )
            if len(s.crew) > aircraft.seats:
                v.append(
                    _v(
                        "C05",
                        [s.sortie_id, s.aircraft_id],
                        f"{s.sortie_id} 机组 {len(s.crew)} 人超过 {s.aircraft_id} 座位数 {aircraft.seats}",
                    )
                )

        mission = ctx.missions.get(s.mission_id)
        trainee = trainee_of(s)
        if mission is None or trainee is None:
            continue
        person = ctx.persons.get(trainee.person_id)
        if person is None:
            continue
        want = expected_crew_size(
            dual_required=mission.dual_required, trainee_identity=person.identity
        )
        if want != len(s.crew):
            v.append(
                _v(
                    "C05",
                    [s.sortie_id, trainee.person_id],
                    f"{s.sortie_id} 机组编成应为 {want} 人（{s.mission_id} 带飞="
                    f"{'是' if mission.dual_required else '否'}，受训人身份 {person.identity}），"
                    f"实际 {len(s.crew)} 人",
                    "A 类（带飞=否）由学员单飞，不带教员" if want == 1 else "该课目须由教员带飞",
                )
            )
    return _finish("C05", started, checked, v)


# ─────────────────────────────────────────────────────────────────────
# 约束6 · 资源有效性与容量（S-10 空域并发，扫描线）
# ─────────────────────────────────────────────────────────────────────
def check_c06(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """两部分：① 机号/机型有效性 ② 空域并发容量（扫描线）。

    ⚠️ **同刻口径「先减后加」**：一个架次 06:35 着陆、另一个 06:35 起飞，**不算
    并发**。事件排序键为 `(时刻, delta)`，`-1 < +1` 保证着陆先于起飞被处理 ——
    与求解侧的半开区间 `[start, start+dur)` 等价。两边不一致会在容量=1 的空域
    （IFR/RT1/RT2/RNG）上直接产出 FTS-3003。
    """
    started = perf_counter()
    v: list[Violation] = []
    checked = 0

    # ① 有效性
    for s in _ordered(plan.sorties):
        checked += 1
        aircraft = ctx.aircraft.get(s.aircraft_id)
        mission = ctx.missions.get(s.mission_id)
        if aircraft is None:
            v.append(_v("C06", [s.sortie_id, s.aircraft_id], f"机号 {s.aircraft_id} 不在册"))
        elif mission is not None:
            if aircraft.aircraft_type not in mission.aircraft_types:
                v.append(
                    _v(
                        "C06",
                        [s.sortie_id, s.aircraft_id, s.mission_id],
                        f"{s.aircraft_id}（{aircraft.aircraft_type}）机型不适配 {s.mission_id}"
                        f"（要求 {'/'.join(sorted(mission.aircraft_types))}）",
                        "改派符合机型要求的飞机",
                    )
                )
            if aircraft.capable_missions and s.mission_id not in aircraft.capable_missions:
                v.append(
                    _v(
                        "C06",
                        [s.sortie_id, s.aircraft_id, s.mission_id],
                        f"{s.aircraft_id} 的适配课目列表不含 {s.mission_id}",
                    )
                )
        if mission is None:
            v.append(_v("C06", [s.sortie_id, s.mission_id], f"课目 {s.mission_id} 不在册"))
        elif s.airspace_id != mission.airspace_id:
            v.append(
                _v(
                    "C06",
                    [s.sortie_id, s.airspace_id, s.mission_id],
                    f"{s.sortie_id} 标注空域 {s.airspace_id}，而 {s.mission_id} 绑定空域为 "
                    f"{mission.airspace_id}",
                )
            )

    # ② 空域并发（扫描线）—— 空域以课目表为准，容量一律从 airspaces 表读，不写死
    keyed: list[tuple[str, date, Sortie]] = []
    for s in _ordered(plan.sorties):
        aid = ctx.airspace_of(s.mission_id) or s.airspace_id
        keyed.append((aid, s.date, s))
    for (aid, day), group in group_by(keyed, lambda x: (x[0], x[1])).items():
        airspace = ctx.airspaces.get(aid)
        if airspace is None:
            v.append(_v("C06", [aid], f"空域 {aid} 不在册，无法核对同时段容量"))
            continue
        cap = airspace.capacity
        events: list[tuple[int, int, int, Sortie]] = []
        for seq, (_, _, s) in enumerate(group):
            events.append((minutes_of(s.takeoff), +1, seq, s))
            events.append((minutes_of(s.landing), -1, seq, s))
        # 排序键 (时刻, delta)：delta=-1 排在 +1 之前 → 同刻先减后加
        events.sort(key=lambda e: (e[0], e[1], e[2]))
        current = 0
        reported = False
        for t, delta, _, s in events:
            current += delta
            checked += 1
            if current > cap and not reported:
                reported = True  # 每个 (空域, 日) 只报一次峰值，避免刷屏
                v.append(
                    _v(
                        "C06",
                        [aid, str(day), s.sortie_id],
                        f"空域 {aid}（{airspace.name}）{day} 在 {fmt_minutes(t)} 并发 "
                        f"{current} 架 > 同时段容量 {cap}",
                        "错开其中一个架次的时段，或改排到其他空域的课目",
                    )
                )
    return _finish("C06", started, checked, v)


# ─────────────────────────────────────────────────────────────────────
# 约束7 · 飞机排期冲突与周转时间（S-06）
# ─────────────────────────────────────────────────────────────────────
def check_c07(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """按机分组排序，逐对检查 `gap = takeoff[b] − landing[a] ≥ 周转`；维护窗重叠。

    周转时间取 `aircraft.turnaround_minutes`（逐机一列，JL-8=30 / JL-9=40），
    **不从 YAML 的机型映射读** —— 用户换一批飞机时变的是数据不是规则。
    """
    started = perf_counter()
    v: list[Violation] = []
    checked = 0

    for ac_id, sorties in group_by(_ordered(plan.sorties), lambda s: s.aircraft_id).items():
        aircraft = ctx.aircraft.get(ac_id)
        if aircraft is None:
            continue  # 机号不在册由 C06 报
        turn = aircraft.turnaround_minutes
        ss = sorted(sorties, key=lambda s: (s.date, s.takeoff, s.sortie_id))
        for a, b in pairwise(ss):
            checked += 1
            if a.date != b.date:
                continue
            gap = minutes_of(b.takeoff) - minutes_of(a.landing)
            if gap < turn:
                v.append(
                    _v(
                        "C07",
                        [ac_id, a.sortie_id, b.sortie_id],
                        f"{ac_id}（{aircraft.aircraft_type}）{a.date} 相邻架次间隔 {gap} 分钟 "
                        f"< 周转要求 {turn} 分钟（{a.sortie_id} 着陆 {a.landing} → "
                        f"{b.sortie_id} 起飞 {b.takeoff}）",
                        f"将 {b.sortie_id} 起飞推迟至 {fmt_minutes(minutes_of(a.landing) + turn)}",
                    )
                )
        for mw in aircraft.maintenance:
            for s in ss:
                checked += 1
                if mw.overlaps(s.date, s.takeoff, s.landing):
                    v.append(
                        _v(
                            "C07",
                            [ac_id, s.sortie_id],
                            f"{ac_id} 的 {s.sortie_id}（{s.date} {s.takeoff}~{s.landing}）落入"
                            f"维护时段 {mw.start}~{mw.end}（{mw.kind}）",
                            "改派其他飞机或移出维护窗",
                        )
                    )
    return _finish("C07", started, checked, v)


# ─────────────────────────────────────────────────────────────────────
# 约束8 · 人员冲突与休息（S-07 仅同日内累计）
# ─────────────────────────────────────────────────────────────────────
def check_c08(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """按 (人, 日) 排序，相邻间隔 ≥10；第 2→第 3 架次间隔 ≥30。**跨日不累计**。"""
    started = perf_counter()
    v: list[Violation] = []
    checked = 0
    min_gap = ctx.ruleset.min_gap_minutes
    rest_after = ctx.ruleset.rest_after_n
    rest_min = ctx.ruleset.rest_minutes

    per_person_day: dict[tuple[str, date], list[Sortie]] = {}
    for s in _ordered(plan.sorties):
        for c in s.crew:
            per_person_day.setdefault((c.person_id, s.date), []).append(s)

    # (人, 日) 分组本身也计入检查项：全周没人一天飞两次时，「相邻对」为 0，
    # `checked_items=0` 会被前端当成「假通过」——而这里确实逐组查过了
    checked += len(per_person_day)
    for person_id, day in sorted(per_person_day, key=lambda k: (k[0], k[1])):
        ss = sorted(per_person_day[(person_id, day)], key=lambda s: (s.takeoff, s.sortie_id))
        name = ctx.persons[person_id].name if person_id in ctx.persons else person_id
        for idx, (a, b) in enumerate(pairwise(ss)):
            checked += 1
            gap = minutes_of(b.takeoff) - minutes_of(a.landing)
            if gap < min_gap:
                v.append(
                    _v(
                        "C08",
                        [person_id, a.sortie_id, b.sortie_id],
                        f"{name}({person_id}) {day} 相邻架次间隔 {gap} 分钟 < {min_gap} 分钟",
                        f"将 {b.sortie_id} 起飞推迟至 {fmt_minutes(minutes_of(a.landing) + min_gap)}",
                    )
                )
            # 连飞 rest_after 次之后需休息 rest_min：idx+1 是 b 在当日的序号（1 基）
            if idx + 1 >= rest_after and gap < rest_min:
                v.append(
                    _v(
                        "C08",
                        [person_id, a.sortie_id, b.sortie_id],
                        f"{name}({person_id}) {day} 连飞 {rest_after} 架次后需休息 {rest_min} 分钟，"
                        f"第 {idx + 2} 架次 {b.sortie_id} 仅间隔 {gap} 分钟",
                        f"将 {b.sortie_id} 起飞推迟至 {fmt_minutes(minutes_of(a.landing) + rest_min)}",
                    )
                )
    return _finish("C08", started, checked, v)


# ─────────────────────────────────────────────────────────────────────
# 约束9 · 起降密度（S-04 + S-05 + D-2）
# ─────────────────────────────────────────────────────────────────────
def check_c09(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """20 分钟窗口按 **(日, 跑道)** 分组；7 分钟间隔 **全场按日**（D-2）。

    ⚠️ 两段循环刻意分开写，**不要合并成一个按跑道的循环** —— `rules.pdf` 约束9
    原文只对前半句限定了「同一跑道」，把 7 分钟也按跑道分组是 v5.2 时期的错误
    理解，会放过「两条跑道 3 分钟内各起飞一次」这种真实冲突。
    """
    started = perf_counter()
    v: list[Violation] = []
    checked = 0
    window = ctx.ruleset.density_window_minutes
    cap = ctx.ruleset.density_window_cap
    separation = ctx.ruleset.separation_minutes

    ordered = _ordered(plan.sorties)

    # ⓪ 跑道有效性：JL-9 架次出现在只服务 JL-8 的跑道上即违规
    for s in ordered:
        checked += 1
        runway = ctx.runways.get(s.runway_id)
        aircraft = ctx.aircraft.get(s.aircraft_id)
        if runway is None:
            v.append(_v("C09", [s.sortie_id, s.runway_id], f"跑道 {s.runway_id} 不在册"))
        elif aircraft is not None and aircraft.aircraft_type not in runway.aircraft_types:
            v.append(
                _v(
                    "C09",
                    [s.sortie_id, s.runway_id, s.aircraft_id],
                    f"{s.sortie_id}：{s.aircraft_id}（{aircraft.aircraft_type}）不得使用跑道 "
                    f"{s.runway_id}（服务机型 {'/'.join(sorted(runway.aircraft_types))}）",
                    f"改用 {'/'.join(sorted(ctx.runways_for_type(aircraft.aircraft_type))) or '（无可用跑道）'}",
                )
            )

    # ① 20 分钟窗口，按 (日, 跑道) 分组，半开 [t, t+20)
    for (day, rwy), group in group_by(ordered, lambda s: (s.date, s.runway_id)).items():
        ts = sorted(minutes_of(s.takeoff) for s in group)
        for t in ts:
            n = sum(1 for u in ts if t <= u < t + window)  # S-04：半开
            checked += 1
            if n > cap:
                v.append(
                    _v(
                        "C09",
                        [str(day), rwy],
                        f"{day} 跑道 {rwy} 在 [{fmt_minutes(t)}, +{window}) 内起飞 {n} 次 > {cap} 次",
                        "把其中一个架次挪到另一条可用跑道或错开 20 分钟",
                    )
                )

    # ② 7 分钟间隔，**全场**按日（D-2：不分跑道）
    for day, group in group_by(ordered, lambda s: s.date).items():
        marked = sorted((minutes_of(s.takeoff), s.runway_id, s.sortie_id) for s in group)
        for a, b in pairwise(marked):
            checked += 1
            if b[0] - a[0] < separation:
                v.append(
                    _v(
                        "C09",
                        [str(day), a[2], b[2]],
                        f"{day} 相邻起飞间隔 {b[0] - a[0]} 分钟 < {separation} 分钟（**全场口径**，"
                        f"{a[2]}@{a[1]} {fmt_minutes(a[0])} → {b[2]}@{b[1]} {fmt_minutes(b[0])}）",
                        f"将 {b[2]} 起飞推迟至 {fmt_minutes(a[0] + separation)}",
                    )
                )
    return _finish("C09", started, checked, v)


# ─────────────────────────────────────────────────────────────────────
# 约束10/11/12 · 上限
# ─────────────────────────────────────────────────────────────────────
def _cap_severity(rule_id: int, plan: SchedulePlan, ctx: ValidationContext) -> str:
    return "SOFT" if rule_id in relaxed_rule_ids(plan, ctx.ruleset) else "HARD"


def check_c10(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """按 (人, 日) 求和飞行时长 ≤ 上限（学员 240 / 其余 480）。"""
    started = perf_counter()
    v: list[Violation] = []
    severity = _cap_severity(10, plan, ctx)
    totals: dict[tuple[str, date], int] = {}
    for s in _ordered(plan.sorties):
        dur = minutes_of(s.landing) - minutes_of(s.takeoff)
        for c in s.crew:
            key = (c.person_id, s.date)
            totals[key] = totals.get(key, 0) + dur

    for person_id, day in sorted(totals, key=lambda k: (k[0], k[1])):
        person = ctx.persons.get(person_id)
        if person is None:
            continue
        cap = ctx.ruleset.daily_minute_cap(person.identity)
        total = totals[(person_id, day)]
        if total > cap:
            v.append(
                _v(
                    "C10",
                    [person_id, str(day)],
                    f"{person.name}({person_id}) {day} 飞行时长合计 {total} 分钟 > 上限 {cap} 分钟"
                    f"（身份 {person.identity}）",
                    "把当日部分架次移到其他日期",
                    severity=severity,
                )
            )
    return _finish("C10", started, len(totals), v)


def check_c11(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """按人计数周架次 ≤ 上限（学员 10 / 其余 12）。"""
    started = perf_counter()
    v: list[Violation] = []
    severity = _cap_severity(11, plan, ctx)
    counts: dict[str, int] = {}
    for s in _ordered(plan.sorties):
        for c in s.crew:
            counts[c.person_id] = counts.get(c.person_id, 0) + 1

    for person_id in sorted(counts):
        person = ctx.persons.get(person_id)
        if person is None:
            continue
        cap = ctx.ruleset.weekly_sortie_cap(person.identity)
        if counts[person_id] > cap:
            v.append(
                _v(
                    "C11",
                    [person_id],
                    f"{person.name}({person_id}) 本周 {counts[person_id]} 架次 > 上限 {cap} 架次"
                    f"（身份 {person.identity}）",
                    severity=severity,
                )
            )
    return _finish("C11", started, len(counts), v)


def check_c12(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """(人, 日) ≤3；(机, 日) ≤6。"""
    started = perf_counter()
    v: list[Violation] = []
    severity = _cap_severity(12, plan, ctx)
    per_person: dict[tuple[str, date], int] = {}
    per_aircraft: dict[tuple[str, date], int] = {}
    for s in _ordered(plan.sorties):
        pkey = (s.aircraft_id, s.date)
        per_aircraft[pkey] = per_aircraft.get(pkey, 0) + 1
        for c in s.crew:
            key = (c.person_id, s.date)
            per_person[key] = per_person.get(key, 0) + 1

    person_cap = ctx.ruleset.daily_sorties_per_person
    for person_id, day in sorted(per_person, key=lambda k: (k[0], k[1])):
        n = per_person[(person_id, day)]
        if n > person_cap:
            name = ctx.persons[person_id].name if person_id in ctx.persons else person_id
            v.append(
                _v(
                    "C12",
                    [person_id, str(day)],
                    f"{name}({person_id}) {day} 安排 {n} 架次 > 单人单日上限 {person_cap}",
                    severity=severity,
                )
            )
    aircraft_cap = ctx.ruleset.daily_sorties_per_aircraft
    for ac_id, day in sorted(per_aircraft, key=lambda k: (k[0], k[1])):
        n = per_aircraft[(ac_id, day)]
        if n > aircraft_cap:
            v.append(
                _v(
                    "C12",
                    [ac_id, str(day)],
                    f"{ac_id} {day} 安排 {n} 架次 > 单机单日上限 {aircraft_cap}",
                    severity=severity,
                )
            )
    return _finish("C12", started, len(per_person) + len(per_aircraft), v)


# ─────────────────────────────────────────────────────────────────────
# 约束13 · 任务完成度（频率滑窗 + 跨周锚点 + 先修）
# ─────────────────────────────────────────────────────────────────────
def _c13_window_params(
    progress_is_recurrent: bool,
    freq_days: int,
    recurrent_days: int,
    recurrent_since: date | None,
    week_start: date,
) -> tuple[int, int]:
    """返回 `(F, origin_day)`。

    - 常规 (学员, 未完成课目)：`F = mission.freq_days`，窗口自周一（day 0）起算
    - S-11 复训：`F = 7`（`recurrent_window_days`），窗口自**到期次日**起算
    """
    if progress_is_recurrent:
        origin = 0 if recurrent_since is None else (recurrent_since - week_start).days
        return recurrent_days, origin
    return freq_days, 0


def _c13_deadline(origin_day: int, freq: int, last_done: date | None, week_start: date) -> int:
    """首次执行截止日（周内偏移）。

    - `last_done_date is None` → **S-12**：窗口从起算日算起，`deadline = origin + F − 1`，
      **不计欠账**（写成 `gap=999` 是 CLAUDE.md §11 的反模式）
    - 有锚点 → **D-4 通式**：`deadline = max(0, F − gap)`，其中
      `gap = (week_monday − last_done).days`；等价于「`last_done + F` 那一天」
    """
    if last_done is None:
        return origin_day + freq - 1
    gap = (week_start - last_done).days
    return max(origin_day, freq - gap)


def check_c13(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """独立重算频率滑窗（从 PG 读 `last_done_date` 与完成状态），并断言先修。

    管辖范围（v6 §3.2 约束13 + §3.5）：
    - **学员** × 未完成且先修满足的课目 → 各自 `freq_days` 滑窗，**逐 (人, 课目) 判**
    - **成熟飞行员**的到期资质（S-11，`is_recurrent`）→ 7 天滑窗，自到期次日起算，
      **且不受 S-03「已完成课目不复训」的豁免**；**粒度是 (人, 类别)**，见下
    - 教员不排课目（S-09）；已完成课目（S-03）不受本条约束
    - 先修未满足 → 断言「该课目在方案中出现次数 = 0」

    ## ⚠️ S-11 的粒度是**类别**，不是课目（业务方 2026-08-12 裁定）

    M2-C 的交叉验证跑出 **FTS-3003**：一个到期类别里有多门课目时（基准周的刘斌
    C 类 = missionC-1 + missionC-2），求解器按「该类 ≥1 次」下要求，本函数原先按
    「每门课各自 7 天滑窗」判，于是求解器排了 C-2、校验器判 C-1 缺失。

    根因是 v6 自相矛盾：§3.2 约束13 行写「S-11……同样受本条约束」（约束13 的粒度
    是 person×mission），而 §12.3 的 S-11 专项断言写的是「≥1 次刘斌的 C-1 **或**
    C-2」。业务方裁定取**类别**粒度 —— 与 S-02（A 类整体 ≥1 次）同构，语义都是
    「保持熟练度」而不是「推进进度」。

    落点就是下面那个 `recurrent_groups`：`is_recurrent` 的行按 (人, 类别) 归组，
    **整类合并计数**，飞该类里任意一门都算完成本次复训。窗口起算日取组内最早的
    `recurrent_since`，跨周锚点取组内最晚的 `last_done_date`。
    """
    started = perf_counter()
    v: list[Violation] = []
    checked = 0
    soft = 13 in relaxed_rule_ids(plan, ctx.ruleset)
    s11_identities = set(ctx.semantics.s11_identities)
    s11_on = ctx.semantics.s11_enabled
    recurrent_days = ctx.ruleset.recurrent_window_days

    #: S-11 复训按 (人, 类别) 归组，循环之后统一判（见函数文档）
    recurrent_groups: dict[tuple[str, str], list[ProgressFacts]] = {}

    # 受训人视角的排班日（教员带飞不算「他自己的课目进度」）
    scheduled: dict[tuple[str, str], list[int]] = {}
    for s in _ordered(plan.sorties):
        for c in s.crew:
            if c.role in TRAINEE_ROLES:
                scheduled.setdefault((c.person_id, s.mission_id), []).append(ctx.day_offset(s.date))

    for key in sorted(ctx.progress):
        pr = ctx.progress[key]
        person = ctx.persons.get(pr.person_id)
        mission = ctx.missions.get(pr.mission_id)
        if person is None or mission is None:
            continue
        checked += 1
        days = sorted(scheduled.get(key, []))

        # 先修未满足 → 一次都不许出现
        if not pr.prereq_met:
            if days:
                v.append(
                    _v(
                        "C13",
                        [pr.person_id, pr.mission_id],
                        f"{person.name}({pr.person_id}) 的 {pr.mission_id} 先修未满足"
                        f"（{pr.blocked_reason or '缺失先修'}），却被安排了 {len(days)} 次",
                        "先完成其先修课目，或把这些架次撤下",
                    )
                )
            continue

        recurrent = bool(pr.is_recurrent) and s11_on and person.identity in s11_identities
        if recurrent:
            # ★ S-11 的粒度是**类别**，不是课目（业务方 2026-08-12 裁定，见函数文档）。
            # 先按 (人, 类别) 归组，循环之后统一判一次。
            group = recurrent_groups.setdefault((pr.person_id, mission.mission_class), [])
            group.append(pr)
            continue
        if pr.completed:
            continue  # S-03：已完成课目不受本条约束
        if not person.is_student:
            continue  # S-09：教员/成熟飞行员不按频率排未完成课目
        freq, origin_day = _c13_window_params(
            recurrent, mission.freq_days, recurrent_days, pr.recurrent_since, ctx.week_start
        )
        start_day = max(0, origin_day)
        if start_day > WEEK_DAYS - 1:
            continue  # 窗口整体落在本周之后
        deadline = _c13_deadline(origin_day, freq, pr.last_done_date, ctx.week_start)
        windows = [(s, s + freq) for s in range(start_day, WEEK_DAYS - freq + 1)]
        deadline_binds = deadline <= WEEK_DAYS - 1
        checked += len(windows)
        severity = "SOFT" if soft and _disclosed_debt(plan, pr.person_id, pr.mission_id) else "HARD"
        anchor = (
            f"锚点 {pr.last_done_date}" if pr.last_done_date else "锚点缺失（S-12：自本周周一起算）"
        )

        if not days:
            if deadline_binds or windows:
                v.append(
                    _v(
                        "C13",
                        [pr.person_id, pr.mission_id],
                        f"{person.name}({pr.person_id}) 的 {pr.mission_id}"
                        f"（每 {freq} 天 ≥1 次，{anchor}）本周一次都未安排",
                        f"在第 0~{min(deadline, WEEK_DAYS - 1)} 天内排 1 次",
                        severity=severity,
                    )
                )
            continue

        if deadline_binds and days[0] > deadline:
            v.append(
                _v(
                    "C13",
                    [pr.person_id, pr.mission_id],
                    f"{person.name}({pr.person_id}) 的 {pr.mission_id} 首次执行在第 {days[0]} 天，"
                    f"晚于截止日第 {deadline} 天（每 {freq} 天 ≥1 次，{anchor}）",
                    f"把首次执行提前到第 {deadline} 天之前",
                    severity=severity,
                )
            )
        for lo, hi in windows:
            if not any(lo <= d < hi for d in days):
                v.append(
                    _v(
                        "C13",
                        [pr.person_id, pr.mission_id],
                        f"{person.name}({pr.person_id}) 的 {pr.mission_id} 在窗口 "
                        f"[第{lo}天, 第{hi - 1}天] 内没有安排（每 {freq} 天 ≥1 次）",
                        f"在第 {lo}~{hi - 1} 天之间补 1 次",
                        severity=severity,
                    )
                )

    # ── S-11 复训：**按 (人, 类别) 判一次**（业务方 2026-08-12 裁定）────────
    for (person_id, mission_class), rows in sorted(recurrent_groups.items()):
        person = ctx.persons[person_id]
        mission_ids = sorted(r.mission_id for r in rows)
        since = min((r.recurrent_since for r in rows if r.recurrent_since), default=None)
        anchors = [r.last_done_date for r in rows if r.last_done_date]
        last_done = max(anchors) if anchors else None
        freq, origin_day = _c13_window_params(
            True, recurrent_days, recurrent_days, since, ctx.week_start
        )
        start_day = max(0, origin_day)
        if start_day > WEEK_DAYS - 1:
            continue  # 复训周期本周还没开始
        deadline = _c13_deadline(origin_day, freq, last_done, ctx.week_start)
        windows = [(s, s + freq) for s in range(start_day, WEEK_DAYS - freq + 1)]
        deadline_binds = deadline <= WEEK_DAYS - 1
        checked += 1 + len(windows)
        severity = (
            "SOFT"
            if soft and any(_disclosed_debt(plan, person_id, mid) for mid in mission_ids)
            else "HARD"
        )
        anchor = f"锚点 {last_done}" if last_done else "锚点缺失（S-12：自本周周一起算）"
        # **整类合并计数**：飞该类里任意一门都算完成本次复训
        days = sorted({d for mid in mission_ids for d in scheduled.get((person_id, mid), [])})
        scope = f"{mission_class} 类复训（{'、'.join(mission_ids)} 任一即可）"

        if not days:
            if deadline_binds or windows:
                v.append(
                    _v(
                        "C13",
                        [person_id, f"{mission_class}类", *mission_ids],
                        f"{person.name}({person_id}) 的 {scope}"
                        f"（每 {freq} 天 ≥1 次，{anchor}）本周一次都未安排",
                        f"在第 {start_day}~{min(deadline, WEEK_DAYS - 1)} 天内排 1 次该类课目",
                        severity=severity,
                    )
                )
            continue
        if deadline_binds and days[0] > deadline:
            v.append(
                _v(
                    "C13",
                    [person_id, f"{mission_class}类", *mission_ids],
                    f"{person.name}({person_id}) 的 {scope} 首次执行在第 {days[0]} 天，"
                    f"晚于截止日第 {deadline} 天（每 {freq} 天 ≥1 次，{anchor}）",
                    f"把首次执行提前到第 {deadline} 天之前",
                    severity=severity,
                )
            )
        for lo, hi in windows:
            if not any(lo <= d < hi for d in days):
                v.append(
                    _v(
                        "C13",
                        [person_id, f"{mission_class}类", *mission_ids],
                        f"{person.name}({person_id}) 的 {scope} 在窗口 "
                        f"[第{lo}天, 第{hi - 1}天] 内没有安排（每 {freq} 天 ≥1 次）",
                        f"在第 {lo}~{hi - 1} 天之间补 1 次该类课目",
                        severity=severity,
                    )
                )
    return _finish("C13", started, checked, v)


# ─────────────────────────────────────────────────────────────────────
# 约束14 · 任务唯一性（req_max = ceil(7 / freq_days)）
# ─────────────────────────────────────────────────────────────────────
def check_c14(plan: SchedulePlan, ctx: ValidationContext) -> CheckResult:
    """完全重复架次检测 + 每 (人, 课目) 计数上界检测。

    计数只算**受训人**（角色 ∈ {学员, 单飞, 复训}）：约束14 约束的是「同一人员与
    同一课目子任务的组合」这项训练安排，而教员一周内带同一门课目多次是正常的
    （否则一名教员每周只能带一次 missionC-1，与约束3 的编成要求直接打架）。
    """
    started = perf_counter()
    v: list[Violation] = []
    ordered = _ordered(plan.sorties)
    checked = len(ordered)

    # ① 完全重复（除架次号外逐字段相同）
    seen: dict[tuple[object, ...], str] = {}
    for s in ordered:
        fingerprint: tuple[object, ...] = (
            s.date,
            s.takeoff,
            s.landing,
            s.mission_id,
            s.aircraft_id,
            s.runway_id,
            s.airspace_id,
            s.is_recurrent,
            tuple(sorted((c.person_id, c.role) for c in s.crew)),
        )
        if fingerprint in seen:
            v.append(
                _v(
                    "C14",
                    [seen[fingerprint], s.sortie_id],
                    f"{s.sortie_id} 与 {seen[fingerprint]} 是完全重复的架次记录"
                    f"（{s.date} {s.takeoff} {s.mission_id} {s.aircraft_id}）",
                    f"删除 {s.sortie_id}",
                )
            )
        else:
            seen[fingerprint] = s.sortie_id

    # ② 每 (人, 课目) 计数 ≤ req_max，req_max 独立重算
    counts: dict[tuple[str, str], int] = {}
    for s in ordered:
        for c in s.crew:
            if c.role in TRAINEE_ROLES:
                counts[(c.person_id, s.mission_id)] = counts.get((c.person_id, s.mission_id), 0) + 1
    checked += len(counts)
    for person_id, mission_id in sorted(counts):
        mission = ctx.missions.get(mission_id)
        if mission is None:
            continue
        req_max = req_max_for(mission.freq_days)
        n = counts[(person_id, mission_id)]
        if n > req_max:
            name = ctx.persons[person_id].name if person_id in ctx.persons else person_id
            v.append(
                _v(
                    "C14",
                    [person_id, mission_id],
                    f"{name}({person_id}) 本周安排 {mission_id} 共 {n} 次 > req_max "
                    f"{req_max} = ceil(7 / freq_days={mission.freq_days})",
                    "撤下多余的重复安排",
                )
            )
    return _finish("C14", started, checked, v)


# ─────────────────────────────────────────────────────────────────────
# 汇总
# ─────────────────────────────────────────────────────────────────────
CheckFn = Callable[[SchedulePlan, ValidationContext], CheckResult]

ALL_CHECKS: tuple[CheckFn, ...] = (
    check_c01,
    check_c02,
    check_c03,
    check_c04,
    check_c05,
    check_c06,
    check_c07,
    check_c08,
    check_c09,
    check_c10,
    check_c11,
    check_c12,
    check_c13,
    check_c14,
)


def run_all_checks(plan: SchedulePlan, ctx: ValidationContext) -> ValidationReport:
    """闸门1：逐条跑完 14 条规则，返回带耗时与检查项数的报告。

    **不短路**：即使第一条就失败也把 14 条全跑完 —— 排班员需要一次看到全部问题，
    而 `missing_rules()` 非空即说明校验没跑全、不能宣称 100% 合规。
    """
    started = perf_counter()
    results = [fn(plan, ctx) for fn in ALL_CHECKS]
    return ValidationReport(
        plan_id=plan.plan_id,
        ruleset_version=ctx.ruleset.version,
        semantics_version=ctx.semantics.version,
        results=results,
        duration_ms=(perf_counter() - started) * 1000.0,
    )


__all__ = [
    "ALL_CHECKS",
    "RULE_TITLES",
    "S11_AUTHORIZED_REWRITE_NOTE",
    "TRAINEE_ROLES",
    "WEEKDAY_LABELS",
    "CheckFn",
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
    "expected_crew_size",
    "fmt_minutes",
    "group_by",
    "minutes_of",
    "pairwise",
    "relaxed_rule_ids",
    "run_all_checks",
    "trainee_of",
]
