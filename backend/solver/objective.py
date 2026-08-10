"""目标函数与分阶段求解（v6 §3.7 + §3.11 求解预算）。

```
阶段1  max  Σ w_mission · 完成度            进度完成度（松弛档下才有取舍空间）
阶段2  min  Σ |x[c] − x_prev[c]|            汉明距离 ← 局部重排的核心
阶段3  min  教员负荷不均衡 + 早飞偏好惩罚 + 机队利用不均衡 + 跑道使用不均衡
```

分阶段（先求 obj1 最优值，固定为约束后求 obj2，依此类推）而不是加权求和，
语义比一个大权重和干净得多：加权和里「进度」和「均衡」的相对量级一变，
排出来的班就变，而那个量级没人能解释。

## 阶段1 为什么按「完成度」而不是按 `Σ x` 算

v6 §3.7 的写法是 `max Σ w_mission · x[c]`，括注是「**松弛档下才有取舍空间**」。
这句括注决定了唯一自洽的读法：**Tier 0 下阶段1 必须是常量**。

若照字面把 `Σ x[c]` 最大化，Tier 0 下它有大把取舍空间 —— 求解器会一路加架次
直到撞上约束11/12/14 的上限（基准周会从 ~14 架次涨到四名学员各飞满 10 架次），
那不是「进度完成度最优」，是「把机队塞满」。所以阶段1 的目标取
**要求满足度** `Σ w_req · sat[req]`：Tier 0 下全部要求是硬约束，`sat` 恒为 1、
目标恒为常量（本实现直接跳过该阶段）；Tier 1/2 下约束13/约束3 降级为软目标，
`sat` 才有取舍空间，此时最大化满足度正是「进度完成度」。

欠账加权按 v6 §3.7：`w_mission = BASE_W × (1 + DEBT_FACTOR × debt_count)`，
让上周欠下的课目本周优先补。

## 阶段3 比 v6 §3.7 多了一项，理由写在这里

v6 §3.7 阶段3 列了四项：教员负荷方差、早飞偏好、机队利用不均衡、跑道使用不均衡。
本实现**多加一项「架次总量」**并让它在阶段3 内部权重最高。原因是上面那段的另一半：
阶段1 取完成度之后，Tier 0 下「多排几个不必要的架次」在阶段1/2 里都是零代价，
四项均衡项也拦不住它（多排的架次只要摊匀，方差反而不涨）。没有这一项，
基准周会排出 30~40 个架次，全部合规但没人想看。

它是 **R3 偏好项，不影响可行性**（`ruleset.tiers.R3` 明确「不影响可行性」），
也不改变任何硬约束的语义。列在收工报告的「对 v6 的实现补充」里。

## 方差的线性替身：min-max 而不是极差

CP-SAT 是整数线性求解器，真方差是二次的。三个「不均衡」项一律用
**峰值负荷（min-max）** 表达：最小化「单个教员/单架飞机/单条跑道承担的最大架次数」，
在负载摊匀时取到最小，且是线性的。

为什么不用极差 `max − min`：机队里可能存在**本周一架次都排不上的飞机**
（基准周的两架 JL-9 —— 学员没有 JL-9 机型资质，刘斌本周又没有任何要求），
它把 `min` 恒钉在 0，于是极差退化成「峰值 − 0」，均衡项只剩噪声。
min-max 没有这个病：它只看峰值，不受「有资源闲着」影响。

这是编码选择，不是语义改动 —— 两者都在「摊匀」时取到最优。
"""

from __future__ import annotations

import time as _time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from ortools.sat.python import cp_model

from backend.core.ruleset import IDENTITY_INSTRUCTOR
from backend.schemas.solver import SolveStatus
from backend.solver.model import BuiltModel

#: `w_mission = BASE_W × (1 + DEBT_FACTOR × debt_count)`（v6 §3.7）。
#: v6 没给数值 —— 这两个是 R3 偏好参数（不影响可行性），取整数便于精确比较。
BASE_MISSION_WEIGHT: Final[int] = 100
DEBT_FACTOR: Final[int] = 1

#: 阶段3c 蕴含下界枚举的架次数上限。只是个枚举范围 —— 少枚举几档只会让下界更松，
#: 不会让任何解被误排除（这条约束是逻辑蕴含的）。
_LATENESS_BOUND_MAX_K: Final[int] = 20

#: 总预算里划给**优化阶段**的比例，剩下的给规范化阶段（见 :func:`solve_staged`）。
#:
#: 0.75 是量出来的，不是拍的。基准周实测：优化阶段约 12s、规范化阶段约 9s。
#: 优化阶段拿 22.5s（余量 87%），规范化阶段拿**墙钟剩余**（正常情况下 18s，
#: 保底 25% = 7.5s）—— 规范化只有墙钟上限按剩余算，因为它排在最后，
#: 剩多少就是多少，不存在「下一阶段」要留。
#:
#: 为什么余量要留这么足：带 coverage 插桩跑同一个算例，优化阶段偶发从 12s 涨到
#: 18s 以上（实测），预算切得紧就会被截断成 FEASIBLE。
OPTIMIZE_BUDGET_RATIO: Final[float] = 0.75

#: 「确定性时间」上限相对墙钟上限的倍数。
#:
#: CP-SAT 的 deterministic time **不是秒**，是一个与机器无关的工作量单位，
#: 它与墙钟的比值随 worker 数变化（本模型 4 worker 下实测约 1.2~1.7 倍）。
#: 早先把它直接设成墙钟秒数，结果是**它先到**、把本来能证到最优的求解切断
#: —— 集成测试里基准周因此偶发 FEASIBLE 且方案不一致。现在放宽到 3 倍，
#: 让它只当兜底：正常情况下两个上限都不会触发（求解靠证明结束）。
DET_TIME_SLACK: Final[float] = 3.0

#: CP-SAT 状态 → 三态（铁律 8：UNKNOWN 与 INFEASIBLE 在类型层就分开）
_STATUS_MAP: Final[dict[cp_model.CpSolverStatus, SolveStatus]] = {
    cp_model.OPTIMAL: "OPTIMAL",
    cp_model.FEASIBLE: "FEASIBLE",
    cp_model.INFEASIBLE: "INFEASIBLE",
    cp_model.UNKNOWN: "UNKNOWN",
    cp_model.MODEL_INVALID: "MODEL_INVALID",
}


def map_status(raw: cp_model.CpSolverStatus) -> SolveStatus:
    return _STATUS_MAP.get(raw, "UNKNOWN")


@dataclass(frozen=True)
class StageResult:
    """一个求解阶段的结果。"""

    name: str
    status: SolveStatus
    objective: float | None
    best_bound: float | None
    wall_time_s: float
    fixed_at: int | None = None


@dataclass
class SolveRun:
    """分阶段求解的完整产物。"""

    status: SolveStatus
    stages: tuple[StageResult, ...]
    selected: tuple[int, ...]
    starts: Mapping[int, int]
    runways: Mapping[int, str]
    objective_value: float | None
    best_bound: float | None
    gap: float | None
    wall_time_s: float
    num_branches: int
    num_conflicts: int
    log_lines: tuple[str, ...] = field(default_factory=tuple)
    satisfied: Mapping[str, bool] = field(default_factory=dict)

    @property
    def has_solution(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE")


def make_solver(
    *,
    seed: int,
    workers: int,
    time_limit_s: float,
    deterministic_limit: float | None = None,
    capture_log: bool = False,
) -> tuple[cp_model.CpSolver, list[str]]:
    """按 v6 §3.11 配置求解器。

    `random_seed` 与 `num_workers` **都是可复现性的组成部分**：CP-SAT 的多线程
    搜索在不同 worker 数下可能返回不同的等价最优解，所以两者都要随 `SolverStats`
    落库并进 manifest（§10.6）。

    ## 为什么同时设墙钟与「确定性时间」两个上限

    铁律 9 要求同输入 + 同 seed + 同 worker 数 → 逐字节可复现。CP-SAT 的并行搜索
    本身是按**确定性时间**同步的，所以只要求解是**靠证明结束**的，结果就可复现；
    可一旦是**靠墙钟超时**结束的，切在哪一刀就取决于这台机器当时有多忙 ——
    同一份输入在快机器上和慢机器上会得到两个不同的解。

    所以这里两个上限都设：`max_time_in_seconds` 守 §3.11 的预算承诺（30/120/300s
    一秒不能超），`max_deterministic_time` 让**真正切下去的那一刀是确定性的**。
    正常机器上后者先到，于是超时截断也可复现。

    ⚠️ **踩过的两个坑，都与这两个上限有关：**

    1. **不能按「总预算 − 已耗墙钟」去算下一阶段的确定性上限。** 那个减数随机器
       负载抖动，于是确定性上限本身每次都不同，切在哪儿也就每次都不同 ——
       实测三次连跑得到三个不同的 `content_sha256`。所以预算按**固定比例**预先切开
       （见 :func:`solve_staged`），不按已耗时动态切。
    2. **确定性时间不是秒。** 它是与机器无关的工作量单位，本模型 4 worker 下实测
       约为墙钟的 1.2~1.7 倍。早先把它直接设成墙钟秒数，结果它先到、把本来能证到
       最优的求解切断了。现在按 `DET_TIME_SLACK` 放宽，让它只当兜底。

    **可复现性的真实边界**：靠证明结束的求解（`OPTIMAL`）逐字节可复现；靠上限
    截断的求解（`FEASIBLE`）不保证 —— 而 `FEASIBLE` 这个状态本身就在说
    「这不是最优解」，不会让人误以为拿到了唯一答案。
    """
    solver = cp_model.CpSolver()
    solver.parameters.random_seed = seed
    solver.parameters.num_workers = workers
    solver.parameters.max_time_in_seconds = max(0.01, time_limit_s)
    solver.parameters.max_deterministic_time = max(
        0.01,
        time_limit_s * DET_TIME_SLACK if deterministic_limit is None else deterministic_limit,
    )
    # 关掉 presolve 探测：本模型里 probing 每次要花 ~4~6 秒，而分阶段求解会
    # 反复调 Solve()，那笔开销要付四遍。实测基准周 probing_level=2 → 11.3s、
    # =0 → 5.1s，最优值一模一样。probing 只影响搜索效率，不影响可行集与最优值。
    solver.parameters.cp_model_probing_level = 0
    lines: list[str] = []
    if capture_log:
        solver.parameters.log_search_progress = True
        solver.log_callback = lines.append
    return solver, lines


# ─────────────────────────────────────────────────────────────────────
# 各阶段目标表达式
# ─────────────────────────────────────────────────────────────────────
def stage1_progress(built: BuiltModel) -> cp_model.LinearExpr | None:
    """阶段1：进度完成度 = `Σ w_req · sat[req]`（松弛档下才非常量）。"""
    if not built.satisfied:
        return None
    terms: list[cp_model.LinearExpr] = []
    for req in built.cset.requirements:
        lit = built.satisfied.get(req.req_id)
        if lit is None:
            continue
        terms.append(round(req.weight) * lit)
    if not terms:
        return None
    return cp_model.LinearExpr.sum(terms)


def stage2_hamming(
    built: BuiltModel, prev_selected: Sequence[int] | None
) -> cp_model.LinearExpr | None:
    """阶段2：与上一版方案的汉明距离 `Σ |x[c] − x_prev[c]|`（局部重排的核心）。"""
    if prev_selected is None:
        return None
    prev = set(prev_selected)
    terms: list[cp_model.LinearExpr] = []
    for idx, var in enumerate(built.x):
        terms.append((1 - var) if idx in prev else var)
    if not terms:
        return None
    return cp_model.LinearExpr.sum(terms)


def _peak(
    model: cp_model.CpModel,
    counts: Sequence[cp_model.LinearExpr],
    name: str,
    ub: int,
    *,
    subsets: Sequence[Sequence[int]] = (),
) -> cp_model.IntVar | None:
    """峰值负荷 `max(counts)`：方差的线性替身（见模块文档）。

    除 `AddMaxEquality` 之外还补一族**冗余但线性**的均值下界：对任意子集 S，
    `|S| · peak ≥ Σ_{i∈S} counts[i]`（峰值不低于任何子集的平均值）。
    `AddMaxEquality` 对 LP 松弛几乎不提供下界，这一族一摆上去，阶段3b 的下界就
    直接给足。它们是蕴含式，不改变可行集。

    `subsets` 用来传**有意义的子集**。基准周的例子很实在：机队峰值若只按全 8 架
    取平均，下界是 `14/8 → 2`；但学员没有 JL-9 机型资质，14 个架次全落在 6 架 JL-8
    上，按机型子集取平均才得到正确下界 `14/6 → 3`。差这 1，阶段3b 就从「秒级证明」
    变成「18 秒证不完」。
    """
    if len(counts) < 2:
        return None
    hi = model.new_int_var(0, ub, f"peak_{name}")
    model.add_max_equality(hi, counts)
    groups: list[Sequence[int]] = [range(len(counts)), *subsets]
    for group in groups:
        members = [counts[i] for i in group]
        if len(members) >= 1:
            model.add(len(members) * hi >= cp_model.LinearExpr.sum(members))
    return hi


def _sortie_upper_bound(built: BuiltModel) -> int:
    """本周架次数的上界：每个人受约束11 的周上限，取与其时隙数的较小值再求和。

    只用于推导阶段3 的权重量级（见 :func:`stage3_preferences`），不是约束。
    """
    slots_of: dict[str, int] = {}
    for slot in built.cset.slots:
        slots_of[slot[0]] = slots_of.get(slot[0], 0) + 1
    total = 0
    for person_id, count in sorted(slots_of.items()):
        identity = built.data.persons[person_id].identity
        total += min(count, built.ruleset.weekly_sortie_cap(identity))
    return max(1, total)


@dataclass(frozen=True)
class PreferenceTerms:
    """阶段3 的合成目标与它的三个分量。

    分量单独留出来是给**规范化**那一步用的：把每个分量各自钉在最优值上，
    比把「合成目标 == 某个八位数」这一条巨大的线性等式摆上去好得多 ——
    后者传播不动，单线程重解连可行解都找不到（实测 18s 一无所获）。
    """

    combined: cp_model.LinearExpr | None
    components: tuple[tuple[str, cp_model.LinearExpr], ...]


def stage3_preferences(built: BuiltModel) -> PreferenceTerms:
    """阶段3：架次总量 + 三项均衡 + 早飞偏好，**一个带权和式**（v6 §3.7 原样）。

    ## 权重不是拍的，是按「词典序等价」算出来的

    v6 §3.7 把阶段3 写成一个和式但没给权重。任意拍三个数会让语义变得没法解释
    （「教员多带一次」和「某架次晚起飞 20 分钟」谁更贵？）。这里反过来做：
    **先定优先次序，再算出保证该次序的最小权重**。

    设三项按优先级从高到低为 `t1`（架次总量）、`t2`（三项峰值负荷之和）、
    `t3`（起飞时刻之和），各自取值范围上界为 `R1/R2/R3`，则

        w3 = 1
        w2 = 1 + R3·w3
        w1 = 1 + R2·w2 + R3·w3

    此时 `w1·t1 + w2·t2 + w3·t3` 的最优解集合与「先最小化 t1、再 t2、再 t3」
    的完全相同 —— 因为低优先级项的**全幅变动**都换不来高优先级项的一个单位。
    上界 `R*` 从模型自身算（约束11 的周上限、训练窗长度），不是估的。

    ## 为什么最后合成一个和式，而不是分三次 Solve

    分阶段求解每一级都要重跑一遍 presolve，而 §3.11 的 30s 预算是**整个请求**的。
    实测基准周分三级要 22~27s（3a 6~10s + 3b 12~14s + 3c 3~9s），一旦某级被预算
    切断，切在哪儿就取决于前几级用掉多少 —— 那个「多少」是墙钟量，于是同一份输入
    连跑三次得到三个不同的 `content_sha256`，**直接违反铁律 9**。

    合成一个和式之后只有一次 Solve、一次 presolve，30s 预算完整给它，
    可复现性也不再依赖阶段间的预算分配。真正让它能证到最优的是三处**蕴含下界**
    （`_peak` 的子集均值、`_post_lateness_bound` 的割线族、`model.py` 里的架次数下界），
    它们让 LP 松弛一上手就拿到正确下界。

    阶段1（进度完成度）与阶段2（汉明距离）仍是**独立阶段**：它们与阶段3 之间是真正的
    词典序关系（进度优先于扰动、扰动优先于偏好），且各自的取值范围不像三项偏好那样
    容易界定。
    """
    model = built.model
    data = built.data
    cset = built.cset
    w_balance = built.spec.objective_weights.balance

    count_ub = _sortie_upper_bound(built)
    ub = count_ub
    peaks: list[cp_model.LinearExpr] = []

    # ① 架次总量（本实现对 §3.7 的补充，理由见模块文档）
    present = [built.slot_present[slot] for slot in cset.slots]
    count_expr = cp_model.LinearExpr.sum(present) if present else None

    # ② 教员负荷不均衡
    instructor_loads: list[cp_model.LinearExpr] = []
    for person_id, person in sorted(data.persons.items()):
        if person.identity != IDENTITY_INSTRUCTOR:
            continue
        idxs = [i for i, c in enumerate(cset.candidates) if c.instructor_id == person_id]
        if idxs:
            instructor_loads.append(cp_model.LinearExpr.sum([built.x[i] for i in idxs]))
    peak = _peak(model, instructor_loads, "instr", ub)
    if peak is not None:
        peaks.append(peak)

    # ③ 机队利用不均衡（按机型分组另给一族均值下界，见 `_peak` 文档）
    fleet_loads: list[cp_model.LinearExpr] = []
    by_type: dict[str, list[int]] = {}
    for aircraft_id in sorted(data.aircraft):
        idxs = [i for i, c in enumerate(cset.candidates) if c.aircraft_id == aircraft_id]
        if idxs:
            by_type.setdefault(data.aircraft[aircraft_id].aircraft_type, []).append(
                len(fleet_loads)
            )
            fleet_loads.append(cp_model.LinearExpr.sum([built.x[i] for i in idxs]))
    peak = _peak(
        model,
        fleet_loads,
        "fleet",
        ub,
        subsets=[members for _t, members in sorted(by_type.items())],
    )
    if peak is not None:
        peaks.append(peak)

    # ④ 跑道使用不均衡（S-05 引入的自由度）
    runway_loads: list[cp_model.LinearExpr] = []
    for runway_id in sorted(built.spec.runways):
        lits = [
            built.slot_runway[slot][runway_id]
            for slot in cset.slots
            if runway_id in built.slot_runway[slot]
        ]
        if lits:
            runway_loads.append(cp_model.LinearExpr.sum(lits))
    peak = _peak(model, runway_loads, "runway", ub)
    if peak is not None:
        peaks.append(peak)

    peaks_expr = cp_model.LinearExpr.sum(peaks) if peaks and w_balance > 0 else None
    peaks_ub = len(peaks) * count_ub if peaks else 0

    # ⑤ 早飞偏好惩罚：被安排的架次越晚起飞代价越高
    late_expr: cp_model.LinearExpr | None = None
    late_ub = 0
    horizon = max(1, data.horizon_minutes)
    if w_balance > 0:
        late_by_day: dict[int, list[cp_model.IntVar]] = {}
        for slot in cset.slots:
            eff = model.new_int_var(0, horizon, f"late_{slot[0]}_{slot[1]}_d{slot[2]}")
            model.add(eff == built.slot_start[slot]).only_enforce_if(built.slot_present[slot])
            model.add(eff == 0).only_enforce_if(built.slot_present[slot].Not())
            late_by_day.setdefault(slot[2], []).append(eff)
        late_terms = [eff for day in sorted(late_by_day) for eff in late_by_day[day]]
        if late_terms:
            _post_lateness_bound(built, late_by_day)
            late_expr = cp_model.LinearExpr.sum(late_terms)
            late_ub = count_ub * horizon

    # ⑥ 按优先级算出词典序等价的权重（见函数文档的推导）
    w_late = 1
    w_peaks = 1 + late_ub * w_late
    w_count = 1 + peaks_ub * w_peaks + late_ub * w_late

    terms: list[cp_model.LinearExpr] = []
    components: list[tuple[str, cp_model.LinearExpr]] = []
    if count_expr is not None:
        terms.append(w_count * count_expr)
        components.append(("架次总量", count_expr))
    if peaks_expr is not None:
        terms.append(w_peaks * peaks_expr)
        components.append(("负荷峰值之和", peaks_expr))
    if late_expr is not None:
        terms.append(w_late * late_expr)
        components.append(("起飞时刻之和", late_expr))
    return PreferenceTerms(
        combined=cp_model.LinearExpr.sum(terms) if terms else None,
        components=tuple(components),
    )


def _post_lateness_bound(
    built: BuiltModel, late_by_day: Mapping[int, Sequence[cp_model.IntVar]]
) -> None:
    """给阶段3c 补一族**逻辑蕴含**的线性下界，否则它证不到最优。

    ## 推导

    约束9 的 7 分钟间隔是**全场统一**的（D-2），所以同一天的 k 个起飞时刻排序后
    满足 `t_i ≥ t_1 + sep·(i−1) ≥ sep·(i−1)`，于是

        Σ_i t_i ≥ f(k) := sep·k(k−1)/2

    `f` 在整数上是凸的，用它在每个整点 k 的**割线**给出一族线性下界：

        Σ_i t_i ≥ f(k) + sep·k·(n_d − k)      （对任意整数 k ≥ 0）

    正确性：令 `m = n_d − k`，两边相减得 `sep·m(m−1)/2 ≥ 0`，对任意整数 m 成立。
    所以整族割线都是合法下界，且它们的上包络在整点上就等于 `f`。

    ## 为什么写成割线而不是「`n_d ≥ k` 则 `Σt ≥ f(k)`」

    先写的是后者（reified 版），**没用**：enforcement literal 形式的约束进不了 LP
    松弛，实测下界只到 5（真最优 49）。割线是**纯线性**约束，LP 直接吃进去 ——
    每天取 k=2 那条割线（`Σt_d ≥ 2·sep·n_d − sep`）在全周求和即给出 49。

    只在常规求解里下这族约束 —— 诊断模式下约束9 会被 assumption literal 关掉，
    那时这条蕴含不再成立。
    """
    if built.diagnose:
        return
    separation = built.ruleset.separation_minutes
    if separation <= 0:
        return
    for day, effs in sorted(late_by_day.items()):
        slots_of_day = [slot for slot in built.cset.slots if slot[2] == day]
        present_sum = cp_model.LinearExpr.sum([built.slot_present[slot] for slot in slots_of_day])
        late_sum = cp_model.LinearExpr.sum(list(effs))
        for k in range(1, min(len(effs), _LATENESS_BOUND_MAX_K) + 1):
            intercept = separation * k * (k - 1) // 2 - separation * k * k
            built.model.add(late_sum >= separation * k * present_sum + intercept)


def canonical_tiebreak(built: BuiltModel) -> cp_model.LinearExpr:
    """规范化目标：`Σ (i+1)·x[i]`，i 为候选在 `Candidate.sort_key` 下的序号。

    最小化它 = 在等价最优解中挑**字典序最小**的那一个
    （按 课目 → 天 → 受训人 → 教员 → 机号 排序）。
    """
    return cp_model.LinearExpr.sum([(i + 1) * var for i, var in enumerate(built.x)])


# ─────────────────────────────────────────────────────────────────────
# 分阶段求解
# ─────────────────────────────────────────────────────────────────────
def solve_staged(
    built: BuiltModel,
    *,
    prev_selected: Sequence[int] | None = None,
    capture_log: bool = False,
) -> SolveRun:
    """按 §3.7 分阶段求解 + 规范化，返回三态严格分离的结果。

    ## 预算怎么分（§3.11 + 铁律 9）

    总预算取 `ConstraintSpec.solver_time_limit_s`（30/120/300s）。**按固定比例
    预先切开，不按「已经用掉多少」动态切**：

    - 优化阶段合计拿 `OPTIMIZE_BUDGET_RATIO`（75%），在活跃阶段间**均分**；
    - 规范化阶段拿墙钟剩余，确定性上限按剩下的 25% 算。

    动态切法（每个阶段拿「总预算 − 已耗时」）看起来更省，实际上是个陷阱：
    那个「已耗时」无论用墙钟还是用 CP-SAT 的 `deterministic_time` 都会在多线程
    下抖动（worker 被打断的位置不一样），于是下一阶段的时限每次都不同，
    切在哪儿就每次都不同 —— **实测同一份输入连跑三次得到三个
    `content_sha256`**。固定比例难看但确定。用不完的时间就浪费掉，值。

    ## 为什么最后要单线程再解一次

    见 :func:`canonicalize`。一句话：CP-SAT 多线程下**不保证**返回同一个等价最优解。

    超时行为严格照 §3.6：有可行解 → 输出并标注非最优（`FEASIBLE`）；
    无解且未证明不可行 → `UNKNOWN`。**绝不把 UNKNOWN 说成 INFEASIBLE。**
    """
    spec = built.spec
    model = built.model
    total_budget = spec.solver_time_limit_s
    started = _time.monotonic()
    stages: list[StageResult] = []
    log_lines: list[str] = []
    last_solver: cp_model.CpSolver | None = None
    status: SolveStatus = "UNKNOWN"
    hint_vars = _hint_vars(built)

    prefs = stage3_preferences(built)
    plan: list[tuple[str, cp_model.LinearExpr | None, bool]] = [
        ("阶段1 进度完成度", stage1_progress(built), True),
        ("阶段2 汉明距离", stage2_hamming(built, prev_selected), False),
        ("阶段3 均衡与偏好", prefs.combined, False),
    ]
    active: list[tuple[str, cp_model.LinearExpr | None, bool]] = [
        (name, expr, maximize) for name, expr, maximize in plan if expr is not None
    ]
    if not active:
        active = [("可行性", None, False)]

    per_stage = total_budget * OPTIMIZE_BUDGET_RATIO / len(active)
    proved_all = True

    for name, expr, maximize in active:
        wall_left = total_budget - (_time.monotonic() - started)
        if wall_left <= 0:
            stages.append(StageResult(name, "UNKNOWN", None, None, 0.0, None))
            proved_all = False
            break

        model.clear_objective()  # type: ignore[no-untyped-call]
        if expr is not None:
            if maximize:
                model.maximize(expr)
            else:
                model.minimize(expr)

        solver, lines = make_solver(
            seed=spec.solver_seed,
            workers=spec.solver_workers,
            time_limit_s=min(per_stage, wall_left),
            deterministic_limit=per_stage * DET_TIME_SLACK,
            capture_log=capture_log,
        )
        raw = solver.solve(model)
        stage_status = map_status(raw)
        log_lines.extend(lines)

        objective = solver.objective_value if stage_status in ("OPTIMAL", "FEASIBLE") else None
        bound = solver.best_objective_bound if stage_status in ("OPTIMAL", "FEASIBLE") else None
        fixed_at: int | None = None

        if stage_status in ("OPTIMAL", "FEASIBLE"):
            last_solver = solver
            if expr is not None:
                fixed_at = round(solver.objective_value)
                # 把本阶段最优值固定为约束，交给下一阶段（分阶段求解的核心）
                model.add(expr == fixed_at)
            # 本阶段的解在下一阶段仍然可行，拿它当 warm start。
            # 提示不改变可行集，只影响搜索顺序。
            model.clear_hints()  # type: ignore[no-untyped-call]
            for var in hint_vars:
                model.add_hint(var, solver.value(var))
        stages.append(
            StageResult(
                name=name,
                status=stage_status,
                objective=objective,
                best_bound=bound,
                wall_time_s=solver.wall_time,
                fixed_at=fixed_at,
            )
        )
        status = stage_status
        if stage_status != "OPTIMAL":
            proved_all = False
        if stage_status in ("INFEASIBLE", "MODEL_INVALID", "UNKNOWN"):
            break
        if stage_status == "FEASIBLE":
            break  # 超时但有解：不再往下压阶段，如实标注非最优

    model.clear_objective()  # type: ignore[no-untyped-call]

    # ── 规范化（铁律 9 的落点）─────────────────────────────────────
    if last_solver is not None:
        canon_wall = total_budget - (_time.monotonic() - started)
        canon_budget = total_budget * (1.0 - OPTIMIZE_BUDGET_RATIO)
        canon = canonicalize(
            built,
            prefs,
            reference=last_solver,
            wall_left=canon_wall,
            det_budget=canon_budget * DET_TIME_SLACK,
            capture_log=capture_log,
        )
        if canon is not None:
            solver, stage, lines = canon
            last_solver = solver
            stages.append(stage)
            log_lines.extend(lines)
            if stage.status != "OPTIMAL":
                proved_all = False

    if last_solver is None:
        return SolveRun(
            status=status,
            stages=tuple(stages),
            selected=(),
            starts={},
            runways={},
            objective_value=None,
            best_bound=None,
            gap=None,
            wall_time_s=_time.monotonic() - started,
            num_branches=0,
            num_conflicts=0,
            log_lines=tuple(log_lines),
        )

    selected = tuple(i for i, var in enumerate(built.x) if last_solver.value(var) == 1)
    starts = {i: int(last_solver.value(built.start_of(i))) for i in selected}
    runways: dict[int, str] = {}
    for i in selected:
        for runway_id, lit in sorted(built.runway_of(i).items()):
            if last_solver.value(lit) == 1:
                runways[i] = runway_id
                break
    satisfied = {
        req_id: last_solver.value(lit) == 1 for req_id, lit in sorted(built.satisfied.items())
    }
    # 统计口径取最后一个**真的求出解**的优化阶段。规范化阶段没有业务目标值，
    # 不该顶替它。
    solved = [
        st
        for st in stages
        if st.status in ("OPTIMAL", "FEASIBLE") and not st.name.startswith("规范化")
    ]
    final = solved[-1] if solved else stages[-1]
    gap = None
    if final.objective is not None and final.best_bound is not None:
        denom = max(1.0, abs(final.objective))
        gap = abs(final.objective - final.best_bound) / denom

    return SolveRun(
        status="OPTIMAL" if proved_all else "FEASIBLE",
        stages=tuple(stages),
        selected=selected,
        starts=starts,
        runways=runways,
        objective_value=final.objective,
        best_bound=final.best_bound,
        gap=gap,
        wall_time_s=_time.monotonic() - started,
        num_branches=last_solver.num_branches,
        num_conflicts=last_solver.num_conflicts,
        log_lines=tuple(log_lines),
        satisfied=satisfied,
    )


def _hint_vars(built: BuiltModel) -> list[cp_model.IntVar]:
    return [
        *built.x,
        *[built.slot_start[slot] for slot in built.cset.slots],
        *[built.slot_present[slot] for slot in built.cset.slots],
        *[lit for slot in built.cset.slots for _r, lit in sorted(built.slot_runway[slot].items())],
    ]


def canonicalize(
    built: BuiltModel,
    prefs: PreferenceTerms,
    *,
    reference: cp_model.CpSolver,
    wall_left: float,
    det_budget: float,
    capture_log: bool = False,
) -> tuple[cp_model.CpSolver, StageResult, list[str]] | None:
    """把「返回哪一个等价最优解」变成确定性的（铁律 9）。

    ## 这是本窗口最反直觉的一个实测结论

    **CP-SAT 在固定 `random_seed` + 固定 `num_workers` 下，依然不保证返回同一个
    等价最优解。** v6 §3.11 写的「同 seed + 同 num_search_workers → 逐字节可复现」
    在 OR-Tools 9.15 上**不成立**。实测证据（同一份模型 proto，逐字节相同）：

    | workers | 3 次连跑的状态 | 目标值 | 不同解的个数 | 胜出的子求解器 |
    |---|---|---|---|---|
    | 1 | FEASIBLE ×3 | 都是 82782140 | **1** | `main` |
    | 4 | OPTIMAL ×3 | 都是 82782100 | **3** | `default_lp` / `scheduling_intervals_lns` / `rins_pump_lns` |

    最优**值**是确定的（82782100 三次一样），但最优**解**不是 —— 哪个 worker 先
    撞上一个等价最优解取决于线程调度。单线程则完全确定。

    ## 所以分两段

    1. 多线程（`spec.solver_workers`，§3.11 要求的 4）负责**把最优值找出来并证明**；
    2. 单线程负责**在等价最优解里挑一个确定的**：把三个偏好分量各自钉在第 1 段
       求得的值上，清掉 warm start 提示（提示会把第 1 段的随机性带进来），
       然后单线程重解一次。

    第 2 段是**纯可行性**问题（不带目标）：钉住分量之后可行域已经很紧，实测基准周
    8 秒左右解完，且三次连跑逐字节一致。曾经试过在第 2 段最小化字典序 tie-break
    目标 `Σ(i+1)·x[i]`，同样确定但要吃掉整整 30 秒预算 —— 确定性并不需要它。

    ## 分量要**逐个**钉，不能钉合成目标

    钉「合成目标 == 那个八位数」这一条巨大的线性等式传播不动，单线程 18 秒连可行解
    都找不到。逐个钉 `架次总量 == 14`、`峰值之和 == 13`、`起飞时刻之和 == 49`
    则约束紧、传播强。三个分量的值由合成目标唯一确定（权重是词典序分离的），
    所以「钉分量」与「钉合成目标」在语义上等价。
    """
    if wall_left <= 0 or det_budget <= 0 or not prefs.components:
        return None
    model = built.model
    model.clear_objective()  # type: ignore[no-untyped-call]
    model.clear_hints()  # type: ignore[no-untyped-call]
    for _label, component in prefs.components:
        model.add(component == reference.value(component))
    solver, lines = make_solver(
        seed=built.spec.solver_seed,
        workers=1,
        time_limit_s=wall_left,
        deterministic_limit=det_budget,
        capture_log=capture_log,
    )
    raw = solver.solve(model)
    status = map_status(raw)
    if status not in ("OPTIMAL", "FEASIBLE"):
        return None
    return (
        solver,
        StageResult(
            name="规范化 单线程重解（铁律 9：可复现性）",
            status=status,
            objective=None,
            best_bound=None,
            wall_time_s=solver.wall_time,
            fixed_at=None,
        ),
        lines,
    )


__all__ = [
    "BASE_MISSION_WEIGHT",
    "DEBT_FACTOR",
    "DET_TIME_SLACK",
    "OPTIMIZE_BUDGET_RATIO",
    "PreferenceTerms",
    "SolveRun",
    "StageResult",
    "canonical_tiebreak",
    "canonicalize",
    "make_solver",
    "map_status",
    "solve_staged",
    "stage1_progress",
    "stage2_hamming",
    "stage3_preferences",
]
