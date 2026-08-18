"""三张表（Sheet 1~3）与周甘特图的数据构造（v6 §10.1~§10.3、§8.3）。

## 拼接格式照 §10.1~§10.3 的版式基准

课目列 `本场起落航线 (missionA-1)（Small Area A）`、机组列 `孙军教，陈伟学`
（**全角逗号**，`Z-10`）、角色后缀 `教/学/单/训`——这些不是随手写的，是版式
基准里可采信的部分（§10.5：列名、列顺序、分组层次、拼接格式可采信，
**图里的架次数据一律不采信**）。

角色后缀直接 import `backend/validator/workbook.py` 的 `ROLE_SUFFIX`：那是
**回读契约**的一部分（M2-B 冻结），前端自己抄一份迟早漂。

## Sheet 1~3 不加跑道列

跑道只出现在 Sheet 4 区块 7（v6 §10.4 那句「Sheet 1~3 不加跑道列，以免偏离
版式基准」）。这条在前端同样成立——预览与产物列不一样，排班员对着屏幕核 Excel
时会当场怀疑其中一个是错的。
"""

from __future__ import annotations

from typing import Any

from backend.schemas.plan import SchedulePlan, Sortie
from backend.validator.workbook import ROLE_SUFFIX

#: 机组列的分隔符（全角逗号，`Z-10`）。回读侧只认这一个。
CREW_SEP = "，"

WEEKDAY_ORDER: dict[str, int] = {
    "周一": 0,
    "周二": 1,
    "周三": 2,
    "周四": 3,
    "周五": 4,
    "周六": 5,
    "周日": 6,
}


def mission_cell(sortie: Sortie, *, with_airspace: bool = True) -> str:
    """`本场起落航线 (missionA-1)（Small Area A）`。"""
    base = f"{sortie.mission_name} ({sortie.mission_id})"
    return f"{base}（{sortie.airspace_id}）" if with_airspace else base


def crew_cell(sortie: Sortie) -> str:
    """`孙军教，陈伟学` —— 姓名 + 角色后缀，全角逗号分隔。"""
    return CREW_SEP.join(f"{m.name}{ROLE_SUFFIX[m.role]}" for m in sortie.crew)


def _sorted_sorties(plan: SchedulePlan) -> list[Sortie]:
    return sorted(plan.sorties, key=lambda s: (s.date, s.takeoff, s.sortie_id))


def sheet1_rows(plan: SchedulePlan | None) -> list[dict[str, Any]]:
    """Sheet 1 · 分日飞行计划表：按周一~周日分组，组内按起飞时刻升序。"""
    if plan is None:
        return []
    return [
        {
            "星期": s.weekday,
            "日期": s.date.isoformat(),
            "起飞": s.takeoff.strftime("%H:%M"),
            "着陆": s.landing.strftime("%H:%M"),
            "飞机": s.aircraft_id,
            "课目（空域）": mission_cell(s),
            "机组": crew_cell(s),
        }
        for s in _sorted_sorties(plan)
    ]


def sheet2_rows(plan: SchedulePlan | None) -> list[dict[str, Any]]:
    """Sheet 2 · 飞行员训练时间表：按人员 → 星期 → 时刻。"""
    if plan is None:
        return []
    rows: list[dict[str, Any]] = []
    for s in _sorted_sorties(plan):
        for member in s.crew:
            rows.append(
                {
                    "飞行员": f"{member.name}({member.person_id})",
                    "星期": s.weekday,
                    "时间": f"{s.takeoff.strftime('%H:%M')}-{s.landing.strftime('%H:%M')}",
                    "课目": mission_cell(s, with_airspace=False),
                    "飞机/角色": f"({s.aircraft_id}/{member.role})",
                }
            )
    return sorted(rows, key=lambda r: (r["飞行员"], WEEKDAY_ORDER[r["星期"]], r["时间"]))


def sheet3_rows(plan: SchedulePlan | None) -> list[dict[str, Any]]:
    """Sheet 3 · 飞机排班表：按机号 → 星期 → 时刻。"""
    if plan is None:
        return []
    rows = [
        {
            "机号": s.aircraft_id,
            "星期": s.weekday,
            "起飞": s.takeoff.strftime("%H:%M"),
            "课目": mission_cell(s, with_airspace=False),
            "机组": "（" + "/".join(m.name for m in s.crew) + "）",
        }
        for s in _sorted_sorties(plan)
    ]
    return sorted(rows, key=lambda r: (r["机号"], WEEKDAY_ORDER[r["星期"]], r["起飞"]))


def gantt_rows(plan: SchedulePlan | None) -> list[dict[str, Any]]:
    """周甘特图的数据（一条架次一行）。

    横轴用**当天的分钟数**而不是 datetime：七天的架次要叠在同一根时间轴上比较
    「哪天挤」，用真实时间戳会把它们摊成一条 168 小时的长条，什么也看不出来。
    """
    if plan is None:
        return []
    rows: list[dict[str, Any]] = []
    for s in _sorted_sorties(plan):
        start = s.takeoff.hour * 60 + s.takeoff.minute
        end = s.landing.hour * 60 + s.landing.minute
        rows.append(
            {
                "星期": s.weekday,
                "日期": s.date.isoformat(),
                "架次": s.sortie_id,
                "飞机": s.aircraft_id,
                "跑道": s.runway_id,
                "课目": s.mission_id,
                "起飞分钟": start,
                "着陆分钟": end,
                "起飞": s.takeoff.strftime("%H:%M"),
                "着陆": s.landing.strftime("%H:%M"),
                "机组": crew_cell(s),
            }
        )
    return rows


def week_summary(plan: SchedulePlan | None) -> dict[str, int]:
    """一周概览：总架次 / 带飞 / 单飞 / 复训 / 阻塞项。

    带飞与单飞的判据是**机组人数**（§3.1.1：带飞 2 人、单飞与复训 1 人），
    不是课目类别——D-1 之后「A 类学员单飞」是常态，按类别判会数错。
    """
    if plan is None:
        return {"架次": 0, "带飞": 0, "单飞": 0, "复训": 0, "阻塞项": 0}
    dual = sum(1 for s in plan.sorties if len(s.crew) == 2)
    recurrent = sum(1 for s in plan.sorties if s.is_recurrent)
    return {
        "架次": len(plan.sorties),
        "带飞": dual,
        "单飞": len(plan.sorties) - dual - recurrent,
        "复训": recurrent,
        "阻塞项": len(plan.blocked_items),
    }


__all__ = [
    "CREW_SEP",
    "WEEKDAY_ORDER",
    "crew_cell",
    "gantt_rows",
    "mission_cell",
    "sheet1_rows",
    "sheet2_rows",
    "sheet3_rows",
    "week_summary",
]
