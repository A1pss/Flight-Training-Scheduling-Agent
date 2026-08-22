"""§12.3 的基线对比：让 LLM 直接排班，看它能不能满足硬约束。

v6 §12.3 那张表原本写着「预期 0~15%」「预期 20~50%」，**按铁律 6 已被删掉**，
要真的跑。本模块就是那两行的实测。

## 三种配置

| 配置 | 做法 |
|---|---|
| `llm_only` | 把世界描述 + 14 条约束给 LLM，要它直接产出整周架次，**一次成型** |
| `llm_retry` | 同上，但把 schema 错误与硬违规回灌，最多 5 轮 |
| 本系统 | CP-SAT + 独立校验 —— 已由 200 场景实测为 100% |

## 判定用的是同一套校验器

LLM 产出的方案走**与本系统完全相同的两道**：`validate_plan_schema`（格式）
与 `run_all_checks`（14 条）。硬违规为零才算「满足硬约束」。这一点很要紧 ——
换一把宽松的尺子去量基线，这组对比就没有意义了。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from backend.validator.checks import run_all_checks
from backend.validator.context import ValidationContext
from backend.validator.schema import validate_plan_schema

#: 回灌重试的上限（§12.3 那一行写死的「≤5 轮」）。
MAX_ROUNDS = 5


def render_world(ctx: ValidationContext, perturbations: Sequence[str] = ()) -> str:
    """把校验上下文渲染成给模型看的世界描述。

    **从 `ValidationContext` 渲染而不是另写一份**：模型看到的世界与校验器据以
    判定的世界必须是同一个，否则测出来的是「描述写得全不全」。
    """
    lines: list[str] = [f"训练周：{ctx.week_start}（周一）起 7 天。", "", "## 人员"]
    for p in ctx.persons.values():
        quals = "、".join(sorted(p.qualifications)) or "无"
        off = "、".join(str(d) for d in sorted(p.unavailable_dates)) or "无"
        lines.append(
            f"- {p.person_id} {p.name}｜身份 {p.identity}｜机型 {'、'.join(sorted(p.aircraft_types)) or '无'}"
            f"｜资质 {quals}｜不可用日期 {off}"
        )
    lines += ["", "## 飞机"]
    for a in ctx.aircraft.values():
        maint = (
            "、".join(sorted(f"{w.start:%m-%d %H:%M}~{w.end:%m-%d %H:%M}" for w in a.maintenance))
            or "无"
        )
        lines.append(
            f"- {a.aircraft_id}｜机型 {a.aircraft_type}｜{a.seats} 座｜周转 {a.turnaround_minutes} 分钟"
            f"｜每日窗口 {a.daily_window_start}-{a.daily_window_end}｜维护日 {maint}"
        )
    lines += ["", "## 课目"]
    for m in ctx.missions.values():
        lines.append(
            f"- {m.mission_id} {m.name}｜类别 {m.mission_class}｜时长 {m.duration_minutes} 分钟"
            f"｜频率 每 {m.freq_days} 天｜带飞 {'是' if m.dual_required else '否'}"
            f"｜空域 {m.airspace_id}｜机型 {'、'.join(sorted(m.aircraft_types))}"
            f"｜先修 {'、'.join(sorted(p.ref for p in m.prereqs)) or '无'}"
        )
    lines += ["", "## 空域"]
    lines += [f"- {s.airspace_id} {s.name}｜同时容量 {s.capacity}" for s in ctx.airspaces.values()]
    lines += ["", "## 跑道"]
    lines += [
        f"- {r.runway_id} {r.name}｜可用机型 {'、'.join(sorted(r.aircraft_types))}"
        for r in ctx.runways.values()
    ]
    if perturbations:
        lines += ["", "## 本场景的额外扰动"] + [f"- {p}" for p in perturbations]
    return "\n".join(lines)


CONSTRAINTS = """## 必须满足的硬约束（14 条）

1. 每个架次必须落在飞机的每日可用窗口内。
2. 只能用有该课目资质、且持有该机型资质的人。
3. 每名学员本周至少飞 1 次 A 类课目。
4. 教员每日带飞不超过 3 架次、每周不超过 12 架次。
5. 同一人同一时刻只能在一个架次上。
6. 飞机、空域、跑道在同一时刻的占用不得超过容量；维护中的飞机不可用。
7. 同一架飞机两个架次之间要留够周转时间（上一架次**着陆** → 下一架次**起飞**）。
8. 同一人同日内累计飞行时间不超过规定上限。
9. 同一跑道 20 分钟窗口内起降不超过 2 次；全场任意两次起飞间隔不少于 7 分钟。
10. 架次时长必须等于课目规定时长。
11. 学员每周架次数不超过上限。
12. 每个架次的空域必须是该课目指定的空域。
13. 未满足先修的 (人, 课目) 一个架次都不能排；课目频率按各自 freq_days 滑窗。
14. 每个 (人, 课目) 本周次数不超过 ceil(7 / freq_days)。

带飞规则：课目「带飞=是」且飞行员身份是「学员」时，机组必须是 2 人
（1 名教员 + 1 名学员）；其余情况是 1 人单飞。"""

OUTPUT_SPEC = """## 输出格式

只输出 JSON，形如：

{"sorties": [
  {"sortie_id": "S000001", "date": "2026-01-05", "weekday": "周一",
   "takeoff": "06:00", "landing": "06:54",
   "mission_id": "missionB-2", "mission_name": "导航飞行",
   "airspace_id": "RT1", "aircraft_id": "AC27", "runway_id": "RWY-1",
   "crew": [{"person_id": "P01", "name": "孙军", "role": "教员"},
            {"person_id": "P08", "name": "何超", "role": "学员"}]}
]}

字段要求（全部必填）：
- `weekday` 只能是 周一/周二/周三/周四/周五/周六/周日，且必须与 `date` 对得上。
- `mission_name` 是该课目的名称，`name` 是该人员的姓名（见上面的清单）。
- `role` 只能是 教员 / 学员 / 单飞 / 复训。单人机组用 单飞（或成熟飞行员复训用 复训）。
- `landing` − `takeoff` 必须等于该课目的时长。

不要输出任何解释文字。"""


@dataclass
class BaselineOutcome:
    """一个场景在一种配置下的结果。"""

    scenario_id: str
    config: str
    rounds_used: int = 0
    parsed: bool = False
    schema_errors: list[str] = field(default_factory=list)
    hard_violations: list[str] = field(default_factory=list)
    num_sorties: int = 0
    satisfied: bool = False
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "config": self.config,
            "rounds_used": self.rounds_used,
            "parsed": self.parsed,
            "schema_errors": self.schema_errors[:8],
            "hard_violations": self.hard_violations,
            "num_sorties": self.num_sorties,
            "satisfied": self.satisfied,
            "error": self.error,
        }


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象。

    **不做任何纠错**（不补引号、不删尾逗号）：那属于替模型把活干了，
    会把「LLM 直接排班」这条基线测成「LLM + 一个修复器」。
    """
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def grade(
    payload: Mapping[str, Any], ctx: ValidationContext, template: Mapping[str, Any]
) -> tuple[bool, list[str], list[str], int]:
    """把模型给的架次装进 `SchedulePlan` 外壳，再走与本系统同一套校验。

    `template` 提供计划的元信息（周次、快照、规则集版本…）——那些不是模型该
    产出的东西，让它填只会引入与能力无关的失败。
    """
    candidate = dict(template)
    candidate["sorties"] = payload.get("sorties", [])
    plan, errors = validate_plan_schema(candidate)
    if plan is None:
        return False, errors, [], 0
    report = run_all_checks(plan, ctx)
    hard = sorted({v.rule_id for v in report.all_violations() if v.severity == "HARD"})
    return True, [], hard, len(plan.sorties)


__all__ = [
    "CONSTRAINTS",
    "MAX_ROUNDS",
    "OUTPUT_SPEC",
    "BaselineOutcome",
    "extract_json",
    "grade",
    "render_world",
]
