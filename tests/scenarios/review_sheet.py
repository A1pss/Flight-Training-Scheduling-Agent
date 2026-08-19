"""人工抽检用的**逐场景一页纸**（v6 §12.3 度量方式第 3 条）。

```bash
conda run -n schedule python -m tests.scenarios.review_sheet > reports/M2_人工抽检核对表.md
```

三重独立验证的前两条（主校验器、第三方 naive checker）是自动跑的；第 3 条只能由
懂业务的人来做。**它要抓的不是「代码有没有按 v6 实现」——那两条已经查过两遍了，
而是「v6 这么规定，结果合不合业务常理」。** 规则被忠实实现、两条通道也一致，但
规定本身不是训练中心想要的 —— 这类问题只有人能看出来（v6 `Z-9` 就是这么发现的）。

所以这份表把核对一个场景需要的东西全摊在一页里：**扰动做了什么 → 系统给了什么
结论 → 方案长什么样**，人只需要读和判断，不必在两个 JSON 之间来回翻。

抽样固定种子（与 :mod:`tests.scenarios.report` 同一个 `SAMPLE_SEED`），业务方
按同一份清单复核即可复现。
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping, Sequence
from typing import Any

from backend.core.config import PROJECT_ROOT
from tests.scenarios.catalog import ScenarioCase, load_dataset
from tests.scenarios.report import RESULT_PATH, SAMPLE_SEED, SAMPLES_PER_CATEGORY, load_results

SOLVED = ("OPTIMAL", "FEASIBLE")

CATEGORY_LABELS: dict[str, str] = {
    "baseline": "基准周",
    "single": "单点扰动",
    "combo": "组合扰动",
    "boundary": "边界场景",
    "infeasible": "构造不可行",
    "reschedule": "局部重排",
}

#: 每类该问的问题 —— 抽检不是重算 14 条，是判断结论合不合业务常理
CATEGORY_QUESTIONS: dict[str, tuple[str, ...]] = {
    "baseline": (
        "14 个架次的分布看着像不像一周真实的训练安排（不是全挤在一天、不是全给一个人）？",
        "7 条阻塞项是否就是你认为本周确实排不了的那几个 (学员, 课目)？",
    ),
    "single": (
        "这一个异常，导致的后果（架次数变化 / 判不可行）与你的预期一致吗？",
        "**如果判了不可行**：这个异常在你看来是不是真的严重到该让整周排不出来？",
    ),
    "combo": (
        "几个异常叠加之后的结论，与你凭经验的判断一致吗？",
        "**如果判了不可行**：换成你来排，这一周是真的排不出来，还是应该降级松弛后照排？",
    ),
    "boundary": (
        "「恰好够」这一档确实是你认为的临界点吗（再少一格就真的不行了）？",
        "临界档下排出来的方案，实际操作中可行吗（有没有过于极限的安排）？",
    ),
    "infeasible": (
        "这个构造在你看来确实应该无解吗？",
        "系统给出的冲突源，是不是你会指出的那几条约束？",
        "「升级人工」还是「给出松弛提案」，哪个才是你期望的处置？",
    ),
    "reschedule": (
        "扰动之后被改动的架次范围，与冻结档位的承诺相符吗（保守档改得少、激进档改得多）？",
        "重排后的方案，排班员拿到手能直接用吗？",
    ),
}


def _sample(results: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    rng = random.Random(SAMPLE_SEED)
    picked: list[Mapping[str, Any]] = []
    for category in CATEGORY_LABELS:
        rows = [r for r in results if r["category"] == category]
        chosen = (
            rows if len(rows) <= SAMPLES_PER_CATEGORY else rng.sample(rows, SAMPLES_PER_CATEGORY)
        )
        picked.extend(sorted(chosen, key=lambda r: r["scenario_id"]))
    return picked


def _perturbation_lines(case: ScenarioCase) -> list[str]:
    """把扰动翻成人话。"""
    ov = case.overrides
    lines: list[str] = []
    if ov.window_start or ov.window_end:
        lines.append(f"- 训练窗改为 **{ov.window_start or '06:00'} – {ov.window_end or '18:00'}**")
    for aid, cap in sorted(ov.airspace_capacity.items()):
        word = "**关闭**（容量 0）" if cap == 0 else f"容量降为 **{cap}**"
        lines.append(f"- 空域 `{aid}` {word}")
    for rid in sorted(ov.closed_runways):
        lines.append(f"- 跑道 `{rid}` **关闭**")
    for pid in sorted(ov.unavailable_all_week):
        lines.append(f"- `{pid}` **整周不可用**")
    for pid, days in sorted(ov.unavailable.items()):
        lines.append(f"- `{pid}` 不可用 **{len(days)} 天**（{'、'.join(days)}）")
    for aid, first, last in sorted(ov.maintenance_all_day):
        span = "整周" if first != last else "当天"
        lines.append(f"- 飞机 `{aid}` 全天维护 {first} ~ {last}（{span}）")
    for key, when in sorted(ov.qual_expiry.items()):
        pid, cls = key.split("|")
        lines.append(f"- `{pid}` 的 **{cls} 类**资质到期日改为 **{when}**")
    if case.reschedule:
        lines.append(
            f"- 局部重排：{case.reschedule.get('reason', '')}，冻结档 "
            f"**{case.reschedule.get('policy', 'BALANCED')}**"
        )
    return lines or ["- （无扰动，原始数据）"]


def _verdict_block(result: Mapping[str, Any], case: ScenarioCase) -> list[str]:
    lines = [f"**系统结论：`{result['status']}`**"]
    if result["status"] in SOLVED:
        lines.append(
            f"排出 **{result['num_sorties']} 个架次**，"
            f"披露 **{result['num_blocked']} 条阻塞项**；"
            f"主校验器 **{'14 条全过' if result['main_passed'] else '有违规'}**、"
            f"第三方 naive checker **{'全过' if result['naive_passed'] else '有违规'}**、"
            f"格式校验 **{'通过' if result['format_passed'] else '未通过'}**。"
        )
    else:
        lines.append(
            f"冲突源（归因后）：{'、'.join(f'`{r}`' for r in result['conflict_rules']) or '—'}；"
            f"CP-SAT 极小 core `{list(result['sat_core_ids'])}`、"
            f"结构性不可满足组 `{list(result['structural_ids'])}`。"
        )
        if result.get("escalate") is not None:
            lines.append(
                f"处置：**{'升级人工' if result['escalate'] else '给出松弛提案'}**"
                f"（经探针验证且真排出架次的提案 {result['useful_proposals']} 个，"
                f"已验证提案 {result['verified_proposals']} 个）。"
            )
    if case.expected_status != "EITHER":
        ok = (
            result["status"] in SOLVED
            if case.expected_status == "SOLVED"
            else result["status"] == "INFEASIBLE"
        )
        lines.append(f"构造时的预期：`{case.expected_status}` → {'✅ 相符' if ok else '❌ 不符'}")
    if case.annotated_conflict_rules:
        lines.append(f"v6 §12.3 标注的真实冲突源：{list(case.annotated_conflict_rules)}")
    return lines


def _plan_table(plan: Mapping[str, Any] | None) -> list[str]:
    if not plan:
        return []
    lines = [
        "",
        "<details><summary>方案全量（点开）</summary>",
        "",
        "| 架次 | 日期 | 星期 | 起飞–着陆 | 课目 | 空域 | 机号 | 跑道 | 机组 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in plan["sorties"]:
        crew = "、".join(f"{c['name']}({c['role']})" for c in s["crew"])
        mark = " ⟳复训" if s["is_recurrent"] else ""
        lines.append(
            f"| {s['sortie_id']} | {s['date']} | {s['weekday']} | "
            f"{s['takeoff'][:5]}–{s['landing'][:5]} | {s['mission_id']}{mark} | "
            f"{s['airspace_id']} | {s['aircraft_id']} | {s['runway_id']} | {crew} |"
        )
    if plan.get("blocked_items"):
        lines += ["", "**阻塞项**：", ""]
        for b in plan["blocked_items"]:
            lines.append(f"- `{b['person_id']}` × `{b['mission_id']}` —— {b['reason']}")
    lines += ["", "</details>"]
    return lines


def _plans_by_scenario() -> dict[str, Mapping[str, Any]]:
    """方案明细来自 200 场景运行时落的 `plans` 分块（没有就退化为不展示）。"""
    payload = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    return payload.get("plans", {})


def render() -> str:
    _summary, results = load_results()
    cases = {c.scenario_id: c for c in load_dataset(PROJECT_ROOT)}
    plans = _plans_by_scenario()
    picked = _sample(results)

    out: list[str] = [
        "# M2 人工抽检核对表（三重独立验证的第 3 条）",
        "",
        f"**抽样**：每类固定种子 `SAMPLE_SEED={SAMPLE_SEED}` 抽 {SAMPLES_PER_CATEGORY} 个，"
        f"共 **{len(picked)}** 个场景。同一份种子任何时候都抽出同一批，可复现。",
        "",
        "## 这份表要你判断什么",
        "",
        "**不是「代码有没有按 v6 实现」** —— 那件事已经被两条互相独立的校验通道各查了一遍，",
        "200 个场景上判定 100% 一致。**是「v6 这么规定，结果合不合业务常理」。**",
        "",
        "规则被忠实实现、两条通道也完全同意，但规定本身不是训练中心想要的 —— 这类问题",
        "自动化测试永远发现不了。v6 的 `Z-9`（学员整周请假导致全周不可行）就是这么被",
        "发现的：代码没错，两条通道都说「按规则确实不可行」，错的是规则没考虑常规请假。",
        "",
        "每个场景下面列了**该问的问题**。逐条读，在 ☐ 里打勾或写下你的疑问。",
        "",
        "| 记号 | 含义 |",
        "|---|---|",
        "| ☑ 合理 | 结论符合业务常理，无异议 |",
        "| ☐ 存疑 | 说不上错，但想问一句 —— 在下面写清楚问什么 |",
        "| ☒ 不合理 | 这个结论业务上不能接受 —— 写清楚你期望的是什么 |",
        "",
        "---",
        "",
    ]

    current = ""
    for result in picked:
        case = cases[result["scenario_id"]]
        if case.category != current:
            current = case.category
            out += [f"## {CATEGORY_LABELS[current]}", ""]
        out += [
            f"### ☐ {result['scenario_id']} · {case.title}",
            "",
            "**扰动**：",
            "",
            *_perturbation_lines(case),
            "",
            *_verdict_block(result, case),
            *_plan_table(plans.get(result["scenario_id"])),
            "",
            "**请判断**：",
            "",
        ]
        for question in CATEGORY_QUESTIONS[case.category]:
            out.append(f"- ☐ {question}")
        out += ["", "> 结论：☐ 合理　☐ 存疑　☐ 不合理　　说明：", "", "---", ""]

    out += [
        "## 抽检完成后",
        "",
        "1. 把打勾结果连同「存疑 / 不合理」的说明发回；",
        "2. 每一条「不合理」都要走 `CLAUDE.md §7` 的裁定流程 —— 先定位到具体条款，",
        "   再决定改规格还是改实现，**不许直接改代码让结果好看**；",
        "3. 抽检通过后，`reports/M2_交叉验收报告.md` §1 的第 3 条通道才能标为已完成，",
        "   届时「三重独立验证」这个说法才成立。",
    ]
    return "\n".join(out)


def main() -> int:  # pragma: no cover —— CLI 入口
    print(render())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["CATEGORY_LABELS", "CATEGORY_QUESTIONS", "render"]
