"""边界场景的「恰好」标定（v6 §12.3「资源恰好够 / 恰好差 1 架次」）。

## 「恰好」怎么判定 —— 成对定义、互为证明

「恰好」这个词单看一个场景是判不出来的：说「资源恰好够」，必须同时回答
「再少一点会怎样」。所以本模块把边界场景**成对**构造：

> 取一个**单调收紧的旋钮**（教员人数、可用飞机数、训练窗长度、空域容量、
> 跑道条数、可用天数…），沿这个旋钮找到**临界档** `L*`：
>
> - **恰好够** = 旋钮拧到 `L*` 的那个场景 —— 它可解；
> - **恰好差 1** = 旋钮拧到 `L*+1` 的那个场景 —— 它不可解。
>
> 两个场景都进 200 场景测试集。于是「恰好」这个性质**由这一对的两个求解结果
> 直接判定**，不需要任何额外断言，也不依赖任何人的主观判断：
> `enough` 解出来了 ∧ `short` 判了 INFEASIBLE ⟺ `L*` 确实是临界档。

「差 1」是**差 1 格旋钮**（少 1 名教员 / 少 1 架飞机 / 窗口短 1 小时 /
容量少 1 / 少 1 天…），不是「差 1 个架次」—— 后者在资源维度上没有对应的操作：
架次数是求解的产物，不是可以拧的输入。这一点在收工报告 §4 里单列说明。

## 标定用「只判可行性」的求解

`find_conflict_core` 建的是**不带目标函数**的可行性模型（v6 §3.9），基准周实测
约 2 秒，而完整求解要 20 秒（其中 8 秒是铁律 9 要求的单线程规范化阶段）。标定要
跑上百次，用完整求解不划算。

⚠️ **标定结果不是实测指标**：200 场景正式运行时，每个边界场景仍然走完整的
`solve()`，`enough` / `short` 的状态以那一次为准（见
:mod:`tests.scenarios.runner`）。标定只负责**找到临界档**。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.nodes.compile_spec import compile_spec
from backend.solver.candidates import enumerate_candidates
from backend.solver.diagnose import find_conflict_core
from tests.scenarios.catalog import Entities, OverrideSpec, ScenarioCase

#: 标定时可行性求解的时限（秒）。基准周实测 2s，给足 10 倍余量。
CALIBRATION_TIME_LIMIT_S: float = 60.0


@dataclass(frozen=True)
class Knob:
    """一个**单调收紧**的资源旋钮。`level=0` 恒为「无扰动」。"""

    knob_id: str
    title: str
    unit: str
    max_level: int
    build: Callable[[Entities, int], OverrideSpec]

    def spec(self, ents: Entities, level: int) -> OverrideSpec:
        return self.build(ents, level)


def _last_days(ents: Entities, k: int) -> tuple[str, ...]:
    return tuple(d.isoformat() for d in ents.days()[-k:]) if k else ()


def _maint(ents: Entities, aircraft_ids: Sequence[str]) -> OverrideSpec:
    days = ents.days()
    return OverrideSpec(
        maintenance_all_day=tuple(
            (aid, days[0].isoformat(), days[-1].isoformat()) for aid in sorted(aircraft_ids)
        )
    )


def candidate_knobs(ents: Entities) -> list[Knob]:
    """候选旋钮。取前 20 个**确实存在临界档**的进边界场景集。"""
    knobs: list[Knob] = []

    knobs.append(
        Knob(
            "instructors_out",
            "整周不可用的教员人数",
            "人",
            len(ents.instructors),
            lambda e, k: OverrideSpec(unavailable_all_week=tuple(sorted(e.instructors)[:k])),
        )
    )
    student_fleet = sorted({aid for t in ents.student_types for aid in ents.aircraft_of_type(t)})
    knobs.append(
        Knob(
            "student_fleet_down",
            "整周维护的「学员机型」飞机数",
            "架",
            len(student_fleet),
            lambda _e, k: _maint(ents, student_fleet[:k]),
        )
    )
    all_fleet = sorted(aid for aid, _t in ents.aircraft)
    knobs.append(
        Knob(
            "fleet_down",
            "整周维护的飞机总数",
            "架",
            len(all_fleet),
            lambda _e, k: _maint(ents, all_fleet[:k]),
        )
    )
    knobs.append(
        Knob(
            "window_end_hours",
            "训练窗尾部缩短的小时数",
            "小时",
            11,
            lambda _e, k: OverrideSpec(window_end=f"{18 - k:02d}:00"),
        )
    )
    knobs.append(
        Knob(
            "window_start_hours",
            "训练窗头部推迟的小时数",
            "小时",
            11,
            lambda _e, k: OverrideSpec(window_start=f"{6 + k:02d}:00"),
        )
    )
    for airspace_id, capacity in ents.airspaces:
        knobs.append(
            Knob(
                f"airspace_{airspace_id}",
                f"空域 {airspace_id} 容量下调格数（基准 {capacity}）",
                "格",
                capacity,
                lambda _e, k, a=airspace_id, c=capacity: OverrideSpec(  # type: ignore[misc]
                    airspace_capacity={a: max(0, c - k)}
                ),
            )
        )
    knobs.append(
        Knob(
            "runways_closed",
            "关闭的跑道条数",
            "条",
            len(ents.runways),
            lambda e, k: OverrideSpec(closed_runways=tuple(rid for rid, _t in e.runways)[:k]),
        )
    )
    knobs.append(
        Knob(
            "everyone_off_days",
            "全员不可用的天数（从周末往前推）",
            "天",
            7,
            lambda e, k: OverrideSpec(
                unavailable={pid: _last_days(e, k) for pid, _n, _i in e.persons} if k else {}
            ),
        )
    )
    knobs.append(
        Knob(
            "students_off_days",
            "全部学员不可用的天数",
            "天",
            7,
            lambda e, k: OverrideSpec(
                unavailable={pid: _last_days(e, k) for pid in e.students} if k else {}
            ),
        )
    )
    knobs.append(
        Knob(
            "instructors_off_days",
            "全部教员不可用的天数",
            "天",
            7,
            lambda e, k: OverrideSpec(
                unavailable={pid: _last_days(e, k) for pid in e.instructors} if k else {}
            ),
        )
    )
    for student in ents.students:
        knobs.append(
            Knob(
                f"student_off_{student}",
                f"学员 {student} 不可用的天数",
                "天",
                7,
                lambda e, k, p=student: OverrideSpec(  # type: ignore[misc]
                    unavailable={p: _last_days(e, k)} if k else {}
                ),
            )
        )
    for instructor in ents.instructors:
        knobs.append(
            Knob(
                f"instructor_off_{instructor}",
                f"教员 {instructor} 不可用的天数",
                "天",
                7,
                lambda e, k, p=instructor: OverrideSpec(  # type: ignore[misc]
                    unavailable={p: _last_days(e, k)} if k else {}
                ),
            )
        )
    knobs.append(
        Knob(
            "all_airspaces_down",
            "全部空域容量同时下调的格数",
            "格",
            max(cap for _a, cap in ents.airspaces),
            lambda e, k: OverrideSpec(
                airspace_capacity={a: max(0, cap - k) for a, cap in e.airspaces}
            ),
        )
    )
    knobs.append(
        Knob(
            "student_fleet_days",
            "「学员机型」飞机全部维护的天数",
            "天",
            7,
            lambda e, k: OverrideSpec(
                maintenance_all_day=tuple(
                    (
                        aid,
                        (e.days()[-k]).isoformat(),
                        e.days()[-1].isoformat(),
                    )
                    for aid in student_fleet
                )
                if k
                else ()
            ),
        )
    )
    knobs.append(
        Knob(
            "window_end_half_hours",
            "训练窗尾部缩短的半小时数",
            "半小时",
            23,
            lambda _e, k: OverrideSpec(
                window_end=f"{18 - (k + 1) // 2:02d}:{'30' if k % 2 else '00'}"
            ),
        )
    )
    knobs.append(
        Knob(
            "window_end_ten_min",
            "训练窗尾部缩短的 10 分钟格数（细粒度）",
            "个10分钟",
            71,
            lambda _e, k: OverrideSpec(
                window_end=f"{18 - (k * 10 + 59) // 60:02d}:{(60 - k * 10 % 60) % 60:02d}"
            ),
        )
    )
    knobs.append(
        Knob(
            "student_fleet_days_front",
            "「学员机型」飞机全部维护的天数（从周一往后推）",
            "天",
            7,
            lambda e, k: OverrideSpec(
                maintenance_all_day=tuple(
                    (aid, e.days()[0].isoformat(), e.days()[k - 1].isoformat())
                    for aid in student_fleet
                )
                if k
                else ()
            ),
        )
    )
    knobs.append(
        Knob(
            "everyone_off_days_front",
            "全员不可用的天数（从周一往后推）",
            "天",
            7,
            lambda e, k: OverrideSpec(
                unavailable={
                    pid: tuple(d.isoformat() for d in e.days()[:k]) for pid, _n, _i in e.persons
                }
                if k
                else {}
            ),
        )
    )
    knobs.append(
        Knob(
            "mature_off_days",
            "成熟飞行员不可用的天数",
            "天",
            7,
            lambda e, k: OverrideSpec(
                unavailable={p: _last_days(e, k) for p in e.persons_of("成熟飞行员")} if k else {}
            ),
        )
    )
    return knobs


@dataclass(frozen=True)
class Calibration:
    knob_id: str
    title: str
    unit: str
    critical_level: int | None
    probes: tuple[tuple[int, str], ...]

    @property
    def found(self) -> bool:
        return self.critical_level is not None


def _feasible(session: Session, ents: Entities, spec: OverrideSpec, *, snapshot_id: str) -> str:
    bundle = compile_spec(
        session,
        snapshot_id=snapshot_id,
        week_start=ents.week_start,
        overrides=spec.to_overrides(),
        materialize=False,
    )
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    core = find_conflict_core(bundle, cset, time_limit_s=CALIBRATION_TIME_LIMIT_S)
    return core.status


def calibrate_knob(session: Session, ents: Entities, knob: Knob) -> Calibration:
    """二分找临界档 `L*`：`level ≤ L*` 可解、`level > L*` 不可解。"""
    probes: list[tuple[int, str]] = []

    def probe(level: int) -> bool:
        status = _feasible(session, ents, knob.spec(ents, level), snapshot_id=ents.snapshot_id)
        probes.append((level, status))
        return status in ("OPTIMAL", "FEASIBLE")

    if probe(knob.max_level):
        # 拧到底都还可解 → 这个旋钮上不存在临界档，不能用来造边界场景
        return Calibration(knob.knob_id, knob.title, knob.unit, None, tuple(probes))
    lo, hi = 0, knob.max_level  # lo 可解（level 0 = 无扰动），hi 不可解
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if probe(mid):
            lo = mid
        else:
            hi = mid
    return Calibration(knob.knob_id, knob.title, knob.unit, lo, tuple(probes))


def boundary_cases_from(
    ents: Entities, knob: Knob, calibration: Calibration, index: int
) -> list[ScenarioCase]:
    """把一次标定变成一对边界场景。"""
    assert calibration.critical_level is not None
    level = calibration.critical_level
    pair_id = f"BD-{index:02d}"
    return [
        ScenarioCase(
            scenario_id=f"{pair_id}-E",
            category="boundary",
            family=knob.knob_id,
            title=f"资源恰好够：{knob.title} = {level} {knob.unit}（再紧 1 {knob.unit}即不可解）",
            expected_status="SOLVED",
            overrides=knob.spec(ents, level),
            pair_id=pair_id,
            pair_role="enough",
            notes=f"v6 §12.3 边界场景；临界档由 {knob.knob_id} 旋钮二分标定，探针序列 {calibration.probes}",
        ),
        ScenarioCase(
            scenario_id=f"{pair_id}-S",
            category="boundary",
            family=knob.knob_id,
            title=f"资源恰好差 1：{knob.title} = {level + 1} {knob.unit}（松 1 {knob.unit}即可解）",
            expected_status="INFEASIBLE",
            overrides=knob.spec(ents, level + 1),
            pair_id=pair_id,
            pair_role="short",
            notes=f"v6 §12.3 边界场景；与 {pair_id}-E 互为「恰好」的证明",
        ),
    ]


def calibrate_boundary(
    session: Session, ents: Entities, *, wanted: int = 20
) -> tuple[list[ScenarioCase], list[Calibration]]:
    """标定出 `wanted` 组边界对（默认 20 组 = 40 个场景）。"""
    cases: list[ScenarioCase] = []
    report: list[Calibration] = []
    index = 0
    for knob in candidate_knobs(ents):
        if len(cases) >= wanted * 2:
            break
        calibration = calibrate_knob(session, ents, knob)
        report.append(calibration)
        if not calibration.found:
            continue
        index += 1
        cases.extend(boundary_cases_from(ents, knob, calibration, index))
    return cases, report


__all__ = [
    "CALIBRATION_TIME_LIMIT_S",
    "Calibration",
    "Knob",
    "boundary_cases_from",
    "calibrate_boundary",
    "calibrate_knob",
    "candidate_knobs",
]
