"""200 场景测试集的**程序化**构造（v6 §12.3）。

| 类别 | 数量 | 构造方式 |
|---|---|---|
| 基准周 | 1 | 2026-W02 原始数据，零扰动 |
| 单点扰动 | 60 | 1 人请假 / 1 机维修 / 1 资质到期 / 1 空域容量降为 0 / 1 跑道关闭 |
| 组合扰动 | 60 | 从单点池里按固定种子抽 2~4 个叠加 |
| 边界场景 | 40 | 20 组「单调旋钮」各出一对：**恰好够** 与 **恰好差 1** |
| 构造不可行 | 30 | I1~I5 五族 × 6 个**沿同一方向更紧**的变体 |
| 局部重排 | 9 | 3 种扰动 × 3 档冻结策略，叠加在已批准计划上 |

## 三条硬性口径

1. **实体编号一律从快照读**，本模块不出现 `P01` / `AC10` / `JL-8` / `SAA`
   这类字面量（CLAUDE.md §11、v6 §5.1.1）。换一批上传数据，同一份代码生成的是
   那批数据的 200 个场景。
2. **标签天然正确**：场景的「构造方式」就是它的标签，不需要人工标注。唯一需要
   人工标注的是不可行族的**真实冲突源**，那份标注直接抄 v6 §12.3 的
   「预期最小冲突集」列（见 :data:`INFEASIBLE_FAMILIES`），不是本窗口自己编的。
3. **可复现**：组合扰动用固定种子（:data:`COMBO_SEED`）的 `random.Random`，
   同一份实体表生成的 200 个场景逐字节一致。

## 「恰好」的判定（边界场景）

见 :mod:`tests.scenarios.calibrate` 的模块文档。一句话：**成对定义、互为证明**
—— 「恰好够」= 该场景可解且把同一个旋钮再紧一格就不可解；「恰好差 1」就是紧一格
的那个场景本身。一对里两个场景都在测试集里，于是「恰好」不需要额外求解去验证，
它由这一对的两个结果直接判定。
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, time, timedelta
from pathlib import Path

from backend.solver.data import ScenarioOverrides

#: 组合扰动的抽样种子（可复现）
COMBO_SEED: int = 20260812

#: 数据集版本目录
DATASET_VERSION: str = "v1"

WEEK_DAYS: int = 7


# ─────────────────────────────────────────────────────────────────────
# 实体（从快照读，不写死）
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Entities:
    """生成 200 场景所需的实体清单。**全部来自 `snapshot_id` 指向的那份上传数据。**"""

    snapshot_id: str
    week_start: date
    persons: tuple[tuple[str, str, str], ...]  # (person_id, name, identity)
    aircraft: tuple[tuple[str, str], ...]  # (aircraft_id, aircraft_type)
    airspaces: tuple[tuple[str, int], ...]  # (airspace_id, capacity)
    runways: tuple[tuple[str, tuple[str, ...]], ...]  # (runway_id, 服务机型)
    missions: tuple[tuple[str, str, int], ...]  # (mission_id, class, freq_days)
    qualifications: tuple[tuple[str, str], ...]  # (person_id, mission_class)
    student_types: tuple[str, ...]  # 学员持有的机型（I5 用）
    window: tuple[time, time] = (time(6, 0), time(18, 0))

    def days(self) -> tuple[date, ...]:
        return tuple(self.week_start + timedelta(days=i) for i in range(WEEK_DAYS))

    def identity_of(self, person_id: str) -> str:
        return next(identity for pid, _n, identity in self.persons if pid == person_id)

    def persons_of(self, identity: str) -> tuple[str, ...]:
        return tuple(pid for pid, _n, ident in self.persons if ident == identity)

    def aircraft_of_type(self, aircraft_type: str) -> tuple[str, ...]:
        return tuple(aid for aid, atype in self.aircraft if atype == aircraft_type)

    def runways_serving(self, aircraft_types: Sequence[str]) -> tuple[str, ...]:
        wanted = set(aircraft_types)
        return tuple(rid for rid, types in self.runways if wanted & set(types))

    @property
    def instructors(self) -> tuple[str, ...]:
        return self.persons_of("教员")

    @property
    def students(self) -> tuple[str, ...]:
        return self.persons_of("学员")


# ─────────────────────────────────────────────────────────────────────
# 可序列化的扰动
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OverrideSpec:
    """`ScenarioOverrides` 的 JSON 可序列化形态（数据集要落盘、要版本化）。"""

    window_start: str | None = None
    window_end: str | None = None
    airspace_capacity: Mapping[str, int] = field(default_factory=dict)
    closed_runways: tuple[str, ...] = ()
    unavailable: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    unavailable_all_week: tuple[str, ...] = ()
    maintenance_all_day: tuple[tuple[str, str, str], ...] = ()
    qual_expiry: Mapping[str, str] = field(default_factory=dict)  # "P04|C" → "2026-01-04"

    def to_overrides(self) -> ScenarioOverrides:
        return ScenarioOverrides(
            window_start=_parse_time(self.window_start),
            window_end=_parse_time(self.window_end),
            airspace_capacity=dict(self.airspace_capacity),
            closed_runways=frozenset(self.closed_runways),
            unavailable={
                pid: frozenset(date.fromisoformat(d) for d in days)
                for pid, days in self.unavailable.items()
            },
            unavailable_all_week=frozenset(self.unavailable_all_week),
            maintenance_all_day=tuple(
                (aid, date.fromisoformat(first), date.fromisoformat(last))
                for aid, first, last in self.maintenance_all_day
            ),
            qual_expiry={
                (key.split("|")[0], key.split("|")[1]): date.fromisoformat(value)
                for key, value in self.qual_expiry.items()
            },
        )

    def merge(self, other: OverrideSpec) -> OverrideSpec:
        """叠加两个扰动（组合扰动用）。冲突时取「更紧」的一侧。"""
        unavailable: dict[str, tuple[str, ...]] = {
            pid: tuple(days) for pid, days in self.unavailable.items()
        }
        for pid, days in other.unavailable.items():
            unavailable[pid] = tuple(sorted(set(unavailable.get(pid, ())) | set(days)))
        capacity = dict(self.airspace_capacity)
        for aid, cap in other.airspace_capacity.items():
            capacity[aid] = min(capacity.get(aid, cap), cap)
        return OverrideSpec(
            window_start=_tighter(self.window_start, other.window_start, later=True),
            window_end=_tighter(self.window_end, other.window_end, later=False),
            airspace_capacity=capacity,
            closed_runways=tuple(sorted(set(self.closed_runways) | set(other.closed_runways))),
            unavailable=unavailable,
            unavailable_all_week=tuple(
                sorted(set(self.unavailable_all_week) | set(other.unavailable_all_week))
            ),
            maintenance_all_day=tuple(
                sorted(set(self.maintenance_all_day) | set(other.maintenance_all_day))
            ),
            qual_expiry={**self.qual_expiry, **other.qual_expiry},
        )

    def is_empty(self) -> bool:
        return self.to_overrides().is_empty()

    def summary(self) -> str:
        bits: list[str] = []
        if self.window_start or self.window_end:
            bits.append(f"窗口 {self.window_start or '06:00'}-{self.window_end or '18:00'}")
        if self.airspace_capacity:
            bits.append(
                "空域容量 "
                + "/".join(f"{k}={v}" for k, v in sorted(self.airspace_capacity.items()))
            )
        if self.closed_runways:
            bits.append("关闭跑道 " + "/".join(self.closed_runways))
        if self.unavailable_all_week:
            bits.append("整周不可用 " + "/".join(self.unavailable_all_week))
        if self.unavailable:
            bits.append(
                "请假 " + "/".join(f"{k}×{len(v)}天" for k, v in sorted(self.unavailable.items()))
            )
        if self.maintenance_all_day:
            bits.append("维护 " + "/".join(a for a, _f, _l in self.maintenance_all_day))
        if self.qual_expiry:
            bits.append("到期 " + "/".join(f"{k}@{v}" for k, v in sorted(self.qual_expiry.items())))
        return "；".join(bits) or "无扰动"


def _parse_time(value: str | None) -> time | None:
    if value is None:
        return None
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _tighter(a: str | None, b: str | None, *, later: bool) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b) if later else min(a, b)


NO_OVERRIDE: OverrideSpec = OverrideSpec()


# ─────────────────────────────────────────────────────────────────────
# 场景
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ScenarioCase:
    """一个测试场景。**它的构造方式就是它的标签。**"""

    scenario_id: str
    category: str  # baseline / single / combo / boundary / infeasible / reschedule
    family: str
    title: str
    #: SOLVED（须出解）/ INFEASIBLE（须判不可行）/ EITHER（两者皆可，如实记录）
    expected_status: str
    overrides: OverrideSpec = NO_OVERRIDE
    #: 人工标注的真实冲突源（只有 infeasible 族有；抄 v6 §12.3「预期最小冲突集」列）
    annotated_conflict_rules: tuple[str, ...] = ()
    #: 边界对：同一个旋钮的相邻两格
    pair_id: str | None = None
    pair_role: str | None = None  # "enough" / "short"
    #: 局部重排：扰动描述 + 冻结档位
    reschedule: Mapping[str, object] | None = None
    #: 求解时限（I4/I5 按 v6 §12.3 用 300s）
    time_limit_s: float | None = None
    notes: str = ""

    def to_json(self) -> dict[str, object]:
        payload = asdict(self)
        payload["overrides"] = {k: v for k, v in asdict(self.overrides).items() if v}
        return payload


# ─────────────────────────────────────────────────────────────────────
# 单点扰动
# ─────────────────────────────────────────────────────────────────────
def absence_cases(ents: Entities, count: int) -> list[ScenarioCase]:
    """1 人请假。整周不可用 + 单日不可用两种形态轮着来。"""
    cases: list[ScenarioCase] = []
    days = ents.days()
    combos: list[tuple[str, tuple[str, ...] | None]] = []
    for pid, _name, _identity in ents.persons:
        combos.append((pid, None))  # 整周
    for i, (pid, _name, _identity) in enumerate(ents.persons):
        combos.append((pid, (days[i % WEEK_DAYS].isoformat(),)))  # 单日
    for i, (pid, _name, _identity) in enumerate(ents.persons):
        span = tuple(d.isoformat() for d in days[i % 4 : i % 4 + 3])
        combos.append((pid, span))  # 连续三天
    for idx in range(count):
        pid, when = combos[idx % len(combos)]
        if when is None:
            spec = OverrideSpec(unavailable_all_week=(pid,))
            what = "整周不可用"
        else:
            spec = OverrideSpec(unavailable={pid: when})
            what = f"{len(when)} 天不可用（{when[0]}起）"
        cases.append(
            ScenarioCase(
                scenario_id=f"SP-ABS-{idx + 1:02d}",
                category="single",
                family="absence",
                title=f"1 人请假：{pid}（{ents.identity_of(pid)}）{what}",
                expected_status="EITHER",
                overrides=spec,
                notes="v6 §12.3 单点扰动：1 人请假",
            )
        )
    return cases


def maintenance_cases(ents: Entities, count: int) -> list[ScenarioCase]:
    """1 机维修。整周维护 + 单日维护 + 连续三天。"""
    cases: list[ScenarioCase] = []
    days = ents.days()
    combos: list[tuple[str, str, str]] = []
    for aid, _atype in ents.aircraft:
        combos.append((aid, days[0].isoformat(), days[-1].isoformat()))
    for i, (aid, _atype) in enumerate(ents.aircraft):
        one = days[i % WEEK_DAYS]
        combos.append((aid, one.isoformat(), one.isoformat()))
    for i, (aid, _atype) in enumerate(ents.aircraft):
        first = days[i % 5]
        combos.append((aid, first.isoformat(), (first + timedelta(days=2)).isoformat()))
    for idx in range(count):
        aid, first, last = combos[idx % len(combos)]
        span = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
        cases.append(
            ScenarioCase(
                scenario_id=f"SP-MNT-{idx + 1:02d}",
                category="single",
                family="maintenance",
                title=f"1 机维修：{aid} 连续 {span} 天全天维护（{first} 起）",
                expected_status="EITHER",
                overrides=OverrideSpec(maintenance_all_day=((aid, first, last),)),
                notes="v6 §12.3 单点扰动：1 机维修",
            )
        )
    return cases


def expiry_cases(ents: Entities, count: int) -> list[ScenarioCase]:
    """1 资质到期。到期日落在周前 / 周内 / 周末三种位置。"""
    cases: list[ScenarioCase] = []
    offsets = (-1, 0, 2, 4, 6)
    combos = [
        (pid, cls, offset)
        for offset in offsets
        for pid, cls in ents.qualifications
        if ents.identity_of(pid) != "教员"  # 教员全类资质，到期没有业务含义
    ]
    for idx in range(count):
        pid, cls, offset = combos[idx % len(combos)]
        when = ents.week_start + timedelta(days=offset)
        cases.append(
            ScenarioCase(
                scenario_id=f"SP-EXP-{idx + 1:02d}",
                category="single",
                family="expiry",
                title=f"1 资质到期：{pid}（{ents.identity_of(pid)}）{cls} 类于 {when} 到期",
                expected_status="EITHER",
                overrides=OverrideSpec(qual_expiry={f"{pid}|{cls}": when.isoformat()}),
                notes="v6 §12.3 单点扰动：1 资质到期（成熟飞行员走 S-11 复训，学员按约束2 字面剔除）",
            )
        )
    return cases


def airspace_cases(ents: Entities, count: int) -> list[ScenarioCase]:
    """1 空域容量降为 0（含「降一格」的形态）。

    容量为 1 的空域「降一格」就是降为 0，与关闭同义；容量为 2 的（基准数据里的
    SAA/SAB）降一格是真正的新构造。为避免生成重复场景，这里按
    「每个空域 → 从基准容量逐格降到 0」展开，天然去重。
    """
    cases: list[ScenarioCase] = []
    combos: list[tuple[str, int]] = []
    for aid, capacity in ents.airspaces:
        for target in range(capacity - 1, -1, -1):
            combos.append((aid, target))
    for idx in range(count):
        aid, target = combos[idx % len(combos)]
        cases.append(
            ScenarioCase(
                scenario_id=f"SP-ASP-{idx + 1:02d}",
                category="single",
                family="airspace",
                title=f"1 空域容量变更：{aid} 容量降为 {target}"
                + ("（关闭）" if target == 0 else ""),
                expected_status="EITHER",
                overrides=OverrideSpec(airspace_capacity={aid: target}),
                notes="v6 §12.3 单点扰动：1 空域容量降为 0（v6 §3.4：关闭 = 容量降为 0）",
            )
        )
    return cases


def runway_cases(ents: Entities, count: int) -> list[ScenarioCase]:
    """1 跑道关闭。

    ⚠️ **单点构造的数量上限就是跑道条数。** 基准数据只有 2 条跑道，因此
    「1 跑道关闭」这个单点扰动只存在 2 个互不相同的构造；要凑到 12 个，要么
    重复、要么就不再是「单点」。数量由 `count` 决定，超出可用构造数时**不重复
    填充**，缺口由 :func:`single_point_cases` 分给其余四类（见其文档）。
    """
    cases: list[ScenarioCase] = []
    for idx, (rid, types) in enumerate(ents.runways[:count]):
        cases.append(
            ScenarioCase(
                scenario_id=f"SP-RWY-{idx + 1:02d}",
                category="single",
                family="runway",
                title=f"1 跑道关闭：{rid}（服务机型 {'/'.join(types)}）",
                expected_status="EITHER",
                overrides=OverrideSpec(closed_runways=(rid,)),
                notes="v6 §12.3 单点扰动：1 跑道关闭（v6 新增）",
            )
        )
    return cases


#: 单点扰动五类的默认配额。**跑道那一格是硬上限**（见 `runway_cases` 的说明），
#: 缺口按「人 / 机 / 资质 / 空域」的顺序补齐到 60。
DEFAULT_SINGLE_QUOTA: dict[str, int] = {
    "absence": 15,
    "maintenance": 15,
    "expiry": 14,
    "airspace": 14,
    "runway": 2,
}


def single_point_cases(
    ents: Entities, quota: Mapping[str, int] | None = None
) -> list[ScenarioCase]:
    """单点扰动 60 个。"""
    q = dict(quota or DEFAULT_SINGLE_QUOTA)
    cases = (
        absence_cases(ents, q["absence"])
        + maintenance_cases(ents, q["maintenance"])
        + expiry_cases(ents, q["expiry"])
        + airspace_cases(ents, q["airspace"])
        + runway_cases(ents, q["runway"])
    )
    return cases


# ─────────────────────────────────────────────────────────────────────
# 组合扰动
# ─────────────────────────────────────────────────────────────────────
def combo_cases(
    ents: Entities, pool: Sequence[ScenarioCase], count: int = 60
) -> list[ScenarioCase]:
    """2~4 个单点扰动叠加。固定种子 → 逐字节可复现。"""
    rng = random.Random(COMBO_SEED)
    cases: list[ScenarioCase] = []
    seen: set[str] = set()
    attempts = 0
    while len(cases) < count and attempts < count * 50:
        attempts += 1
        size = rng.choice((2, 2, 3, 3, 4))
        picked = rng.sample(list(pool), size)
        families = [c.family for c in picked]
        # 同族两条叠在一起往往退化成「更狠的单点」，跳过
        if len(set(families)) < min(size, 2):
            continue
        merged = NO_OVERRIDE
        for case in picked:
            merged = merged.merge(case.overrides)
        # 三名教员全部整周不可用属于 I1，不放进组合族
        if set(merged.unavailable_all_week) >= set(ents.instructors):
            continue
        key = json.dumps(asdict(merged), sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        idx = len(cases) + 1
        cases.append(
            ScenarioCase(
                scenario_id=f"CB-{idx:02d}",
                category="combo",
                family=f"combo{size}",
                title=f"组合扰动（{size} 个）：" + "；".join(c.title for c in picked),
                expected_status="EITHER",
                overrides=merged,
                notes="v6 §12.3 组合扰动：2~4 个异常叠加，来源见 title",
            )
        )
    return cases


# ─────────────────────────────────────────────────────────────────────
# 构造不可行 I1~I5
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class InfeasibleFamily:
    """一族不可行场景。`annotated` 直接抄 v6 §12.3 的「预期最小冲突集」列。"""

    family_id: str
    title: str
    annotated: tuple[str, ...]
    time_limit_s: float | None = None


#: v6 §12.3 的五族（**M2-A 实测后由业务方 2026-08-11 换过 I1/I4/I5 的构造，Z-2**）
INFEASIBLE_FAMILIES: tuple[InfeasibleFamily, ...] = (
    InfeasibleFamily("I1", "三名教员全部整周不可用", ("C13", "C03", "C04")),
    InfeasibleFamily("I2", "服务学员机型的飞机全部整周维护", ("C06", "C03")),
    InfeasibleFamily("I3", "承载 C 类课目的空域整周容量降为 0", ("C06", "C13")),
    InfeasibleFamily("I4", "训练窗压缩至 06:00-06:30", ("C01", "C13"), time_limit_s=300.0),
    InfeasibleFamily("I5", "服务学员机型的跑道全部关闭", ("C09", "C03", "C13"), time_limit_s=300.0),
)


def _i1_variants(ents: Entities) -> list[tuple[str, OverrideSpec]]:
    """I1：教员容量归零。**变体一律沿「让教员岗更不可得」的方向更紧。**"""
    base = OverrideSpec(unavailable_all_week=tuple(sorted(ents.instructors)))
    jl9 = [aid for aid, atype in ents.aircraft if atype not in ents.student_types]
    days = ents.days()
    return [
        ("三名教员全部整周不可用", base),
        (
            "＋成熟飞行员也整周不可用（可带飞的人一个不剩）",
            base.merge(
                OverrideSpec(unavailable_all_week=tuple(sorted(ents.persons_of("成熟飞行员"))))
            ),
        ),
        (
            "＋非学员机型的飞机整周维护",
            base.merge(
                OverrideSpec(
                    maintenance_all_day=tuple(
                        (aid, days[0].isoformat(), days[-1].isoformat()) for aid in sorted(jl9)
                    )
                )
            ),
        ),
        ("＋训练窗压到 06:00-09:00", base.merge(OverrideSpec(window_end="09:00"))),
        (
            "＋一条跑道关闭",
            base.merge(OverrideSpec(closed_runways=(ents.runways[0][0],))),
        ),
        (
            "＋全部空域容量降为 1",
            base.merge(
                OverrideSpec(airspace_capacity={aid: min(1, cap) for aid, cap in ents.airspaces})
            ),
        ),
    ]


def _i2_variants(ents: Entities) -> list[tuple[str, OverrideSpec]]:
    """I2：学员机型的飞机全部整周维护（v6 更正：6 架全封，3 架封不死 A 类）。"""
    days = ents.days()
    student_fleet = sorted(
        {aid for atype in ents.student_types for aid in ents.aircraft_of_type(atype)}
    )
    other_fleet = sorted({aid for aid, _t in ents.aircraft} - set(student_fleet))
    base = OverrideSpec(
        maintenance_all_day=tuple(
            (aid, days[0].isoformat(), days[-1].isoformat()) for aid in student_fleet
        )
    )
    return [
        (f"学员机型 {len(student_fleet)} 架全部整周维护", base),
        (
            "＋其余机型也整周维护（全场停飞）",
            base.merge(
                OverrideSpec(
                    maintenance_all_day=tuple(
                        (aid, days[0].isoformat(), days[-1].isoformat()) for aid in other_fleet
                    )
                )
            ),
        ),
        ("＋训练窗压到 06:00-12:00", base.merge(OverrideSpec(window_end="12:00"))),
        (
            "＋一条跑道关闭",
            base.merge(OverrideSpec(closed_runways=(ents.runways[0][0],))),
        ),
        (
            "＋一名教员整周不可用",
            base.merge(OverrideSpec(unavailable_all_week=(ents.instructors[0],))),
        ),
        (
            "＋全部空域容量降为 1",
            base.merge(
                OverrideSpec(airspace_capacity={aid: min(1, cap) for aid, cap in ents.airspaces})
            ),
        ),
    ]


def _i3_variants(ents: Entities, c_airspaces: Sequence[str]) -> list[tuple[str, OverrideSpec]]:
    """I3：承载 C 类课目的空域容量降为 0。变体沿「关掉更多空域」的方向更紧。"""
    base = OverrideSpec(airspace_capacity=dict.fromkeys(sorted(c_airspaces), 0))
    others = [aid for aid, _cap in ents.airspaces if aid not in set(c_airspaces)]
    out: list[tuple[str, OverrideSpec]] = [
        (f"{'/'.join(sorted(c_airspaces))} 整周容量降为 0", base)
    ]
    for i in range(1, 5):
        extra = others[:i]
        out.append(
            (
                f"＋再关 {'/'.join(extra)}",
                base.merge(OverrideSpec(airspace_capacity=dict.fromkeys(extra, 0))),
            )
        )
    out.append(
        (
            "＋全部空域整周容量降为 0",
            base.merge(OverrideSpec(airspace_capacity={aid: 0 for aid, _c in ents.airspaces})),
        )
    )
    return out[:6]


def _i4_variants(_ents: Entities) -> list[tuple[str, OverrideSpec]]:
    """I4：训练窗压缩。**严格单调**：06:30 → 06:05，每格 5 分钟。"""
    return [
        (f"训练窗压缩至 06:00-06:{minute:02d}", OverrideSpec(window_end=f"06:{minute:02d}"))
        for minute in (30, 25, 20, 15, 10, 5)
    ]


def _i5_variants(ents: Entities) -> list[tuple[str, OverrideSpec]]:
    """I5：服务学员机型的跑道全部关闭。变体沿「再抽走别的资源」的方向更紧。"""
    student_runways = sorted(ents.runways_serving(ents.student_types))
    base = OverrideSpec(closed_runways=tuple(student_runways))
    days = ents.days()
    return [
        (f"服务学员机型的跑道全部关闭（{'/'.join(student_runways)}）", base),
        (
            "＋其余跑道也关闭（全场无跑道）",
            base.merge(OverrideSpec(closed_runways=tuple(rid for rid, _t in ents.runways))),
        ),
        ("＋训练窗压到 06:00-09:00", base.merge(OverrideSpec(window_end="09:00"))),
        (
            "＋一名教员整周不可用",
            base.merge(OverrideSpec(unavailable_all_week=(ents.instructors[0],))),
        ),
        (
            "＋一架学员机型的飞机整周维护",
            base.merge(
                OverrideSpec(
                    maintenance_all_day=(
                        (
                            ents.aircraft_of_type(ents.student_types[0])[0],
                            days[0].isoformat(),
                            days[-1].isoformat(),
                        ),
                    )
                )
            ),
        ),
        (
            "＋全部空域容量降为 1",
            base.merge(
                OverrideSpec(airspace_capacity={aid: min(1, cap) for aid, cap in ents.airspaces})
            ),
        ),
    ]


def infeasible_cases(ents: Entities, c_airspaces: Sequence[str]) -> list[ScenarioCase]:
    """I1~I5 各 6 个变体，共 30 个。"""
    builders = {
        "I1": _i1_variants(ents),
        "I2": _i2_variants(ents),
        "I3": _i3_variants(ents, c_airspaces),
        "I4": _i4_variants(ents),
        "I5": _i5_variants(ents),
    }
    cases: list[ScenarioCase] = []
    for family in INFEASIBLE_FAMILIES:
        for i, (title, spec) in enumerate(builders[family.family_id], start=1):
            cases.append(
                ScenarioCase(
                    scenario_id=f"{family.family_id}-{i:02d}",
                    category="infeasible",
                    family=family.family_id,
                    title=f"{family.family_id} 变体{i}：{title}",
                    expected_status="INFEASIBLE",
                    overrides=spec,
                    annotated_conflict_rules=family.annotated,
                    time_limit_s=family.time_limit_s,
                    notes=(
                        f"v6 §12.3 {family.family_id}（{family.title}）。"
                        "变体沿「让同一条约束更紧」的方向构造，冲突源标注抄自 §12.3「预期最小冲突集」列。"
                    ),
                )
            )
    return cases


# ─────────────────────────────────────────────────────────────────────
# 局部重排
# ─────────────────────────────────────────────────────────────────────
FREEZE_POLICIES: tuple[str, ...] = ("CONSERVATIVE", "BALANCED", "AGGRESSIVE")


def reschedule_cases(ents: Entities) -> list[ScenarioCase]:
    """9 个局部重排：3 种扰动 × 3 档冻结策略，叠加在**已批准的基准周计划**上。"""
    days = ents.days()
    instructor = ents.instructors[0]
    aircraft_id = ents.aircraft_of_type(ents.student_types[0])[0]
    airspace_id = sorted(aid for aid, _cap in ents.airspaces)[0]
    disruptions = [
        (
            "person",
            {"persons": [instructor], "days": [2, 3], "reason": f"{instructor} 临时请假两天"},
            OverrideSpec(unavailable={instructor: (days[2].isoformat(), days[3].isoformat())}),
        ),
        (
            "aircraft",
            {"aircraft": [aircraft_id], "days": [], "reason": f"{aircraft_id} 临时定检整周"},
            OverrideSpec(
                maintenance_all_day=((aircraft_id, days[0].isoformat(), days[-1].isoformat()),)
            ),
        ),
        (
            "airspace",
            {"airspaces": [airspace_id], "days": [], "reason": f"{airspace_id} 空域整周关闭"},
            OverrideSpec(airspace_capacity={airspace_id: 0}),
        ),
    ]
    cases: list[ScenarioCase] = []
    idx = 0
    for kind, disruption, spec in disruptions:
        for policy in FREEZE_POLICIES:
            idx += 1
            cases.append(
                ScenarioCase(
                    scenario_id=f"RS-{idx:02d}",
                    category="reschedule",
                    family=f"reschedule-{kind}",
                    title=f"局部重排：{disruption['reason']}，冻结档 {policy}",
                    expected_status="EITHER",
                    overrides=spec,
                    reschedule={**disruption, "policy": policy},
                    notes="v6 §12.3 局部重排：在已批准计划上叠加扰动（v6 §3.8 三档冻结）",
                )
            )
    return cases


# ─────────────────────────────────────────────────────────────────────
# 汇总
# ─────────────────────────────────────────────────────────────────────
def baseline_case(ents: Entities) -> ScenarioCase:
    return ScenarioCase(
        scenario_id="BASE-01",
        category="baseline",
        family="baseline",
        title=f"基准周 {ents.week_start} 原始数据，零扰动",
        expected_status="SOLVED",
        notes="v6 §12.3 基准周：M2-A 实测 OPTIMAL / 14 架次 / 7 条阻塞项",
    )


def build_catalog(
    ents: Entities,
    *,
    c_airspaces: Sequence[str],
    boundary: Sequence[ScenarioCase] = (),
    single_quota: Mapping[str, int] | None = None,
) -> list[ScenarioCase]:
    """拼出完整的 200 场景清单（`boundary` 由标定器给出）。"""
    singles = single_point_cases(ents, single_quota)
    return [
        baseline_case(ents),
        *singles,
        *combo_cases(ents, singles),
        *boundary,
        *infeasible_cases(ents, c_airspaces),
        *reschedule_cases(ents),
    ]


def catalog_counts(cases: Sequence[ScenarioCase]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for case in cases:
        counts[case.category] = counts.get(case.category, 0) + 1
    return counts


def dataset_dir(root: Path) -> Path:
    return root / "datasets" / "plan_scenarios" / DATASET_VERSION


def write_dataset(root: Path, ents: Entities, cases: Sequence[ScenarioCase]) -> Path:
    """把清单落盘并版本化。"""
    target = dataset_dir(root)
    target.mkdir(parents=True, exist_ok=True)
    (target / "scenarios.json").write_text(
        json.dumps([c.to_json() for c in cases], ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    manifest = {
        "version": DATASET_VERSION,
        "snapshot_id": ents.snapshot_id,
        "week_start": ents.week_start.isoformat(),
        "combo_seed": COMBO_SEED,
        "counts": catalog_counts(cases),
        "total": len(cases),
        "entities": {
            "persons": [pid for pid, _n, _i in ents.persons],
            "aircraft": [aid for aid, _t in ents.aircraft],
            "airspaces": dict(ents.airspaces),
            "runways": {rid: list(types) for rid, types in ents.runways},
            "missions": [mid for mid, _c, _f in ents.missions],
        },
        "infeasible_annotations": {f.family_id: list(f.annotated) for f in INFEASIBLE_FAMILIES},
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def load_dataset(root: Path) -> list[ScenarioCase]:
    payload = json.loads((dataset_dir(root) / "scenarios.json").read_text(encoding="utf-8"))
    out: list[ScenarioCase] = []
    for row in payload:
        overrides = OverrideSpec(
            window_start=row["overrides"].get("window_start"),
            window_end=row["overrides"].get("window_end"),
            airspace_capacity=row["overrides"].get("airspace_capacity", {}),
            closed_runways=tuple(row["overrides"].get("closed_runways", ())),
            unavailable={k: tuple(v) for k, v in row["overrides"].get("unavailable", {}).items()},
            unavailable_all_week=tuple(row["overrides"].get("unavailable_all_week", ())),
            maintenance_all_day=tuple(
                tuple(item) for item in row["overrides"].get("maintenance_all_day", ())
            ),  # type: ignore[misc]
            qual_expiry=row["overrides"].get("qual_expiry", {}),
        )
        out.append(
            ScenarioCase(
                scenario_id=row["scenario_id"],
                category=row["category"],
                family=row["family"],
                title=row["title"],
                expected_status=row["expected_status"],
                overrides=overrides,
                annotated_conflict_rules=tuple(row.get("annotated_conflict_rules", ())),
                pair_id=row.get("pair_id"),
                pair_role=row.get("pair_role"),
                reschedule=row.get("reschedule"),
                time_limit_s=row.get("time_limit_s"),
                notes=row.get("notes", ""),
            )
        )
    return out


__all__ = [
    "COMBO_SEED",
    "DATASET_VERSION",
    "DEFAULT_SINGLE_QUOTA",
    "FREEZE_POLICIES",
    "INFEASIBLE_FAMILIES",
    "NO_OVERRIDE",
    "Entities",
    "InfeasibleFamily",
    "OverrideSpec",
    "ScenarioCase",
    "absence_cases",
    "airspace_cases",
    "baseline_case",
    "build_catalog",
    "catalog_counts",
    "combo_cases",
    "dataset_dir",
    "expiry_cases",
    "infeasible_cases",
    "load_dataset",
    "maintenance_cases",
    "reschedule_cases",
    "runway_cases",
    "single_point_cases",
    "write_dataset",
]
