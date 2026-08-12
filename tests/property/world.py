"""注入用的固定小世界 + **手工排出来的**合规基线方案。

## 为什么基线方案要手工排，不用求解器出的解

`test_validator_catches_injected_violations` 的形态是「向**合法**计划注入单点违规」。
如果那份合法计划来自求解器，这条属性测的就变成了「求解器的解被改坏之后校验器能发现」
—— 求解器的偏好（总在 06:00 起飞、总挑同一架飞机）会把注入点的分布带偏，而且它与
另一条属性测试（`test_solver_output_always_passes_validator`）耦合在了一起：求解器
一旦出错，两条属性测试同时失效。

所以这里的 5 个架次是**按 v6 §3.2 的 14 条手工推出来的**（推导写在
:func:`compliant_plan` 的文档里），只依赖规格，不依赖求解器的任何选择。
`tests/property/test_injected_violations.py::test_baseline_plan_is_legal` 用
**主校验器 + naive checker 两条通道**同时确认它确实合法 —— 基线不合法的话，
后面所有注入用例都是空转。

## 世界的形状是「为了让 14 条都注得进去」设计的

| 设计 | 为了注入哪条 |
|---|---|
| `P402` 教员在第 0/1 天不可用 | C02（把某个架次的教员换成他） |
| `P413` 学员**只持 A 类资质** | C04（让他去飞 B 类） |
| `AC703` 是 `TX-2`，学员只持 `TX-1` | C05（把学员的架次换到这架） |
| `RWY-8` 只服务 `TX-1` | C09（让 `TX-2` 架次落在 RWY-8） |
| `NAV` / `BLD` 容量 = 1 | C06（同空域两个架次时间重叠） |
| `AC702` 第 5 天全天维护 | C07（把架次排进维护窗） |
| `missionA-2` 时长 90 分钟、freq 3（`req_max`=3） | C10（学员单日 3×90 = 270 > 240） |
| `missionC-1` 先修 `missionB-1`，而 `P411` 没完成 B-1 | C13（BLOCKED 的课目被排上） |

编号一律非基准取值（`P4xx` / `AC7xx` / `TX-1` / `LAC` / `RWY-7`）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta

from backend.core.ruleset import get_ruleset, get_semantics
from backend.schemas.plan import BlockedItem, CrewMember, SchedulePlan, Sortie
from backend.validator.context import ValidationContext
from tests.property.scenario import (
    SCENARIO_WEEK_START,
    TYPE_A,
    TYPE_B,
    AircraftSpec,
    AirspaceSpec,
    MissionSpec,
    PersonSpec,
    RunwaySpec,
    ScenarioSpec,
)
from tests.property.scenario import (
    ScenarioSpec as _ScenarioSpec,  # noqa: F401  (给类型标注读者一个明确的名字)
)

WEEKDAY_NAMES: tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def day(offset: int) -> date:
    return SCENARIO_WEEK_START + timedelta(days=offset)


def clock(minutes_from_six: int) -> time:
    total = 6 * 60 + minutes_from_six
    return time(total // 60, total % 60)


# ─────────────────────────────────────────────────────────────────────
# 世界
# ─────────────────────────────────────────────────────────────────────
def injection_world() -> ScenarioSpec:
    """注入用的固定世界（形状见模块文档）。"""
    both = (TYPE_A, TYPE_B)
    airspaces = (
        AirspaceSpec(airspace_id="LAC", name="小区甲", capacity=2),
        AirspaceSpec(airspace_id="LAD", name="小区乙", capacity=2),
        AirspaceSpec(airspace_id="NAV", name="航线", capacity=1),
        AirspaceSpec(airspace_id="BLD", name="仪表航线", capacity=1),
    )
    missions = (
        MissionSpec(
            mission_id="missionA-1",
            name="本场起落",
            mission_class="A",
            duration_minutes=30,
            freq_days=3,
            dual_required=False,
            weekly_required=True,
            airspace_id="LAC",
            aircraft_types=both,
            cycle_weeks=12,
        ),
        MissionSpec(
            mission_id="missionA-2",
            name="本场起落二",
            mission_class="A",
            duration_minutes=90,
            freq_days=3,
            dual_required=False,
            weekly_required=True,
            airspace_id="LAD",
            aircraft_types=both,
            cycle_weeks=12,
        ),
        MissionSpec(
            mission_id="missionB-1",
            name="导航飞行",
            mission_class="B",
            duration_minutes=40,
            freq_days=7,
            dual_required=True,
            weekly_required=False,
            airspace_id="NAV",
            aircraft_types=both,
            prereqs=(("A类", "class"),),
        ),
        MissionSpec(
            mission_id="missionC-1",
            name="仪表飞行",
            mission_class="C",
            duration_minutes=35,
            freq_days=7,
            dual_required=True,
            weekly_required=False,
            airspace_id="BLD",
            aircraft_types=both,
            prereqs=(("missionB-1", "mission"),),
        ),
    )
    all_missions = tuple(m.mission_id for m in missions)
    aircraft = (
        AircraftSpec(
            aircraft_id="AC701",
            aircraft_type=TYPE_A,
            seats=2,
            turnaround_minutes=20,
            capable_missions=all_missions,
        ),
        AircraftSpec(
            aircraft_id="AC702",
            aircraft_type=TYPE_A,
            seats=2,
            turnaround_minutes=20,
            capable_missions=all_missions,
            maintenance_days=(day(5),),
        ),
        AircraftSpec(
            aircraft_id="AC703",
            aircraft_type=TYPE_B,
            seats=2,
            turnaround_minutes=30,
            capable_missions=all_missions,
        ),
    )
    runways = (
        RunwaySpec(runway_id="RWY-7", name="跑道七", aircraft_types=both),
        RunwaySpec(runway_id="RWY-8", name="跑道八", aircraft_types=(TYPE_A,)),
    )
    persons = (
        PersonSpec(
            person_id="P401",
            name="教员甲",
            identity="教员",
            aircraft_types=both,
            levels={"A": "教员", "B": "教员", "C": "教员"},
            completed=all_missions,
        ),
        PersonSpec(
            person_id="P402",
            name="教员乙",
            identity="教员",
            aircraft_types=both,
            levels={"A": "教员", "B": "教员", "C": "教员"},
            completed=all_missions,
            unavailable=(day(0), day(1)),
        ),
        PersonSpec(
            person_id="P411",
            name="学员甲",
            identity="学员",
            aircraft_types=(TYPE_A,),
            levels={"A": "单飞", "B": "带飞", "C": "带飞"},
            completed=("missionA-1", "missionA-2"),
        ),
        PersonSpec(
            person_id="P412",
            name="学员乙",
            identity="学员",
            aircraft_types=(TYPE_A,),
            levels={"A": "单飞", "B": "带飞", "C": "带飞"},
            completed=("missionA-1", "missionA-2", "missionB-1"),
        ),
        PersonSpec(
            person_id="P413",
            name="学员丙",
            identity="学员",
            aircraft_types=(TYPE_A,),
            levels={"A": "单飞"},  # ★ 只持 A 类资质 → C04 的注入口
            completed=("missionA-1", "missionA-2"),
        ),
        PersonSpec(
            person_id="P421",
            name="成熟甲",
            identity="成熟飞行员",
            aircraft_types=both,
            levels={"A": "单飞", "B": "单飞", "C": "单飞"},
            completed=all_missions,
        ),
    )
    return ScenarioSpec(
        label="injection-world",
        week_start=SCENARIO_WEEK_START,
        persons=persons,
        aircraft=aircraft,
        missions=missions,
        airspaces=airspaces,
        runways=runways,
    )


# ─────────────────────────────────────────────────────────────────────
# 方案构造（绕过 Pydantic 契约层，好让闸门1 能被单独测到）
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SortieDraft:
    """一个架次的最小描述。`start` 是「当日 06:00 起的分钟数」。"""

    day: int
    start: int
    mission_id: str
    trainee_id: str
    aircraft_id: str
    runway_id: str
    instructor_id: str | None = None
    is_recurrent: bool = False
    #: 覆盖项（注入违规时用）：着陆时刻偏移、空域、星期
    landing_delta: int = 0
    airspace_id: str | None = None
    weekday: str | None = None


def _crew(draft: SortieDraft, ctx: ValidationContext) -> list[CrewMember]:
    members: list[CrewMember] = []
    if draft.instructor_id is not None:
        person = ctx.persons[draft.instructor_id]
        members.append(CrewMember(person_id=person.person_id, name=person.name, role="教员"))
    trainee = ctx.persons[draft.trainee_id]
    solo_role = "复训" if draft.is_recurrent else "单飞"
    role = "学员" if draft.instructor_id is not None else solo_role
    members.append(CrewMember(person_id=trainee.person_id, name=trainee.name, role=role))
    return members


def make_sortie(index: int, draft: SortieDraft, ctx: ValidationContext) -> Sortie:
    """按草稿造一个架次。**用 `model_construct` 绕过契约层** —— 闸门1 与闸门2 是
    两道独立的闸门，注入的违规样本必须能到达 `checks.py`。"""
    mission = ctx.missions[draft.mission_id]
    when = ctx.week_start + timedelta(days=draft.day)
    return Sortie.model_construct(
        sortie_id=f"S{index:06d}",
        date=when,
        weekday=draft.weekday or WEEKDAY_NAMES[when.weekday()],
        takeoff=clock(draft.start),
        landing=clock(draft.start + mission.duration_minutes + draft.landing_delta),
        mission_id=mission.mission_id,
        mission_name=mission.name,
        airspace_id=draft.airspace_id or mission.airspace_id,
        aircraft_id=draft.aircraft_id,
        runway_id=draft.runway_id,
        is_recurrent=draft.is_recurrent,
        crew=_crew(draft, ctx),
    )


def make_plan(
    drafts: Sequence[SortieDraft],
    ctx: ValidationContext,
    *,
    blocked: Sequence[BlockedItem] = (),
    plan_id: str = "PROP-PLAN",
) -> SchedulePlan:
    """把草稿列表装成 `SchedulePlan`（同样 `model_construct`）。"""
    sorties = [make_sortie(i + 1, d, ctx) for i, d in enumerate(drafts)]
    payload = [s.model_dump(mode="json") for s in sorties]
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    week_end = ctx.week_start + timedelta(days=6)
    return SchedulePlan.model_construct(
        plan_id=plan_id,
        iso_week=f"{ctx.week_start.isocalendar().year}W{ctx.week_start.isocalendar().week:02d}",
        week_start=ctx.week_start,
        week_end=week_end,
        snapshot_id=ctx.snapshot_id or "snap_prop00000",
        ruleset_version=get_ruleset().version,
        semantics_version=get_semantics().version,
        semantics_switches=get_semantics().snapshot(),
        runway_model="dual_runway",
        relaxation_tier=0,
        sorties=sorties,
        debts=[],
        blocked_items=list(blocked),
        content_sha256=digest,
    )


# ─────────────────────────────────────────────────────────────────────
# 合规基线
# ─────────────────────────────────────────────────────────────────────
#: 基线方案的 5 个架次（推导见 :func:`compliant_plan`）
BASELINE_DRAFTS: tuple[SortieDraft, ...] = (
    SortieDraft(
        day=0,
        start=0,
        mission_id="missionA-1",
        trainee_id="P411",
        aircraft_id="AC701",
        runway_id="RWY-7",
    ),
    SortieDraft(
        day=1,
        start=0,
        mission_id="missionA-2",
        trainee_id="P412",
        aircraft_id="AC701",
        runway_id="RWY-7",
    ),
    SortieDraft(
        day=2,
        start=0,
        mission_id="missionB-1",
        trainee_id="P411",
        aircraft_id="AC702",
        runway_id="RWY-7",
        instructor_id="P401",
    ),
    SortieDraft(
        day=3,
        start=0,
        mission_id="missionC-1",
        trainee_id="P412",
        aircraft_id="AC702",
        runway_id="RWY-7",
        instructor_id="P401",
    ),
    SortieDraft(
        day=4,
        start=0,
        mission_id="missionA-1",
        trainee_id="P413",
        aircraft_id="AC701",
        runway_id="RWY-7",
    ),
)

#: 基线的阻塞项：P411 的 C-1 缺先修 missionB-1（措辞按 v6 §12.3 ②）
BASELINE_BLOCKED: tuple[BlockedItem, ...] = (
    BlockedItem(
        person_id="P411",
        mission_id="missionC-1",
        reason="missionB-1 未完成",
        missing_prereqs=["missionB-1"],
    ),
)


def compliant_plan(ctx: ValidationContext) -> SchedulePlan:
    """按 v6 §3.2 手工推出来的 5 架次合规方案。

    逐条推导：

    - **约束3 + S-02 + S-13**：三名学员各需 ≥1 次 A 类 → `S1(P411)`、`S2(P412)`、
      `S5(P413)`，且 A-1/A-2 已完成 → 按 D-1 **单飞**（不带教员）；
    - **约束13**：`P411` 的 B-1 未完成、先修「A类」已达标（A-1+A-2 都完成，S-01）
      → freq 7 → 周内唯一窗口 `[0,6]` 且 S-12 截止日 = 6 → `S3` 排在第 2 天；
      `P412` 的 C-1 同理 → `S4` 排在第 3 天；`P412` 的 B-1 已完成（S-03）不受管辖；
      `P413` 只持 A 类资质，两门 A 都已完成 → 本周无频率要求；
    - **约束13 的另一半**：`P411` 的 C-1 先修（missionB-1）未完成 → **不得安排**，
      写入 `blocked_items`；
    - **约束7**：AC701 用在第 0/1/4 天、AC702 用在第 2/3 天，两两跨日 → 周转充裕；
      AC702 第 5 天维护，方案里没有第 5 天的架次；
    - **约束9**：每天只有一次起飞 → 20 分钟窗口 1 次、全场间隔无从谈起；
    - **约束6**：`NAV`/`BLD` 容量 1，各只有一个架次；
    - **约束2**：`P402`（第 0/1 天不可用）一次都没用上。
    """
    return make_plan(BASELINE_DRAFTS, ctx, blocked=BASELINE_BLOCKED, plan_id="PROP-BASELINE")


__all__ = [
    "BASELINE_BLOCKED",
    "BASELINE_DRAFTS",
    "WEEKDAY_NAMES",
    "SortieDraft",
    "clock",
    "compliant_plan",
    "day",
    "injection_world",
    "make_plan",
    "make_sortie",
]
