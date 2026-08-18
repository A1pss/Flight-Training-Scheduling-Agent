"""Sheet 4「合规与解释报告」的**七个区块**（v6 §10.4）在前端的预览形态。

## 这里是「预览」，不是「产物」

真正的 Sheet 4 由 `backend/validator/workbook.py` 写出，并由 §4.3 闸门 3
（Excel 回读反解 == `SchedulePlan` 深度相等）把关。前端这一份是**同一批数据的
另一种呈现**，让排班员在归档之前就能看到七个区块长什么样。

**两者不共用代码是刻意的**：workbook 那边的字段顺序、分隔符、底纹属于版式契约
（`SWITCH_SEP` / `SWITCH_KV` 之类），改一个字节就会让闸门 3 失败；前端这边要的是
可读性。硬拉到一起，前端的一次排版调整就能把归档链路搞挂。

## 全部是纯函数

每个 `block_n(...)` 都是「数据进、表格出」，不碰 `st.*`。于是
`tests/unit/test_frontend_sheet4.py` 能直接断言七个区块的内容，
而不必起一个浏览器。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from backend.schemas.api import RunResultView
from backend.schemas.plan import SchedulePlan
from backend.schemas.validation import ValidationReport

#: 七个区块的标题（v6 §10.4 逐字）。**顺序即纵向排列顺序。**
BLOCK_TITLES: tuple[str, ...] = (
    "区块 1 · 计划元信息",
    "区块 2 · 约束校验结果",
    "区块 3 · 训练进度与欠账",
    "区块 4 · 阻塞项",
    "区块 5 · 资源利用",
    "区块 6 · 松弛与决策记录",
    "区块 7 · 跑道与空域占用明细",
)

#: 松弛档位的说明文案。
#:
#: ⚠️ **Tier 2 按 D-6 写「约束3 整体降级为软目标」**，不是旧的「A 类降至每人 1 次」
#: —— S-02 裁定「A 类整体每周 ≥1 次」之后，旧定义已经成了空操作
#: （v6 §3.10 / 本版说明 D-6）。UI 上照旧写会让排班员以为选了 T2 还有别的效果。
TIER_LABELS: dict[int, str] = {
    0: "T0 · 全硬约束",
    1: "T1 · 约束13 的频率窗口降级为软目标（允许欠账，最大化完成度）",
    2: "T2 · T1 + 约束3「A 类每周必飞」整体降级为软目标",
    3: "T3 · T2 + 经授权放宽 R1（约束10/11/12），需训练主任",
}


def _fmt_switches(switches: dict[str, str]) -> str:
    """`S-01=…；S-02=…` —— 与 Sheet 4 区块1 同一读法（`Z-7`）。"""
    return "；".join(f"{k}={v}" for k, v in sorted(switches.items()))


def block_1_meta(run: RunResultView) -> list[dict[str, str]]:
    """计划元信息。九行，缺的写「—」而不是留空。"""
    plan = run.plan
    stats = run.solver.stats
    rows: list[tuple[str, str]] = [
        ("计划编号", plan.plan_id if plan else "—"),
        (
            "ISO 周 / 覆盖日期",
            f"{plan.iso_week} / {plan.week_start} ~ {plan.week_end}" if plan else "—",
        ),
        ("数据快照", run.snapshot_id or "—"),
        (
            "规则版本 / 语义版本",
            f"{run.ruleset_version or '—'} / {run.semantics_version or '—'}",
        ),
        ("语义开关", _fmt_switches(plan.semantics_switches) if plan else "—"),
        ("跑道模型", plan.runway_model if plan else "—"),
        ("松弛档位", TIER_LABELS.get(plan.relaxation_tier if plan else 0, "—")),
        (
            "求解状态 / 耗时 / 目标值 / gap / worker / seed",
            "—"
            if stats is None
            else (
                f"{stats.status} / {stats.wall_time_ms / 1000:.1f}s / "
                f"{stats.objective_value if stats.objective_value is not None else '—'} / "
                f"{stats.gap if stats.gap is not None else '—'} / "
                f"{stats.num_workers} / {stats.random_seed}"
            ),
        ),
        ("内容指纹", plan.content_sha256 if plan else "—"),
    ]
    return [{"字段": key, "取值": value} for key, value in rows]


def block_2_validation(run: RunResultView) -> list[dict[str, Any]]:
    """约束校验结果：14 行 + 末行格式三层。"""
    report = run.validation
    rows: list[dict[str, Any]] = []
    if report is not None:
        for result in report.results:
            rows.append(
                {
                    "规则编号": f"约束{int(result.rule_id[1:])}",
                    "规则名称": result.rule_title,
                    "判定": "✅ 通过" if result.passed else "❌ 未通过",
                    "检查项数": result.checked_items,
                    "违规数": len(result.violations),
                    "说明": "；".join(result.notes) if result.notes else "—",
                }
            )
    return rows


def format_gates(run: RunResultView) -> str:
    """格式校验三层（v6 §4.3）的一行小结。

    闸门 1 是 14 条规则，闸门 2 是业务完整性，闸门 3 是「写出去再读回来、
    与源对象深度相等」。**三层都要显示**——只显示第一层会让人以为
    「14 条全过 = 可以交付」，而 Excel 写坏了同样交付不了（FTS-5001）。
    """
    report = run.validation
    gate1 = "✅" if report is not None and report.all_passed else "❌"
    gate2 = "✅" if report is not None and not report.all_violations() else "❌"
    check = run.schema_check
    gate3 = "✅" if check is not None and check.passed else ("—" if check is None else "❌")
    return f"Schema 层 {gate1} / 业务完整性层 {gate2} / 产物回读层 {gate3}"


def block_3_progress(run: RunResultView) -> list[dict[str, Any]]:
    """训练进度与欠账。**「松弛档」是第 10 列**（`Z-10`），无松弛写「—」。"""
    plan = run.plan
    if plan is None:
        return []
    return [
        {
            "人员": debt.person_id,
            "课目": debt.mission_id,
            "本周应排": debt.required,
            "实际排": debt.scheduled,
            "欠账": debt.debt,
            "松弛档": debt.relaxed_by or "—",
        }
        for debt in plan.debts
    ]


def block_4_blocked(run: RunResultView) -> list[dict[str, Any]]:
    """阻塞项（先修未满足，按约束13 不得安排）。

    **披露率 100% 是 v6 §0.3 的四条可测断言之一**：被排除的组合必须出现在这里，
    不能悄悄消失。基准周应当有 7 条。
    """
    plan = run.plan
    if plan is None:
        return []
    return [
        {
            "人员": item.person_id,
            "课目": item.mission_id,
            "阻塞原因": item.reason,
            "缺失先修": "、".join(item.missing_prereqs) or "—",
        }
        for item in plan.blocked_items
    ]


def block_5_resources(run: RunResultView) -> list[dict[str, Any]]:
    """资源利用：飞机 / 人员 / 空域 / 跑道逐行。"""
    plan = run.plan
    if plan is None:
        return []
    rows: list[dict[str, Any]] = []
    aircraft: Counter[str] = Counter()
    airspace: Counter[str] = Counter()
    runway: Counter[str] = Counter()
    person: Counter[str] = Counter()
    minutes: defaultdict[str, int] = defaultdict(int)

    for sortie in plan.sorties:
        duration = (
            sortie.landing.hour * 60
            + sortie.landing.minute
            - sortie.takeoff.hour * 60
            - sortie.takeoff.minute
        )
        aircraft[sortie.aircraft_id] += 1
        minutes[sortie.aircraft_id] += duration
        airspace[sortie.airspace_id] += 1
        runway[sortie.runway_id] += 1
        for member in sortie.crew:
            person[f"{member.name}({member.person_id})"] += 1

    for name, count in sorted(aircraft.items()):
        rows.append({"对象": name, "类别": "飞机", "架次": count, "飞行时长(分)": minutes[name]})
    for name, count in sorted(person.items()):
        rows.append({"对象": name, "类别": "人员", "架次": count, "飞行时长(分)": "—"})
    for name, count in sorted(airspace.items()):
        rows.append({"对象": name, "类别": "空域", "架次": count, "飞行时长(分)": "—"})
    for name, count in sorted(runway.items()):
        rows.append({"对象": name, "类别": "跑道", "架次": count, "飞行时长(分)": "—"})
    return rows


def block_6_relaxation(run: RunResultView) -> list[dict[str, str]]:
    """松弛与决策记录。

    **「授权改写声明」是强制项**（v6 §10.4 区块6）：只要 S-11 开关为 on，
    无论本周是否真排出复训架次，这一行都必须出现——它让评审者看到
    「刘斌到期后还在飞 C 类」时立刻知道这是设计而非 bug（风险 R17）。
    校验器把这条声明放在 `CheckResult.notes` 里，这里原样取出来。
    """
    plan = run.plan
    report: ValidationReport | None = run.validation
    tier = plan.relaxation_tier if plan else 0
    rows = [
        {
            "项": "使用的松弛",
            "内容": TIER_LABELS.get(tier, str(tier)) if tier else "本次未使用任何松弛",
        },
        {
            "项": "冲突集",
            "内容": "；".join(c.description for c in run.conflicts) if run.conflicts else "无",
        },
    ]
    notes = report.all_notes() if report is not None else []
    rows.append({"项": "授权改写声明", "内容": "；".join(notes) if notes else "—"})
    return rows


def block_7_runway(run: RunResultView) -> list[dict[str, Any]]:
    """跑道与空域占用明细（v6 §10.4 区块7，v6 新增）。

    Sheet 1~3 **不加跑道列**（会偏离版式基准 §1.2.2），跑道只在这一块出现。
    """
    plan = run.plan
    if plan is None:
        return []
    return [
        {
            "架次号": s.sortie_id,
            "日期": s.date.isoformat(),
            "起飞": s.takeoff.strftime("%H:%M"),
            "机号": s.aircraft_id,
            "跑道": s.runway_id,
            "空域": s.airspace_id,
            "复训标记": "复训" if s.is_recurrent else "—",
        }
        for s in sorted(plan.sorties, key=lambda x: (x.date, x.takeoff, x.sortie_id))
    ]


def all_blocks(run: RunResultView) -> list[tuple[str, list[dict[str, Any]]]]:
    """七个区块，按 v6 §10.4 的纵向顺序。**永远是 7 个**，空的也占位。"""
    return [
        (BLOCK_TITLES[0], block_1_meta(run)),
        (BLOCK_TITLES[1], block_2_validation(run)),
        (BLOCK_TITLES[2], block_3_progress(run)),
        (BLOCK_TITLES[3], block_4_blocked(run)),
        (BLOCK_TITLES[4], block_5_resources(run)),
        (BLOCK_TITLES[5], block_6_relaxation(run)),
        (BLOCK_TITLES[6], block_7_runway(run)),
    ]


def blocked_banner(plan: SchedulePlan | None) -> str:
    """BLOCKED 提示条的文案（黄色，v6 §8.3 那句「⚠ 7 项因先修未满足未安排」）。"""
    if plan is None or not plan.blocked_items:
        return ""
    by_person: Counter[str] = Counter(item.person_id for item in plan.blocked_items)
    detail = " · ".join(f"{pid} {count} 项" for pid, count in sorted(by_person.items()))
    return f"⚠ {len(plan.blocked_items)} 项因先修未满足未安排（{detail}）"


__all__ = [
    "BLOCK_TITLES",
    "TIER_LABELS",
    "all_blocks",
    "block_1_meta",
    "block_2_validation",
    "block_3_progress",
    "block_4_blocked",
    "block_5_resources",
    "block_6_relaxation",
    "block_7_runway",
    "blocked_banner",
    "format_gates",
]
