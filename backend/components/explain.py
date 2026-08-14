"""LLM 节点 ④：`explain_llm` + Critic（v6 §7.2.3 / §7.1.4）。

> 有循环，但**循环条件是 `verify_claims` 的确定性判定**，不是模型自选。
> 生成 → 逐句核验 → 重写（≤N 轮）。

## 核验器是确定性的，这是本节点能被称作「LLM 节点」而非 Agent 的全部理由

模型自评「我觉得这段写得挺准」毫无意义。这里的核验器做的是一件很笨但很硬的事：
**把解释文本里出现的每一个数字与每一个实体编号，拿去和事实表对**。

- 数字对不上任何一条事实 → 该句 `supported=False`；
- 实体编号不在本方案里 → 同上；
- 既没有数字也没有实体的句子（「本周排班已完成」这类）→ 视为**无需核验**，
  不计入分母。核验一句没有事实内容的话，得到的只是一个虚高的比率。

## 事实表里放什么

只放**能从方案与校验报告里数出来的量**：架次总数、逐日架次数、带飞/单飞数、
阻塞项数、欠账数、校验通过条数、逐人架次数、逐机架次数、松弛档位……
`FactIndex` 不含任何推断值。模型写「利用率约七成」这种话时，`七成` 不是数字
也不在事实表里，那句就该被判不支持——而这正是我们要它被判出来的。

## 重写循环的边界

`EXPLAIN_MAX_REWRITES` 默认 1（v6 §7.6：`explain 生成 + 核验` 2~3 次调用，
含 1 轮重写）。重写完仍有不支持的断言时**不再重写**，而是把那几句连同
`GroundingReport` 一起交出去——**留着比抹掉好**：评审者要看到「哪一句没根据」，
而不是看到一段被反复打磨到无信息量的话。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from backend.core.config import Settings, get_settings
from backend.harness import AgentSpec, ContextBlock, Harness, structured_summary
from backend.schemas.plan import SchedulePlan
from backend.schemas.retrieval import Citation, GroundedClaim, GroundingReport
from backend.schemas.solver import SolverStats
from backend.schemas.validation import ValidationReport
from backend.skills_loader import SkillLibrary, render_skills, route_for_component

EXPLAIN_AGENT: Final[AgentSpec] = AgentSpec(
    name="explain",
    tools=(),
    requires_tool_call=False,
)

#: 阿拉伯数字（含小数与百分号）。中文数字刻意不认——「七成」不是可核验的量。
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
#: 编号形态，与 `schemas.plan` 同源（只固定前缀、不限位数）
_ENTITY = re.compile(r"\b(?:P\d+|AC\d+|mission[A-Z]-\d+|RWY-\d+|S\d{6})\b")
#: 断句：中文句号/问号/叹号/分号 + 换行
_SENTENCE = re.compile(r"[^。！？；\n]+[。！？；]?")


@dataclass(frozen=True)
class FactIndex:
    """可核验的事实集合。**只有数出来的量，没有推断值。**"""

    numbers: frozenset[str]
    entities: frozenset[str]
    citations: tuple[Citation, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)

    def supports_number(self, token: str) -> bool:
        return token in self.numbers

    def supports_entity(self, token: str) -> bool:
        return token in self.entities


def build_fact_index(
    plan: SchedulePlan,
    validation: ValidationReport | None = None,
    stats: SolverStats | None = None,
) -> FactIndex:
    """从方案与校验报告里数出全部可核验的量。"""
    numbers: set[str] = set()
    entities: set[str] = set()
    labels: dict[str, str] = {}

    def note(value: Any, label: str) -> None:
        token = _fmt(value)
        numbers.add(token)
        labels.setdefault(token, label)

    per_day: dict[str, int] = {}
    per_person: dict[str, int] = {}
    per_aircraft: dict[str, int] = {}
    dual = solo = 0
    for sortie in plan.sorties:
        entities.add(sortie.sortie_id)
        entities.add(sortie.aircraft_id)
        entities.add(sortie.mission_id)
        entities.add(sortie.runway_id)
        entities.add(sortie.airspace_id)
        per_day[sortie.weekday] = per_day.get(sortie.weekday, 0) + 1
        per_aircraft[sortie.aircraft_id] = per_aircraft.get(sortie.aircraft_id, 0) + 1
        if len(sortie.crew) == 2:
            dual += 1
        else:
            solo += 1
        for member in sortie.crew:
            entities.add(member.person_id)
            per_person[member.person_id] = per_person.get(member.person_id, 0) + 1

    note(len(plan.sorties), "架次总数")
    note(dual, "带飞架次数")
    note(solo, "单飞架次数")
    note(len(plan.blocked_items), "阻塞项数")
    note(len(plan.debts), "欠账条数")
    note(plan.relaxation_tier, "松弛档位")
    for day, count in per_day.items():
        note(count, f"{day}架次数")
    for person_id, count in per_person.items():
        note(count, f"{person_id}架次数")
        entities.add(person_id)
    for aircraft_id, count in per_aircraft.items():
        note(count, f"{aircraft_id}架次数")
    for blocked in plan.blocked_items:
        entities.add(blocked.person_id)
        entities.add(blocked.mission_id)
    for debt in plan.debts:
        entities.add(debt.person_id)
        entities.add(debt.mission_id)
        note(debt.debt, f"{debt.person_id}/{debt.mission_id} 欠账")

    citations = [
        Citation(source_kind="structured", source_id=f"plan:{plan.plan_id}", snippet=""),
    ]
    if validation is not None:
        note(len(validation.results), "已校验规则条数")
        note(sum(1 for r in validation.results if r.passed), "校验通过条数")
        note(len(validation.all_violations()), "违规条数")
        note(validation.total_checked_items, "检查项总数")
        citations.append(
            Citation(source_kind="structured", source_id=f"validation:{plan.plan_id}", snippet="")
        )
    if stats is not None:
        note(stats.num_candidates, "候选数")
        note(stats.num_variables, "变量数")
        note(stats.num_constraints, "约束数")
        note(round(stats.wall_time_ms / 1000, 1), "墙钟秒")
        note(int(stats.wall_time_ms), "墙钟毫秒")
        citations.append(
            Citation(source_kind="structured", source_id=f"solver:{plan.plan_id}", snippet="")
        )

    # 周次里的年份与周号本身是事实。**补零与不补零两种写法都要收**——
    # `2026W02` 被数字扫描切成 `2026` 与 `02`，只登记 `2` 会让方案自己的周次
    # 在核验时被判成「查无实据」（fallback_text 的实测反例）。
    numbers.add(plan.iso_week[:4])
    numbers.add(plan.iso_week[-2:])
    numbers.add(plan.iso_week[-2:].lstrip("0") or "0")
    entities.add(plan.iso_week)

    return FactIndex(
        numbers=frozenset(numbers),
        entities=frozenset(entities),
        citations=tuple(citations),
        labels=labels,
    )


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def split_claims(text: str) -> list[str]:
    """按句切分。空句与纯标点句丢掉。"""
    return [s.strip() for s in _SENTENCE.findall(text) if s.strip(" 。！？；\n")]


def verify_claim(claim: str, index: FactIndex) -> GroundedClaim:
    """核验一句话。**确定性，没有模型参与。**"""
    numbers = _NUMBER.findall(claim)
    entities = _ENTITY.findall(claim)
    if not numbers and not entities:
        # 没有事实内容的句子无需核验，也不该算作「有依据」
        return GroundedClaim(claim=claim, citations=[], supported=True)

    bad_numbers = [n for n in numbers if not index.supports_number(n)]
    bad_entities = [e for e in entities if not index.supports_entity(e)]
    supported = not bad_numbers and not bad_entities
    citations = list(index.citations) if supported else []
    return GroundedClaim(claim=claim, citations=citations, supported=supported)


def verify_claims(text: str, index: FactIndex) -> GroundingReport:
    """逐句核验，产出 `GroundingReport`（v6 §7.4 `grounding_report`）。"""
    return GroundingReport(claims=[verify_claim(c, index) for c in split_claims(text)])


def rewrite_hint(report: GroundingReport, index: FactIndex) -> str:
    """把「哪几句没根据」翻译成给模型的重写要求。

    要具体到句子与数字，笼统地说「请核对事实」等于让它再猜一遍。
    """
    lines = ["以下句子里的数字或编号与事实对不上，请只用给定事实重写它们："]
    for claim in report.claims:
        if claim.supported:
            continue
        offending = [n for n in _NUMBER.findall(claim.claim) if not index.supports_number(n)]
        offending += [e for e in _ENTITY.findall(claim.claim) if not index.supports_entity(e)]
        lines.append(f"- 「{claim.claim}」（对不上的：{'、'.join(offending) or '未知'}）")
    lines.append("不确定的量就不要写。少写一句，好过写一个查无实据的数。")
    return "\n".join(lines)


@dataclass(frozen=True)
class Explanation:
    """解释生成的完整产物。"""

    text: str
    report: GroundingReport
    rewrites: int
    llm_calls: int
    skills_used: tuple[str, ...] = ()
    degraded: bool = False


def _facts_block(plan: SchedulePlan, index: FactIndex, validation: ValidationReport | None) -> str:
    summary: dict[str, Any] = {
        "计划编号": plan.plan_id,
        "ISO 周": plan.iso_week,
        "架次总数": len(plan.sorties),
        "带飞 / 单飞": f"{sum(1 for s in plan.sorties if len(s.crew) == 2)} / "
        f"{sum(1 for s in plan.sorties if len(s.crew) == 1)}",
        "阻塞项": len(plan.blocked_items),
        "欠账": len(plan.debts),
        "松弛档位": f"Tier {plan.relaxation_tier}",
    }
    if validation is not None:
        summary["校验"] = (
            f"{sum(1 for r in validation.results if r.passed)}/{len(validation.results)} 通过，"
            f"{len(validation.all_violations())} 条违规"
        )
    detail = "、".join(f"{index.labels[n]}={n}" for n in sorted(index.labels))
    return structured_summary("本次方案事实", summary) + f"\n可引用的量：{detail}"


def explain(
    plan: SchedulePlan,
    *,
    harness: Harness,
    validation: ValidationReport | None = None,
    stats: SolverStats | None = None,
    library: SkillLibrary | None = None,
    settings: Settings | None = None,
) -> Explanation:
    """生成 → 核验 → 重写（≤N 轮）。"""
    cfg = settings or get_settings()
    index = build_fact_index(plan, validation, stats)
    names = route_for_component("explain")
    blocks: list[ContextBlock] = []
    if library is not None and not library.empty and names:
        blocks.append(
            ContextBlock(kind="evidence", content=render_skills(library, names), label="skills")
        )
    blocks.append(
        ContextBlock(kind="summary", content=_facts_block(plan, index, validation), label="facts")
    )
    blocks.append(
        ContextBlock(
            kind="history",
            content="请用中文写一段面向排班员的方案说明，只使用上面给出的事实。",
            role="user",
        )
    )

    llm_calls = 0
    text = ""
    report = GroundingReport(claims=[])
    for attempt in range(cfg.EXPLAIN_MAX_REWRITES + 1):
        out = harness.call(EXPLAIN_AGENT, blocks)
        llm_calls += out.llm_calls
        if out.degraded:
            return Explanation(
                text=fallback_text(plan, validation),
                report=GroundingReport(claims=[]),
                rewrites=attempt,
                llm_calls=llm_calls,
                skills_used=names,
                degraded=True,
            )
        text = out.text.strip()
        report = verify_claims(text, index)
        if not report.unsupported_claims:
            return Explanation(
                text=text,
                report=report,
                rewrites=attempt,
                llm_calls=llm_calls,
                skills_used=names,
            )
        if attempt >= cfg.EXPLAIN_MAX_REWRITES:
            break
        blocks = [
            *blocks,
            ContextBlock(kind="decision", content=rewrite_hint(report, index), label="critic"),
        ]

    # 重写次数用完仍有不支持的断言：**如实交出去**，连同 GroundingReport。
    return Explanation(
        text=text,
        report=report,
        rewrites=cfg.EXPLAIN_MAX_REWRITES,
        llm_calls=llm_calls,
        skills_used=names,
    )


def fallback_text(plan: SchedulePlan, validation: ValidationReport | None) -> str:
    """LLM 不可用时的说明文字（FTS-4001 降级路径）。

    它是**拼出来的事实**，不是生成的文字——所以永远不会出现查无实据的数。
    排班能力不依赖 LLM，解释能力降级为「只报事实、不组织人话」。
    """
    parts = [
        f"{plan.iso_week} 排班方案 {plan.plan_id}：共 {len(plan.sorties)} 个架次，"
        f"其中带飞 {sum(1 for s in plan.sorties if len(s.crew) == 2)} 个、"
        f"单飞 {sum(1 for s in plan.sorties if len(s.crew) == 1)} 个。",
        f"松弛档位 Tier {plan.relaxation_tier}，欠账 {len(plan.debts)} 条，"
        f"因先修未满足被排除的组合 {len(plan.blocked_items)} 项。",
    ]
    if validation is not None:
        passed = sum(1 for r in validation.results if r.passed)
        parts.append(
            f"独立校验：{passed}/{len(validation.results)} 条通过，"
            f"共检查 {validation.total_checked_items} 项，"
            f"违规 {len(validation.all_violations())} 条。"
        )
    parts.append("（LLM 服务不可用，本段为事实直出，未经语言组织。）")
    return "".join(parts)


def unsupported_ratio(report: GroundingReport) -> float:
    """没有依据的断言占比。空报告返回 0.0——**没有断言不等于有问题**。"""
    if not report.claims:
        return 0.0
    return len(report.unsupported_claims) / len(report.claims)


def explain_summary(claims: Sequence[GroundedClaim]) -> str:
    return f"{sum(1 for c in claims if c.supported)}/{len(claims)} 句有事实依据"


__all__ = [
    "EXPLAIN_AGENT",
    "Explanation",
    "FactIndex",
    "build_fact_index",
    "explain",
    "explain_summary",
    "fallback_text",
    "rewrite_hint",
    "split_claims",
    "unsupported_ratio",
    "verify_claim",
    "verify_claims",
]
