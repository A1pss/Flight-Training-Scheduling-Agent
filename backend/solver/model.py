"""14 条规则的 CP-SAT 编码（v6 §3.2 对照表，逐条落点见本模块的 `_post_c**` 函数）。

**本模块是 `validator/checks.py` 的对照面，不是它的依赖。** 两套代码依据同一份
v6 §3.2 规格表**分别实现**，不共享任何约束表达代码（CLAUDE.md 铁律 2）。

## 三层对象：候选 / 时隙 / (飞机, 时隙)

```
候选   Candidate = (mission, day, crew, aircraft)   → x[c]
时隙   Slot      = (trainee, mission, day)          → present[s]、start[s]、rwy[s][r]
机位   (aircraft, slot)                             → u[ac,s]、飞行区间
```

**同一时隙至多产出一个架次**（约束14「候选集按 (person, mission, day) 唯一」，
本模块以结构性约束 `Σ x ≤ 1` 钉死）。这条性质带来三个直接后果：

1. **`start` 变量按时隙共享，而不是逐候选各持一个。** 同一时隙的多个候选只在
   教员/机号上不同，被选中的至多一个，共享起飞时刻与逐候选各持一个变量
   **在语义上完全等价**，但整数变量数从 O(候选) 降到 O(时隙)（基准周 2276 → 231）。
   候选自己的机号窗口若更紧，用 `OnlyEnforceIf(x[c])` 单独收紧。
2. **跑道变量也按时隙建**（v6 §3.3 的代码块写在候选层）。跑道是**架次**的属性，
   同一时隙里不同候选能用哪些跑道由机型决定，用
   `x[c] → ¬rwy[s][r]`（r 不适用于该候选的机型）把两层挂起来。
   20 分钟窗口的区间数因此从 4552 降到 462。
3. **约束7 的周转时间用「尾部延长 T 的区间 + `AddNoOverlap`」**，与 v6 §3.2 写的
   「同机候选两两 reified 析取」逐字等价（见 `post_c07_aircraft` 的推导），
   约束数从 O(n²) 降到 O(n)。

这三处是 v6 §3.1.3/§3.2/§3.3 编码的**等价重写**，不改变任何一条约束的语义。
它们不是「顺手优化」：**照字面写的那一版基准周 30s 内证不到 OPTIMAL**（实测
gap 35.7%，状态停在 FEASIBLE），等价重写之后 1 秒内证到。规模对比与理由写在
`reports/M2A_收工报告.md`。

## 约束9 是本模型最关键的性能设计（v6 §3.3）

朴素做法对任意三个起飞时刻加 `max−min ≥ 20` 是 O(n³)，退化成 1 分钟网格布尔矩阵
要 ~86 万变量。这里用 CP-SAT 原生 `AddCumulative`：语义上表达「任意时刻并发数
≤ cap」，与 S-04 的半开窗 `[t, t+20)` 完全等价，变量数为区间级。

- **20 分钟窗口 cap=2 → 按 (day, runway) 分组**（S-05 `per_runway`）
- **7 分钟间隔 cap=1 → 按 day 分组、全场统一，不分跑道**（D-2）

⚠️ 把 7 分钟也写成按跑道分组就违反了 D-2：`rules.pdf` 约束9 原文
「**同一跑道**任意 20 分钟滑动窗口内起飞次数不得超过 2 次；任意两架次起飞时刻
间隔不少于 7 分钟」——**前半句限定了「同一跑道」，后半句没有**。

## 诊断模式

`diagnose=True` 时每个约束组挂一个 assumption literal，`Solve()` 判 INFEASIBLE 后
`sufficient_assumptions_for_infeasibility()` 直接给出最小冲突集（v6 §3.9）。
**R0 组同样挂 literal** —— 那是为了让「跑道密度」这类约束能真的出现在冲突集里
（v6 §12.3 的 I5 专门验这一点），**与「可否松弛」是两件事**：可松弛性由
`ConstraintGroup.relaxable` 独立表达，R0 恒为 False，代码层硬编码禁止。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from ortools.sat.python import cp_model

from backend.core.ruleset import Ruleset, Semantics
from backend.schemas.intent import ConstraintSpec
from backend.solver.candidates import Candidate, CandidateSet, Requirement, SlotKey
from backend.solver.data import ProblemData

#: 诊断模式下 `start` 的宽域上界（分钟）。放宽约束1 之后架次可以落到训练窗之外，
#: 但仍限定在当日之内（不跨日，约束1 的另一半）。
DIAGNOSE_HORIZON: Final[int] = 24 * 60


@dataclass(frozen=True)
class ConstraintGroup:
    """一个可独立开关的约束组（诊断粒度）。"""

    group_id: str
    rule_ids: tuple[int, ...]
    tier: str
    description: str
    relaxable: bool

    @property
    def check_ids(self) -> tuple[str, ...]:
        return tuple(f"C{rid:02d}" for rid in self.rule_ids)


@dataclass(frozen=True)
class FrozenSortie:
    """局部重排里被硬固定的架次（v6 §3.8）。**跑道一并冻结。**"""

    trainee_id: str
    mission_id: str
    day: int
    aircraft_id: str
    instructor_id: str | None
    takeoff_minute: int
    runway_id: str

    @property
    def slot(self) -> SlotKey:
        return (self.trainee_id, self.mission_id, self.day)


@dataclass(frozen=True)
class RelaxationSettings:
    """当前松弛档位（v6 §3.10 的 Tier 0~3）。

    **R0 恒不可松弛**：本类没有任何字段能碰到 R0 组，这是代码层的硬编码禁止。
    R1（约束10/11/12）只在 Tier 3 且**显式给出授权后的上限增量**时才放宽——
    「临时提至 16」这类数字必须由提案带着走并经 `probe_solve` 验证，
    不能由代码替训练主任拍一个。
    """

    tier: int = 0
    daily_minutes_bonus: int = 0
    weekly_sorties_bonus: int = 0
    daily_sorties_bonus: int = 0

    def soft_rules(self, ruleset: Ruleset) -> frozenset[int]:
        """本档位下降级为软目标的规则编号。"""
        if self.tier <= 0:
            return frozenset()
        relaxes = set(ruleset.ladder_step(min(self.tier, 3)).relaxes)
        return frozenset(rid for rid in relaxes if ruleset.is_relaxable(rid))

    def r1_active(self) -> bool:
        return self.tier >= 3 and bool(
            self.daily_minutes_bonus or self.weekly_sorties_bonus or self.daily_sorties_bonus
        )


@dataclass
class BuiltModel:
    """`build_model` 的产物。持有全部变量句柄，供目标函数、解析与诊断使用。"""

    model: cp_model.CpModel
    data: ProblemData
    spec: ConstraintSpec
    cset: CandidateSet
    ruleset: Ruleset
    relaxation: RelaxationSettings
    x: tuple[cp_model.IntVar, ...]
    slot_start: Mapping[SlotKey, cp_model.IntVar]
    slot_present: Mapping[SlotKey, cp_model.IntVar]
    slot_runway: Mapping[SlotKey, Mapping[str, cp_model.IntVar]]
    slot_bounds: Mapping[SlotKey, tuple[int, int]]
    groups: Mapping[str, ConstraintGroup]
    assumptions: Mapping[str, cp_model.IntVar]
    satisfied: Mapping[str, cp_model.IntVar]
    diagnose: bool
    num_slot_vars: int = 0
    forced_zero: tuple[int, ...] = ()
    hints: tuple[int, ...] = field(default_factory=tuple)

    def start_of(self, index: int) -> cp_model.IntVar:
        """候选的起飞时刻变量（按时隙共享，见模块文档）。"""
        return self.slot_start[self.cset.candidates[index].slot]

    def runway_of(self, index: int) -> Mapping[str, cp_model.IntVar]:
        """候选所属时隙的跑道变量（跑道是架次的属性，见模块文档 ③）。"""
        return self.slot_runway[self.cset.candidates[index].slot]

    def slot_bounds_of(self, slot: SlotKey) -> tuple[int, int]:
        """时隙起飞时刻变量的域 `[lo, hi]`（约束1 的结构性落点）。"""
        return self.slot_bounds[slot]

    def group(self, group_id: str) -> ConstraintGroup:
        return self.groups[group_id]


# ─────────────────────────────────────────────────────────────────────
# 组定义
# ─────────────────────────────────────────────────────────────────────
def _group_catalog(ruleset: Ruleset) -> dict[str, ConstraintGroup]:
    """约束组目录。`tier` 与 `relaxable` 一律取 `ruleset_v1.3.yaml`，不在代码里另立。"""

    def make(gid: str, rule_ids: tuple[int, ...], desc: str) -> ConstraintGroup:
        tiers = {ruleset.tier_of(rid) for rid in rule_ids}
        # 一个组横跨多级时取最严的一级（R0 < R1 < R2 < R3 越前越严）
        tier = sorted(tiers)[0]
        return ConstraintGroup(
            group_id=gid,
            rule_ids=rule_ids,
            tier=tier,
            description=desc,
            relaxable=all(ruleset.is_relaxable(rid) for rid in rule_ids),
        )

    return {
        g.group_id: g
        for g in (
            make("C01_window", (1,), "约束1 时间一致性：架次落在训练窗内、不跨日"),
            make("C03_weekly", (3,), "约束3 每周必飞：每名学员每周 ≥1 次 A 类（S-02/S-13）"),
            make("C04_person_overlap", (4,), "约束4 岗位互斥：同一人同刻只在一个架次"),
            make("C06_airspace", (6,), "约束6 空域同时段容量（S-10，硬约束）"),
            make("C07_aircraft", (7,), "约束7 飞机排期冲突与周转时间（S-06 着陆→起飞）"),
            make("C08_gap", (8,), "约束8 同人同日相邻架次间隔 ≥10 分钟"),
            make("C08_rest", (8,), "约束8 连续 2 架次后休息 ≥30 分钟（S-07 仅同日累计）"),
            make("C09_window", (9,), "约束9 同一跑道 20 分钟窗口内起飞 ≤2 次（S-04/S-05）"),
            make("C09_separation", (9,), "约束9 任意两次起飞间隔 ≥7 分钟（全场统一，D-2）"),
            make("C10_daily_minutes", (10,), "约束10 单人单日飞行时长上限"),
            make("C11_weekly_sorties", (11,), "约束11 单人单周架次上限"),
            make("C12_person_daily", (12,), "约束12 单人单日架次上限"),
            make("C12_aircraft_daily", (12,), "约束12 单机单日架次上限"),
            make("C13_frequency", (13,), "约束13 频率滑窗与跨周锚点（S-01/S-03/S-11/S-12）"),
            make("C14_req_max", (14,), "约束14 任务唯一性：Σx ≤ ceil(7/freq_days)"),
        )
    }


# ─────────────────────────────────────────────────────────────────────
# 建模
# ─────────────────────────────────────────────────────────────────────
class _Builder:
    """把 :class:`CandidateSet` 编译成 CP-SAT 模型。

    拆成一个类只为让十几个 `_post_c**` 方法共享变量句柄；对外只暴露
    :func:`build_model`。
    """

    def __init__(
        self,
        data: ProblemData,
        spec: ConstraintSpec,
        cset: CandidateSet,
        *,
        ruleset: Ruleset,
        semantics: Semantics,
        relaxation: RelaxationSettings,
        diagnose: bool,
        frozen: Sequence[FrozenSortie],
        hints: Mapping[int, int] | None,
    ) -> None:
        self.data = data
        self.spec = spec
        self.cset = cset
        self.ruleset = ruleset
        self.semantics = semantics
        self.relaxation = relaxation
        self.diagnose = diagnose
        self.frozen = tuple(frozen)
        self.hint_map = dict(hints or {})
        self.m = cp_model.CpModel()
        self.groups = _group_catalog(ruleset)
        self.assumptions: dict[str, cp_model.IntVar] = {}
        self.satisfied: dict[str, cp_model.IntVar] = {}
        self.forced_zero: list[int] = []
        self.soft_rules = relaxation.soft_rules(ruleset)

        self.x: list[cp_model.IntVar] = []
        self.slot_runway: dict[SlotKey, dict[str, cp_model.IntVar]] = {}
        self.slot_start: dict[SlotKey, cp_model.IntVar] = {}
        self.slot_present: dict[SlotKey, cp_model.IntVar] = {}
        #: 时隙起飞时刻变量的域（自己记着，不去读 CP-SAT 的 proto）
        self.slot_bounds: dict[SlotKey, tuple[int, int]] = {}
        #: (aircraft_id, slot) → 该机执行该时隙的指示变量（约束7/12 用）
        self.aircraft_slot: dict[tuple[str, SlotKey], cp_model.IntVar] = {}
        #: person → 参与的 (lit, slot) 列表，按天分组（约束4/8/10/12 用）
        self.person_parts: dict[tuple[str, int], list[tuple[cp_model.IntVar, SlotKey]]] = {}

    # ── 工具 ────────────────────────────────────────────────────────
    def _lit(self, group_id: str) -> cp_model.IntVar | None:
        """诊断模式下取该组的 assumption literal，常规模式返回 None（约束无条件生效）。"""
        if not self.diagnose:
            return None
        if group_id not in self.assumptions:
            self.assumptions[group_id] = self.m.new_bool_var(f"assume_{group_id}")
        return self.assumptions[group_id]

    def _gated(
        self, base: cp_model.IntVar, gate: cp_model.IntVar | None, name: str
    ) -> cp_model.IntVar:
        """`p ⇔ base ∧ gate`。区间类约束（NoOverlap/Cumulative）不支持
        `OnlyEnforceIf`，只能靠「让区间在关掉这一组时整体消失」来实现开关。"""
        if gate is None:
            return base
        p = self.m.new_bool_var(name)
        self.m.add_implication(p, base)
        self.m.add_implication(p, gate)
        self.m.add_bool_or([~base, ~gate, p])
        return p

    def _duration(self, mission_id: str) -> int:
        return self.data.missions[mission_id].duration_minutes

    def _candidate_bounds(self, cand: Candidate) -> tuple[int, int]:
        """候选可行的起飞分钟区间 `[lo, hi]`（约束1 + 机号可用窗）。"""
        ac = self.data.aircraft[cand.aircraft_id]
        dur = self._duration(cand.mission_id)
        lo = max(0, self.data.minutes_of(ac.window_start))
        hi = min(self.data.horizon_minutes, self.data.minutes_of(ac.window_end)) - dur
        return lo, hi

    # ── 变量 ────────────────────────────────────────────────────────
    def build_variables(self) -> None:
        cands = self.cset.candidates
        # ① 时隙层：起飞时刻与「本时隙是否产出架次」
        for slot, idxs in self.cset.slots.items():
            trainee, mission_id, _day = slot
            dur = self._duration(mission_id)
            cand_bounds = [self._candidate_bounds(cands[i]) for i in idxs]
            lo = min(b[0] for b in cand_bounds)
            hi = max(b[1] for b in cand_bounds)
            name = f"s_{trainee}_{mission_id}_d{slot[2]}"
            bounds = (0, max(0, DIAGNOSE_HORIZON - dur)) if self.diagnose else (lo, max(lo, hi))
            self.slot_bounds[slot] = bounds
            self.slot_start[slot] = self.m.new_int_var(bounds[0], bounds[1], name)
            self.slot_present[slot] = self.m.new_bool_var(f"y_{trainee}_{mission_id}_d{slot[2]}")

        # ② 候选层：选择变量
        for cand in cands:
            self.x.append(self.m.new_bool_var(f"x_{cand.key}"))

        # ③ 跑道变量按**时隙**建（一个时隙至多一个架次，跑道是那个架次的属性）。
        #    v6 §3.3 的代码块写在候选层；时隙层与它等价，但跑道布尔变量与 20 分钟
        #    窗口区间的个数都降一个量级（基准周 4552 → 462）。
        for slot, idxs in self.cset.slots.items():
            allowed: set[str] = set()
            for i in idxs:
                allowed |= set(
                    self.data.allowed_runways(
                        self.data.aircraft[cands[i].aircraft_id].aircraft_type
                    )
                )
            lits = {
                r: self.m.new_bool_var(f"rwy_{slot[0]}_{slot[1]}_d{slot[2]}_{r}")
                for r in sorted(allowed)
            }
            self.slot_runway[slot] = lits
        for idx, cand in enumerate(cands):
            lits = self.slot_runway[cand.slot]
            ac_allowed = set(
                self.data.allowed_runways(self.data.aircraft[cand.aircraft_id].aircraft_type)
            )
            if not ac_allowed:
                # 该机型没有任何可用跑道（跑道关闭）→ 归约束9 组，好让跑道关闭
                # 这个原因能真的出现在最小冲突集里（v6 §12.3 I5）
                gate = self._lit("C09_window")
                if gate is None:
                    self.m.add(self.x[idx] == 0)
                else:
                    self.m.add(self.x[idx] == 0).only_enforce_if(gate)
                self.forced_zero.append(idx)
                continue
            # 选了这个候选 → 该时隙只能落在这架飞机允许的跑道上
            for runway_id, lit in sorted(lits.items()):
                if runway_id not in ac_allowed:
                    self.m.add_implication(self.x[idx], ~lit)

        # ④ 结构性：同一时隙至多一个候选被选中，且 present == Σx
        for slot, idxs in self.cset.slots.items():
            total = sum(self.x[i] for i in idxs)
            self.m.add(total <= 1)
            self.m.add(self.slot_present[slot] == total)

        # ⑤ 候选 → 时隙起飞时刻的机号窗口收紧（机号窗口比时隙域更紧时）
        for idx, cand in enumerate(cands):
            lo, hi = self._candidate_bounds(cand)
            start = self.slot_start[cand.slot]
            if self.diagnose:
                # 诊断模式下窗口上下界由 `post_c01` 以 assumption literal 下达 ——
                # 这里**不能**抢先无条件把 x 压成 0，否则「训练窗压缩」这个根因
                # 永远进不了最小冲突集（v6 §12.3 的 I4 就是验这一条）。
                continue
            if hi < lo:
                # 该候选连一次都放不进训练窗
                self.m.add(self.x[idx] == 0)
                self.forced_zero.append(idx)
                continue
            dom_lo, dom_hi = self.slot_bounds[cand.slot]
            if dom_lo < lo:
                self.m.add(start >= lo).only_enforce_if(self.x[idx])
            if dom_hi > hi:
                self.m.add(start <= hi).only_enforce_if(self.x[idx])

        # ⑥ (飞机, 时隙) 指示变量
        by_aircraft_slot: dict[tuple[str, SlotKey], list[int]] = {}
        for idx, cand in enumerate(cands):
            by_aircraft_slot.setdefault((cand.aircraft_id, cand.slot), []).append(idx)
        for key, ac_idxs in sorted(by_aircraft_slot.items()):
            lit = self.m.new_bool_var(f"u_{key[0]}_{key[1][0]}_{key[1][1]}_d{key[1][2]}")
            self.m.add(lit == sum(self.x[i] for i in ac_idxs))
            self.aircraft_slot[key] = lit

        # ⑦ 人员参与：受训人拿时隙指示，教员拿 (教员, 时隙) 指示
        by_instructor_slot: dict[tuple[str, SlotKey], list[int]] = {}
        for idx, cand in enumerate(cands):
            if cand.instructor_id is not None:
                by_instructor_slot.setdefault((cand.instructor_id, cand.slot), []).append(idx)
        for slot in self.cset.slots:
            trainee, _mission_id, day = slot
            self.person_parts.setdefault((trainee, day), []).append((self.slot_present[slot], slot))
        for (person_id, slot), ins_idxs in sorted(by_instructor_slot.items()):
            lit = self.m.new_bool_var(f"z_{person_id}_{slot[0]}_{slot[1]}_d{slot[2]}")
            self.m.add(lit == sum(self.x[i] for i in ins_idxs))
            self.person_parts.setdefault((person_id, slot[2]), []).append((lit, slot))

    # ── 约束1 ───────────────────────────────────────────────────────
    def post_c01(self) -> None:
        """约束1 时间一致性。

        常规模式下**结构性成立**：时间以「当日训练窗起点起的分钟数」编码、域限定
        `[lo, hi]`、天维度独立，`end = start + dur` 由区间变量构造保证，无需额外约束
        （v6 §3.1.3）。诊断模式下改用宽域 + 显式上下界，好让训练窗压缩这类扰动
        能真的出现在最小冲突集里（v6 §12.3 的 I4/I5）。
        """
        if not self.diagnose:
            return
        gate = self._lit("C01_window")
        for idx, cand in enumerate(self.cset.candidates):
            lo, hi = self._candidate_bounds(cand)
            start = self.slot_start[cand.slot]
            enforce = [self.x[idx]] + ([gate] if gate is not None else [])
            self.m.add(start >= lo).only_enforce_if(enforce)
            self.m.add(start <= hi).only_enforce_if(enforce)

    # ── 约束3 / 约束13 / S-11：要求集 ───────────────────────────────
    def post_requirements(self) -> None:
        """约束3、约束13、S-11 复训 —— 统一为 `Σ x ≥ min_count`。

        松弛档下（Tier 1 软化约束13、Tier 2 再软化约束3，D-6）改为挂一个满足
        指示变量，由 §3.7 阶段1 的完成度目标去最大化，**未满足的量 100% 写进
        `TrainingDebt` 显式披露**。
        """
        mission_class_of = {mid: m.mission_class for mid, m in self.data.missions.items()}
        for req in self.cset.requirements:
            scope = [
                self.x[i]
                for i, cand in enumerate(self.cset.candidates)
                if req.matches(cand, mission_class_of)
            ]
            group_id = "C03_weekly" if req.rule_id == 3 else "C13_frequency"
            gate = self._lit(group_id)
            # 候选集为空时用一个恒 0 变量表达要求，`0 ≥ 1` 即不可满足。
            # **这正是 I2 那类场景的形状**：6 架 JL-8 全部维护 → A 类一个候选都没有
            # → 约束3 进入冲突集，真实根因（约束6 机队）由 §3.9 的归因补上。
            total = sum(scope) if scope else self.m.new_int_var(0, 0, f"empty_{req.req_id}")
            if req.rule_id in self.soft_rules:
                sat = self.m.new_bool_var(f"sat_{req.req_id}")
                self.satisfied[req.req_id] = sat
                if not scope:
                    self.m.add(sat == 0)
                    continue
                enforce = [sat] + ([gate] if gate is not None else [])
                self.m.add(total >= req.min_count).only_enforce_if(enforce)
                self.m.add(total <= req.min_count - 1).only_enforce_if(~sat)
            elif gate is not None:
                self.m.add(total >= req.min_count).only_enforce_if(gate)
            else:
                self.m.add(total >= req.min_count)

        # 冗余但有用：每个 (人, 课目) 的**本周最少架次数**（滑窗的最小命中数）。
        # 它由上面那组窗口约束**逻辑蕴含**，不改变可行集；作用是把「本周至少要飞
        # 多少架次」这个下界直接摆到 LP 松弛面前。没有它，CP-SAT 只能靠 bool_core
        # 一格一格往上顶下界（基准周实测 14.8s → 8.9s）。
        # 同一性质的第二条冗余下界：全周架次总数。由约束3 + 约束13 共同蕴含，
        # 所以诊断模式下不下这条（那时这两组会被 assumption literal 关掉）。
        if not self.diagnose and not self.soft_rules:
            floor = self.cset.implied_min_sorties(mission_class_of)
            if floor > 0:
                self.m.add(sum(self.slot_present[slot] for slot in self.cset.slots) >= floor)

        gate13 = self._lit("C13_frequency")
        if 13 not in self.soft_rules:
            for basis in self.cset.debt_basis:
                if basis.required <= 1:
                    continue
                idxs = [
                    i
                    for i, cand in enumerate(self.cset.candidates)
                    if cand.trainee_id == basis.person_id and cand.mission_id == basis.mission_id
                ]
                if not idxs:
                    continue
                expr = sum(self.x[i] for i in idxs)
                if gate13 is None:
                    self.m.add(expr >= basis.required)
                else:
                    self.m.add(expr >= basis.required).only_enforce_if(gate13)

    # ── 约束4 / 约束8 ───────────────────────────────────────────────
    def post_person_constraints(self) -> None:
        """约束4（岗位互斥）、约束8（间隔 ≥10 / 连续 2 架次后休息 ≥30）。

        - 约束4 用 `AddNoOverlap`：同一人（含教员岗）同刻只能在一个架次上。
        - 约束8 按 **S-07 仅同日内累计**，两两 reified 析取给出 ≥10 分钟间隔；
          休息 30 分钟用「前置架次数 ≥2 的那个架次，与它之前的每个架次间隔 ≥30」
          表达 —— 与「第 2 架次→第 3 架次 ≥30」等价（第 1 架次结束更早，那条
          由传递性自动成立），但不需要枚举三元组。
        """
        overlap_gate = self._lit("C04_person_overlap")
        gap_gate = self._lit("C08_gap")
        rest_gate = self._lit("C08_rest")
        min_gap = self.ruleset.min_gap_minutes
        rest_after = self.ruleset.rest_after_n
        rest_min = self.ruleset.rest_minutes

        for (person_id, day), parts in sorted(self.person_parts.items()):
            if len(parts) < 2:
                continue
            # 约束4：不重叠
            intervals = [
                self.m.new_optional_interval_var(
                    self.slot_start[slot],
                    self._duration(slot[1]),
                    self.slot_start[slot] + self._duration(slot[1]),
                    self._gated(lit, overlap_gate, f"pc4_{person_id}_{slot[1]}_d{day}"),
                    f"itv_p_{person_id}_{slot[0]}_{slot[1]}_d{day}",
                )
                for lit, slot in parts
            ]
            self.m.add_no_overlap(intervals)

            # 约束8：两两间隔与休息
            precedes: dict[tuple[int, int], cp_model.IntVar] = {}
            for i in range(len(parts)):
                lit_i, slot_i = parts[i]
                dur_i = self._duration(slot_i[1])
                for j in range(i + 1, len(parts)):
                    lit_j, slot_j = parts[j]
                    dur_j = self._duration(slot_j[1])
                    order = self.m.new_bool_var(f"ord_{person_id}_d{day}_{i}_{j}")
                    base = [lit_i, lit_j] + ([gap_gate] if gap_gate is not None else [])
                    self.m.add(
                        self.slot_start[slot_j] >= self.slot_start[slot_i] + dur_i + min_gap
                    ).only_enforce_if([*base, order])
                    self.m.add(
                        self.slot_start[slot_i] >= self.slot_start[slot_j] + dur_j + min_gap
                    ).only_enforce_if([*base, ~order])
                    if len(parts) > rest_after:
                        precedes[i, j] = self._and3(
                            lit_i, lit_j, order, f"pre_{person_id}_d{day}_{i}_{j}"
                        )
                        precedes[j, i] = self._and3(
                            lit_i, lit_j, ~order, f"pre_{person_id}_d{day}_{j}_{i}"
                        )

            if len(parts) <= rest_after or not precedes:
                continue
            # rank[b] − 1 = 前置架次数；≥ rest_after 即「它是第 rest_after+1 个」
            needs_rest: dict[int, cp_model.IntVar] = {}
            for b in range(len(parts)):
                preds = [precedes[a, b] for a in range(len(parts)) if (a, b) in precedes]
                flag = self.m.new_bool_var(f"rest_{person_id}_d{day}_{b}")
                self.m.add(sum(preds) >= rest_after).only_enforce_if(flag)
                self.m.add(sum(preds) <= rest_after - 1).only_enforce_if(~flag)
                needs_rest[b] = flag
            for (a, b), pre_lit in sorted(precedes.items()):
                dur_a = self._duration(parts[a][1][1])
                enforce = [pre_lit, needs_rest[b]] + ([rest_gate] if rest_gate is not None else [])
                self.m.add(
                    self.slot_start[parts[b][1]] >= self.slot_start[parts[a][1]] + dur_a + rest_min
                ).only_enforce_if(enforce)

    def _and3(
        self,
        a: cp_model.LiteralT,
        b: cp_model.LiteralT,
        c: cp_model.LiteralT,
        name: str,
    ) -> cp_model.IntVar:
        """`p ⇔ a ∧ b ∧ c`。"""
        p = self.m.new_bool_var(name)
        self.m.add_implication(p, a)
        self.m.add_implication(p, b)
        self.m.add_implication(p, c)
        self.m.add_bool_or([~a, ~b, ~c, p])
        return p

    # ── 约束6 空域容量 ──────────────────────────────────────────────
    def post_c06_airspace(self) -> None:
        """约束6 的第二半（S-10）：空域同时段容量，硬约束。

        用**完整飞行区间** `[start, start+dur]` 做 `AddCumulative`。
        ⚠️ 与约束9 的区别必须讲清：约束9 约束**起飞时刻**的密度（长度 20/7 的
        人造区间），本条约束**占用时段**的并发（真实飞行区间）——两套独立的
        Cumulative，容量维度与区间语义都不同。
        """
        gate = self._lit("C06_airspace")
        by_key: dict[tuple[str, int], list[SlotKey]] = {}
        for slot in self.cset.slots:
            airspace = self.data.missions[slot[1]].airspace_id
            by_key.setdefault((airspace, slot[2]), []).append(slot)
        for (airspace_id, day), slots in sorted(by_key.items()):
            cap = self.data.capacity_of(airspace_id)
            intervals = [
                self.m.new_optional_interval_var(
                    self.slot_start[slot],
                    self._duration(slot[1]),
                    self.slot_start[slot] + self._duration(slot[1]),
                    self._gated(
                        self.slot_present[slot], gate, f"pc6_{airspace_id}_{slot[1]}_d{day}"
                    ),
                    f"itv_as_{airspace_id}_{slot[0]}_{slot[1]}_d{day}",
                )
                for slot in slots
            ]
            self.m.add_cumulative(intervals, [1] * len(intervals), cap)

    # ── 约束7 飞机冲突与周转 ────────────────────────────────────────
    def post_c07_aircraft(self) -> None:
        """约束7：同机架次的周转时间 + 维护时段固定区间。

        周转基准按 **S-06 从上一架次着陆算到下一架次起飞**。v6 §3.2 给的编码是
        「同机候选两两 reified 析取」：
        `x[a] ∧ x[b] → (s[b] ≥ e[a]+T) ∨ (s[a] ≥ e[b]+T)`。
        这里改用**把区间尾部延长 T 之后做 `AddNoOverlap`**：两条无重叠的区间
        `[s_i, s_i+dur_i+T)` 与 `[s_j, s_j+dur_j+T)` 当且仅当
        `s_j ≥ s_i+dur_i+T ∨ s_i ≥ s_j+dur_j+T`，**与那条析取逐字等价**，
        但约束数从 O(n²) 降到 O(n)，且吃上 CP-SAT 原生 disjunctive 传播器。

        > 基准周实测：两两析取要多造 1.6 万个布尔序变量、3.3 万条 reified 约束，
        > 30s 内证不到 OPTIMAL；改成延长区间后模型规模减半、1 秒内证到。
        > 这与 v6 §3.3 为约束9 放弃 O(n³) 朴素写法、改用原生 `AddCumulative`
        > 是同一性质的选择：**换编码，不换语义**。

        `T` 取 `aircraft.turnaround_minutes`（逐机一列的真实数据，不是按机型抄的常量）。
        维护另起一条 `NoOverlap`，用**未延长**的飞行区间 —— 因为规格只说
        「维护时段内该机不得安排架次」，没有要求维护前后也留周转时间。
        """
        gate = self._lit("C07_aircraft")
        by_key: dict[tuple[str, int], list[SlotKey]] = {}
        for aircraft_id, slot in self.aircraft_slot:
            by_key.setdefault((aircraft_id, slot[2]), []).append(slot)
        for (aircraft_id, day), slot_list in sorted(by_key.items()):
            ac = self.data.aircraft[aircraft_id]
            turnaround = ac.turnaround_minutes
            slots = sorted(slot_list)
            extended: list[cp_model.IntervalVar] = []
            plain: list[cp_model.IntervalVar] = []
            for slot in slots:
                lit = self._gated(
                    self.aircraft_slot[aircraft_id, slot],
                    gate,
                    f"pc7_{aircraft_id}_{slot[1]}_d{day}",
                )
                dur = self._duration(slot[1])
                start = self.slot_start[slot]
                extended.append(
                    self.m.new_optional_interval_var(
                        start,
                        dur + turnaround,
                        start + dur + turnaround,
                        lit,
                        f"itv_acT_{aircraft_id}_{slot[0]}_{slot[1]}_d{day}",
                    )
                )
                plain.append(
                    self.m.new_optional_interval_var(
                        start,
                        dur,
                        start + dur,
                        lit,
                        f"itv_ac_{aircraft_id}_{slot[0]}_{slot[1]}_d{day}",
                    )
                )
            if turnaround > 0 and len(extended) > 1:
                self.m.add_no_overlap(extended)

            when = self.data.date_of(day)
            maintenance: list[cp_model.IntervalVar] = []
            for k, window in enumerate(ac.maintenance):
                span = window.minute_span(when, self.data.window_start)
                if span is None:
                    continue
                lo, hi = span
                size = max(1, hi - lo)
                name = f"maint_{aircraft_id}_d{day}_{k}"
                if gate is None:
                    maintenance.append(self.m.new_interval_var(lo, size, lo + size, name))
                else:
                    maintenance.append(
                        self.m.new_optional_interval_var(lo, size, lo + size, gate, name)
                    )
            if maintenance or turnaround == 0:
                self.m.add_no_overlap(plain + maintenance)

    # ── 约束9 起降密度 ──────────────────────────────────────────────
    def post_c09_density(self) -> None:
        """约束9（v6 §3.3，S-05 + D-2）。

        ① 跑道分配：每个被选中的候选**恰好**占用一条允许的跑道；未选中的不占跑道。
           JL-9 只有 RWY-1 可用 → 由数据（`runway_aircraft_types`）自然固定，
           **不是代码里写死的**；JL-8 的跑道是决策变量。
        ② 20 分钟窗口 cap=2，按 **(day, runway)** 分组（`per_runway`）。
        ③ 7 分钟间隔 cap=1，按 **day** 分组、**全场统一不分跑道**（`airport_wide`，D-2）。
        """
        window_gate = self._lit("C09_window")
        sep_gate = self._lit("C09_separation")
        window_len = self.ruleset.density_window_minutes
        window_cap = self.ruleset.density_window_cap
        sep_len = self.ruleset.separation_minutes

        # ① 跑道分配：产出架次的时隙恰好占用一条跑道，不产出的不占
        for slot, lits in self.slot_runway.items():
            if not lits:
                continue
            self.m.add_exactly_one(lits.values()).only_enforce_if(self.slot_present[slot])
            for lit in lits.values():
                self.m.add_implication(lit, self.slot_present[slot])

        # ② 20 分钟窗口 cap=2，按 (day, runway) 分组（S-05 per_runway）
        per_runway = self.spec.density_scope.get("window_20min", "per_runway") == "per_runway"
        for day in self.data.days:
            buckets: dict[str, list[cp_model.IntervalVar]] = {}
            for slot, lits in self.slot_runway.items():
                if slot[2] != day:
                    continue
                start = self.slot_start[slot]
                tag = f"{slot[0]}_{slot[1]}_d{day}"
                if per_runway:
                    for runway_id, lit in sorted(lits.items()):
                        presence = self._gated(lit, window_gate, f"pc9w_{tag}_{runway_id}")
                        buckets.setdefault(runway_id, []).append(
                            self.m.new_optional_interval_var(
                                start,
                                window_len,
                                start + window_len,
                                presence,
                                f"win_{tag}_{runway_id}",
                            )
                        )
                else:
                    # single_runway：全场一个窗口池，按架次记一次，别按跑道重复计数
                    presence = self._gated(self.slot_present[slot], window_gate, f"pc9w_{tag}")
                    buckets.setdefault("ALL", []).append(
                        self.m.new_optional_interval_var(
                            start, window_len, start + window_len, presence, f"win_{tag}"
                        )
                    )
            for _key, intervals in sorted(buckets.items()):
                if intervals:
                    self.m.add_cumulative(intervals, [1] * len(intervals), window_cap)

        # ③ 7 分钟间隔 cap=1，按 day 分组、**全场统一不分跑道**（D-2）
        airport_wide = (
            self.spec.density_scope.get("separation_7min", "airport_wide") == "airport_wide"
        )
        for day in self.data.days:
            grouped: dict[str, list[cp_model.IntervalVar]] = {}
            for slot in self.cset.slots:
                if slot[2] != day:
                    continue
                start = self.slot_start[slot]
                if airport_wide:
                    presence = self._gated(
                        self.slot_present[slot], sep_gate, f"pc9s_{slot[0]}_{slot[1]}_d{day}"
                    )
                    grouped.setdefault("ALL", []).append(
                        self.m.new_optional_interval_var(
                            start,
                            sep_len,
                            start + sep_len,
                            presence,
                            f"sep_{slot[0]}_{slot[1]}_d{day}",
                        )
                    )
            if not airport_wide:
                # 备用口径（S-05 density_scope 切成按跑道）：按 (day, runway) 分池
                for slot, lits in self.slot_runway.items():
                    if slot[2] != day:
                        continue
                    start = self.slot_start[slot]
                    tag = f"{slot[0]}_{slot[1]}_d{day}"
                    for runway_id, lit in sorted(lits.items()):
                        presence = self._gated(lit, sep_gate, f"pc9sr_{tag}_{runway_id}")
                        grouped.setdefault(runway_id, []).append(
                            self.m.new_optional_interval_var(
                                start,
                                sep_len,
                                start + sep_len,
                                presence,
                                f"sepr_{tag}_{runway_id}",
                            )
                        )
            for _key, intervals in sorted(grouped.items()):
                if intervals:
                    self.m.add_cumulative(intervals, [1] * len(intervals), 1)

    # ── 约束10 / 11 / 12 ────────────────────────────────────────────
    def post_capacity_limits(self) -> None:
        """约束10（单人单日时长）、约束11（单人单周架次）、约束12（单人/单机单日架次）。

        三条都是 R1 管理刚性：只有 Tier 3 且**带着授权后的具体增量**时才放宽
        （`RelaxationSettings`），代码不替训练主任拍数字。
        """
        soft = self.relaxation.r1_active()
        d_bonus = self.relaxation.daily_minutes_bonus if soft else 0
        w_bonus = self.relaxation.weekly_sorties_bonus if soft else 0
        s_bonus = self.relaxation.daily_sorties_bonus if soft else 0

        crew_by_person: dict[str, list[int]] = {}
        crew_by_person_day: dict[tuple[str, int], list[int]] = {}
        for idx, cand in enumerate(self.cset.candidates):
            for person_id in cand.crew_ids:
                crew_by_person.setdefault(person_id, []).append(idx)
                crew_by_person_day.setdefault((person_id, cand.day), []).append(idx)

        gate10 = self._lit("C10_daily_minutes")
        gate12p = self._lit("C12_person_daily")
        for (person_id, _day), idxs in sorted(crew_by_person_day.items()):
            identity = self.data.persons[person_id].identity
            minutes = sum(
                self.x[i] * self._duration(self.cset.candidates[i].mission_id) for i in idxs
            )
            cap_minutes = self.ruleset.daily_minute_cap(identity) + d_bonus
            if gate10 is None:
                self.m.add(minutes <= cap_minutes)
            else:
                self.m.add(minutes <= cap_minutes).only_enforce_if(gate10)
            cap_sorties = self.ruleset.daily_sorties_per_person + s_bonus
            if gate12p is None:
                self.m.add(sum(self.x[i] for i in idxs) <= cap_sorties)
            else:
                self.m.add(sum(self.x[i] for i in idxs) <= cap_sorties).only_enforce_if(gate12p)

        gate11 = self._lit("C11_weekly_sorties")
        for person_id, idxs in sorted(crew_by_person.items()):
            identity = self.data.persons[person_id].identity
            cap = self.ruleset.weekly_sortie_cap(identity) + w_bonus
            if gate11 is None:
                self.m.add(sum(self.x[i] for i in idxs) <= cap)
            else:
                self.m.add(sum(self.x[i] for i in idxs) <= cap).only_enforce_if(gate11)

        gate12a = self._lit("C12_aircraft_daily")
        by_aircraft_day: dict[tuple[str, int], list[int]] = {}
        for idx, cand in enumerate(self.cset.candidates):
            by_aircraft_day.setdefault((cand.aircraft_id, cand.day), []).append(idx)
        for (_aircraft_id, _day), idxs in sorted(by_aircraft_day.items()):
            cap = self.ruleset.daily_sorties_per_aircraft + s_bonus
            if gate12a is None:
                self.m.add(sum(self.x[i] for i in idxs) <= cap)
            else:
                self.m.add(sum(self.x[i] for i in idxs) <= cap).only_enforce_if(gate12a)

    # ── 约束14 ──────────────────────────────────────────────────────
    def post_c14_req_max(self) -> None:
        """约束14：`Σ x ≤ req_max = ceil(7 / freq_days)` per (person, mission)。

        A 类 3、B~F 类 1、G/H 类 1。定级 R0（ruleset 里业务方 2026-08-07 补裁）：
        `req_max` 由 `freq_days` 唯一确定，放宽它等于允许无意义的重复安排。
        """
        gate = self._lit("C14_req_max")
        by_pair: dict[tuple[str, str], list[int]] = {}
        for idx, cand in enumerate(self.cset.candidates):
            by_pair.setdefault((cand.trainee_id, cand.mission_id), []).append(idx)
        for (_person_id, mission_id), idxs in sorted(by_pair.items()):
            cap = self.spec.req_max[mission_id]
            total = sum(self.x[i] for i in idxs)
            if gate is None:
                self.m.add(total <= cap)
            else:
                self.m.add(total <= cap).only_enforce_if(gate)

    # ── 局部重排：冻结与 warm start ─────────────────────────────────
    def post_freeze(self) -> None:
        """冻结架次硬固定（v6 §3.8）：选中、起飞时刻、**跑道**一并钉死。"""
        index_of = {
            (c.trainee_id, c.mission_id, c.day, c.aircraft_id, c.instructor_id): i
            for i, c in enumerate(self.cset.candidates)
        }
        for sortie in self.frozen:
            key = (
                sortie.trainee_id,
                sortie.mission_id,
                sortie.day,
                sortie.aircraft_id,
                sortie.instructor_id,
            )
            idx = index_of.get(key)
            if idx is None:
                raise KeyError(
                    f"冻结架次 {key} 不在候选集中 —— 扰动可能已经把它排除了，"
                    "应先把它划入受影响集合而不是冻结它"
                )
            self.m.add(self.x[idx] == 1)
            self.m.add(self.slot_start[self.cset.candidates[idx].slot] == sortie.takeoff_minute)
            lits = self.slot_runway[self.cset.candidates[idx].slot]
            if sortie.runway_id not in lits:
                raise KeyError(f"冻结架次 {key} 要求跑道 {sortie.runway_id}，但该机型不可用该跑道")
            self.m.add(lits[sortie.runway_id] == 1)

    def post_hints(self) -> None:
        """上一版解作 warm start（v6 §3.8）。提示不改变可行集，只影响搜索顺序。"""
        for idx, value in sorted(self.hint_map.items()):
            self.m.add_hint(self.x[idx], value)

    # ── 增量约束（多轮修订，v6 §7.3.4）─────────────────────────────
    def post_incremental(self) -> None:
        """`IncrementalConstraint` 的求解器侧落点。

        它们是**求解器输入**，不是对结果的修改（v6 §7.3.4）。翻译由 Planner
        （W7）负责，本处只把已翻译好的六种 kind 编码进模型。
        """
        for inc in self.spec.incremental_constraints:
            targets = set(inc.targets)
            if inc.kind == "FORBID":
                for idx, cand in enumerate(self.cset.candidates):
                    if targets & set(cand.crew_ids) or cand.aircraft_id in targets:
                        self.m.add(self.x[idx] == 0)
            elif inc.kind == "PIN_RUNWAY":
                runway_id = str(inc.params.get("runway_id", ""))
                for idx, cand in enumerate(self.cset.candidates):
                    if not (targets & set(cand.crew_ids) or cand.aircraft_id in targets):
                        continue
                    lits = self.slot_runway[cand.slot]
                    if runway_id in lits and runway_id in self.data.allowed_runways(
                        self.data.aircraft[cand.aircraft_id].aircraft_type
                    ):
                        self.m.add(lits[runway_id] == 1).only_enforce_if(self.x[idx])
                    else:
                        # 该机型不可用该跑道（如 JL-9 被要求走 RWY-2）→ 该候选不可选。
                        # Planner 侧据此回滚并解释（v6 §7.3.4 第 3 条硬性设计）。
                        self.m.add(self.x[idx] == 0)
            elif inc.kind == "SHIFT_WINDOW":
                lo = int(inc.params.get("earliest_minute", 0))
                hi = int(inc.params.get("latest_minute", self.data.horizon_minutes))
                for idx, cand in enumerate(self.cset.candidates):
                    if not (targets & set(cand.crew_ids) or cand.aircraft_id in targets):
                        continue
                    start = self.slot_start[cand.slot]
                    self.m.add(start >= lo).only_enforce_if(self.x[idx])
                    self.m.add(start + self._duration(cand.mission_id) <= hi).only_enforce_if(
                        self.x[idx]
                    )
            elif inc.kind == "PIN_TIME":
                minute = int(inc.params.get("takeoff_minute", 0))
                for idx, cand in enumerate(self.cset.candidates):
                    if targets & set(cand.crew_ids):
                        self.m.add(self.slot_start[cand.slot] == minute).only_enforce_if(
                            self.x[idx]
                        )
            elif inc.kind == "PIN_RESOURCE":
                aircraft_id = str(inc.params.get("aircraft_id", ""))
                for idx, cand in enumerate(self.cset.candidates):
                    if targets & set(cand.crew_ids) and cand.aircraft_id != aircraft_id:
                        self.m.add(self.x[idx] == 0)
            elif inc.kind == "REDUCE_DENSITY":
                cap = int(inc.params.get("max_takeoffs_per_day", self.data.horizon_minutes))
                for day in self.data.days:
                    idxs = [i for i, c in enumerate(self.cset.candidates) if c.day == day]
                    if idxs:
                        self.m.add(sum(self.x[i] for i in idxs) <= cap)


def build_model(
    data: ProblemData,
    spec: ConstraintSpec,
    cset: CandidateSet,
    *,
    ruleset: Ruleset,
    semantics: Semantics,
    relaxation: RelaxationSettings | None = None,
    diagnose: bool = False,
    frozen: Sequence[FrozenSortie] = (),
    hints: Mapping[int, int] | None = None,
) -> BuiltModel:
    """把候选集编译成 CP-SAT 模型（14 条规则全部就位）。"""
    relax = relaxation or RelaxationSettings()
    builder = _Builder(
        data,
        spec,
        cset,
        ruleset=ruleset,
        semantics=semantics,
        relaxation=relax,
        diagnose=diagnose,
        frozen=frozen,
        hints=hints,
    )
    builder.build_variables()
    builder.post_c01()
    builder.post_requirements()
    builder.post_person_constraints()
    builder.post_c06_airspace()
    builder.post_c07_aircraft()
    builder.post_c09_density()
    builder.post_capacity_limits()
    builder.post_c14_req_max()
    builder.post_incremental()
    builder.post_freeze()
    builder.post_hints()

    if diagnose and builder.assumptions:
        builder.m.add_assumptions([builder.assumptions[k] for k in sorted(builder.assumptions)])

    return BuiltModel(
        model=builder.m,
        data=data,
        spec=spec,
        cset=cset,
        ruleset=ruleset,
        relaxation=relax,
        x=tuple(builder.x),
        slot_start=dict(builder.slot_start),
        slot_present=dict(builder.slot_present),
        slot_runway={k: dict(v) for k, v in builder.slot_runway.items()},
        slot_bounds=dict(builder.slot_bounds),
        groups=builder.groups,
        assumptions=dict(builder.assumptions),
        satisfied=dict(builder.satisfied),
        diagnose=diagnose,
        num_slot_vars=len(builder.slot_start),
        forced_zero=tuple(sorted(set(builder.forced_zero))),
        hints=tuple(sorted(builder.hint_map)),
    )


def requirement_scope(
    cset: CandidateSet, req: Requirement, mission_class_of: Mapping[str, str]
) -> tuple[int, ...]:
    """要求 `req` 的候选下标集合（目标函数与欠账结算共用）。"""
    return tuple(i for i, cand in enumerate(cset.candidates) if req.matches(cand, mission_class_of))


__all__ = [
    "DIAGNOSE_HORIZON",
    "BuiltModel",
    "ConstraintGroup",
    "FrozenSortie",
    "RelaxationSettings",
    "build_model",
    "requirement_scope",
]
