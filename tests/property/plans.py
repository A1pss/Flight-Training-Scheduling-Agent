"""`arbitrary_schedule_plan(ctx)` —— 随机**合法**方案的生成器（v6 §12.1）。

## 合法性靠构造，不靠过滤

v6 §12.1 那条属性测试的形态是「向**合法**计划注入单点违规」。所以生成器必须
**保证**产出合法方案，不能靠 `assume(报告全过)` 事后过滤（那样大部分样本会被
丢掉，Hypothesis 还会报 `FailedHealthCheck`）。

做法是把随机性限制在**证明可保持合规的四个自由度**上，基线的五个架次原样保留：

| 自由度 | 取值 | 为什么保持合规 |
|---|---|---|
| 每个架次落在**互不相同的一天** | `0..6` 里取 5 天 | 每天至多 1 个架次 → 约束7/8/9/12 的「同日」条款全部平凡成立 |
| 起飞时刻 | `[0, 720−dur]` 内任取 | 约束1 的窗口由取值域保证；当日只有一个架次，密度无从冲突 |
| 机号 | `AC701` / `AC702` | 两架同为 `TX-1`、适配全部课目；**第 5 天强制 AC701**（AC702 当天全天维护） |
| 跑道 | `RWY-7` / `RWY-8` | 两条都服务 `TX-1` |

约束13 不受影响：`P411` 的 B-1 与 `P412` 的 C-1 的 `freq_days=7`，周内唯一窗口
是 `[0,6]`、S-12 截止日是第 6 天 —— 排在哪一天都满足。约束3 的「每周必飞」同理
只看周计数。

`test_injected_violations.py::test_generated_plans_are_legal` 把「生成器只产出合法
方案」这件事本身做成了断言（两条通道同时确认）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from hypothesis import strategies as st

from backend.schemas.plan import SchedulePlan
from backend.validator.context import ValidationContext
from tests.property.world import BASELINE_BLOCKED, BASELINE_DRAFTS, SortieDraft, make_plan

#: 可自由挑选的机号（同机型、同适配课目）
SWAPPABLE_AIRCRAFT: tuple[str, ...] = ("AC701", "AC702")
#: AC702 第 5 天全天维护 → 那天只能用 AC701
MAINTENANCE_DAY: int = 5
MAINTAINED_AIRCRAFT: str = "AC702"
#: 两条跑道都服务 TX-1
SWAPPABLE_RUNWAYS: tuple[str, ...] = ("RWY-7", "RWY-8")


@st.composite
def arbitrary_drafts(draw: st.DrawFn, ctx: ValidationContext) -> tuple[SortieDraft, ...]:
    """在四个自由度上随机化基线的五个架次。"""
    days = draw(
        st.lists(st.integers(min_value=0, max_value=6), min_size=5, max_size=5, unique=True)
    )
    out: list[SortieDraft] = []
    for draft, day in zip(BASELINE_DRAFTS, days, strict=True):
        duration = ctx.missions[draft.mission_id].duration_minutes
        horizon = 720 - duration
        start = draw(st.integers(min_value=0, max_value=max(0, horizon)))
        aircraft = draw(st.sampled_from(SWAPPABLE_AIRCRAFT))
        if day == MAINTENANCE_DAY and aircraft == MAINTAINED_AIRCRAFT:
            aircraft = "AC701"
        runway = draw(st.sampled_from(SWAPPABLE_RUNWAYS))
        out.append(replace(draft, day=day, start=start, aircraft_id=aircraft, runway_id=runway))
    return tuple(out)


@st.composite
def arbitrary_schedule_plan(draw: st.DrawFn, ctx: ValidationContext) -> SchedulePlan:
    """v6 §12.1 的 `arbitrary_schedule_plan(ctx)`。产出的方案**保证合规**。"""
    drafts = draw(arbitrary_drafts(ctx))
    return make_plan(drafts, ctx, blocked=BASELINE_BLOCKED, plan_id="PROP-RANDOM")


def plan_from_drafts(drafts: Sequence[SortieDraft], ctx: ValidationContext) -> SchedulePlan:
    return make_plan(drafts, ctx, blocked=BASELINE_BLOCKED, plan_id="PROP-RANDOM")


__all__ = [
    "MAINTAINED_AIRCRAFT",
    "MAINTENANCE_DAY",
    "SWAPPABLE_AIRCRAFT",
    "SWAPPABLE_RUNWAYS",
    "arbitrary_drafts",
    "arbitrary_schedule_plan",
    "plan_from_drafts",
]
