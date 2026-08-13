"""M4-B 编排层的测试替身与基准夹具。

**这里的编号与姓名是测试期望值，不是代码常量**（CLAUDE.md §11）：它们描述
「基准数据集长什么样」，用来断言消解、路由、解释三层的行为。生产代码里
一个都没有。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any

from backend.harness import AgentSpec, ContextBlock
from backend.harness.types import AgentOutput, AttemptRecord, ToolResult, ValidatedCall
from backend.routing.entities import EntityDirectory
from backend.schemas.plan import CrewMember, SchedulePlan, Sortie
from backend.schemas.solver import SolverStats
from backend.schemas.validation import RULE_IDS, CheckResult, ValidationReport

#: 基准周（SPEC_DECISIONS §C.3 / v6 §1.2.3）
BASELINE_WEEK = date(2026, 1, 5)

#: v6 §1.3.1 的 8 人。**注意 P02 高超 与 P08 何超只差一个字** —— 消解层的
#: 歧义判定就是拿它俩验的。
BASELINE_PERSONS: dict[str, str] = {
    "P01": "孙军",
    "P02": "高超",
    "P03": "吴鹏",
    "P04": "刘斌",
    "P05": "罗磊",
    "P06": "张勇",
    "P07": "陈伟",
    "P08": "何超",
}

#: v6 §1.3.2 的 8 机。**AC73 是 JL-8**；JL-9 只有 AC84 / AC95。
BASELINE_AIRCRAFT: dict[str, str] = {
    "AC10": "JL-8",
    "AC27": "JL-8",
    "AC34": "JL-8",
    "AC49": "JL-8",
    "AC61": "JL-8",
    "AC73": "JL-8",
    "AC84": "JL-9",
    "AC95": "JL-9",
}

BASELINE_MISSIONS: dict[str, str] = {
    "missionA-1": "本场起落航线",
    "missionA-2": "本场起落航线",
    "missionB-1": "导航飞行",
    "missionB-2": "导航飞行",
    "missionC-1": "仪表飞行",
    "missionC-2": "仪表飞行",
    "missionD-1": "轰炸与射击",
    "missionE-1": "空战机动",
    "missionE-2": "空战机动",
    "missionF-1": "编队飞行",
    "missionG-1": "特技飞行",
    "missionH-1": "低空突防",
}

#: v6 §1.3.5：RWY-1 服务 JL-8 与 JL-9；**RWY-2 只服务 JL-8**
BASELINE_RUNWAYS: dict[str, list[str]] = {
    "RWY-1": ["JL-8", "JL-9"],
    "RWY-2": ["JL-8"],
}


def directory() -> EntityDirectory:
    return EntityDirectory(
        persons=dict(BASELINE_PERSONS),
        aircraft=dict(BASELINE_AIRCRAFT),
        missions=dict(BASELINE_MISSIONS),
    )


# ─────────────────────────────────────────────────────────────────────
# 假 Harness
# ─────────────────────────────────────────────────────────────────────
@dataclass
class FakeHarness:
    """一个只按脚本作答的 Harness 替身。

    **不继承 `Harness`**：那会把预算、ACL、录制一并拖进来，而这些在 M4-A 已经
    逐条测过。这里要测的是「编排层怎么消费 `AgentOutput`」。

    `responses` 按调用顺序出队；用完后重复最后一条（self-consistency 会连采
    n 次，脚本不必写 n 遍）。
    """

    responses: list[AgentOutput] = field(default_factory=list)
    calls: list[tuple[AgentSpec, list[ContextBlock]]] = field(default_factory=list)
    registry: Any = None
    _index: int = 0

    def call(self, agent: AgentSpec, blocks: Sequence[ContextBlock] = (), **_: Any) -> AgentOutput:
        self.calls.append((agent, list(blocks)))
        if not self.responses:
            return text_output(agent.name, "")
        index = min(self._index, len(self.responses) - 1)
        self._index += 1
        return self.responses[index]


class FakeRegistry:
    """`registry.register_many` 的最小替身（DiagnosisAgent 会调它接线）。"""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def register_many(self, handlers: dict[str, Any]) -> None:
        self.handlers.update(handlers)

    def register(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler


def text_output(
    component: str,
    text: str,
    *,
    first_pass: bool = True,
    retries: int = 0,
    degraded: bool = False,
    error_code: str = "",
) -> AgentOutput:
    """造一个「纯文本 / 结构化输出」形态的 `AgentOutput`。"""
    attempts = tuple(
        AttemptRecord(attempt=i, mode="constrained_json", failures=()) for i in range(retries + 1)
    )
    if not first_pass and attempts:
        # 首次失败：给第 0 次挂一个失败记录，让 `first_pass` 为 False
        from backend.harness.types import FailureMode, ValidationFailure

        attempts = (
            AttemptRecord(
                attempt=0,
                mode="constrained_json",
                failures=(
                    ValidationFailure(mode=FailureMode.TYPE_ERROR, message="测试构造的失败"),
                ),
            ),
            *attempts[1:],
        )
    return AgentOutput(
        component=component,  # type: ignore[arg-type]
        text=text,
        mode="constrained_json",
        attempts=attempts,
        llm_calls=len(attempts),
        degraded=degraded,
        error_code=error_code,
        prompt_version=f"{component}/system@v1",
    )


def tool_output(
    component: str,
    calls: Sequence[tuple[str, dict[str, Any]]],
    results: Sequence[Any] = (),
) -> AgentOutput:
    """造一个「工具调用」形态的 `AgentOutput`。"""
    validated = tuple(ValidatedCall(name=name, arguments=args) for name, args in calls)
    payload = tuple(
        ToolResult(tool=name, ok=True, value=value)
        for (name, _), value in zip(calls, results, strict=False)
    )
    return AgentOutput(
        component=component,  # type: ignore[arg-type]
        calls=validated,
        results=payload,
        mode="native",
        attempts=(AttemptRecord(attempt=0, mode="native", failures=()),),
        llm_calls=1,
        prompt_version=f"{component}/system@v1",
    )


def degraded_output(component: str, error_code: str = "FTS-4002") -> AgentOutput:
    return text_output(component, "", degraded=True, error_code=error_code, retries=2)


# ─────────────────────────────────────────────────────────────────────
# 假方案
# ─────────────────────────────────────────────────────────────────────
def sortie(
    sortie_id: str,
    *,
    day: int = 0,
    takeoff: time = time(8, 0),
    minutes: int = 30,
    mission_id: str = "missionC-1",
    aircraft_id: str = "AC10",
    runway_id: str = "RWY-1",
    airspace_id: str = "IFR",
    crew: Sequence[tuple[str, str]] = (("P01", "教员"), ("P06", "学员")),
) -> Sortie:
    flight_date = BASELINE_WEEK.fromordinal(BASELINE_WEEK.toordinal() + day)
    landing_minutes = takeoff.hour * 60 + takeoff.minute + minutes
    return Sortie(
        sortie_id=sortie_id,
        date=flight_date,
        weekday=("周一", "周二", "周三", "周四", "周五", "周六", "周日")[day],
        takeoff=takeoff,
        landing=time(landing_minutes // 60, landing_minutes % 60),
        mission_id=mission_id,
        mission_name=BASELINE_MISSIONS[mission_id],
        airspace_id=airspace_id,
        aircraft_id=aircraft_id,
        runway_id=runway_id,
        crew=[
            CrewMember(person_id=pid, name=BASELINE_PERSONS[pid], role=role)  # type: ignore[arg-type]
            for pid, role in crew
        ],
    )


def plan(sorties: Sequence[Sortie], *, plan_id: str = "pl_test", tier: int = 0) -> SchedulePlan:
    import hashlib

    digest = hashlib.sha256(
        "".join(sorted(s.sortie_id for s in sorties)).encode("utf-8")
    ).hexdigest()
    return SchedulePlan(
        plan_id=plan_id,
        iso_week="2026W02",
        week_start=BASELINE_WEEK,
        week_end=date(2026, 1, 11),
        snapshot_id="snap_test",
        ruleset_version="rs_1.3",
        semantics_version="sem_1.1",
        runway_model="dual_runway",
        relaxation_tier=tier,
        sorties=list(sorties),
        content_sha256=digest,
    )


def all_green_report(plan_id: str = "pl_test") -> ValidationReport:
    """14 条全绿的校验报告。"""
    return ValidationReport(
        plan_id=plan_id,
        ruleset_version="rs_1.3",
        semantics_version="sem_1.1",
        results=[
            CheckResult(
                rule_id=rid,
                rule_title=f"规则{rid}",
                passed=True,
                checked_items=3,
                duration_ms=1.0,
            )
            for rid in RULE_IDS
        ],
    )


def stats(status: str = "OPTIMAL") -> SolverStats:
    return SolverStats(
        status=status,  # type: ignore[arg-type]
        num_candidates=2276,
        num_variables=12568,
        num_constraints=37235,
        objective_value=123.0 if status in ("OPTIMAL", "FEASIBLE") else None,
        wall_time_ms=21000.0,
    )


__all__ = [
    "BASELINE_AIRCRAFT",
    "BASELINE_MISSIONS",
    "BASELINE_PERSONS",
    "BASELINE_RUNWAYS",
    "BASELINE_WEEK",
    "FakeHarness",
    "FakeRegistry",
    "all_green_report",
    "degraded_output",
    "directory",
    "plan",
    "sortie",
    "stats",
    "text_output",
    "tool_output",
]
