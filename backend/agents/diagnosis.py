"""`DiagnosisAgent` 冲突诊断（v6 §7.2.2 / §3.9）—— 本系统两处受控自治之一。

> 自主决定探测哪些约束组、跑几轮 `probe_solve`（受独立预算池约束）。

## 自治在哪，边界在哪

**自治**：冲突集拿到手之后，先探哪一组、探几轮、怎么排序提案，取决于最小冲突集
的内容，运行前不可知。所以它是 Agent 而不是 LLM 节点。

**边界**（v6 §7.1.5）：

| 边界 | 落点 |
|---|---|
| 独立预算池：单次 30s / 5 次 / 累计 120s | `solver.diagnose.ProbeBudget`，与 Harness 的 LLM 预算**互不挤占** |
| 每条松弛提案必经 `probe_solve` 实证验证 | `verify_proposals`；探针判不可行的提案**直接丢弃** |
| 提案只影响 R1/R2，R0 恒不可松弛 | `RelaxationProposal` 契约层就把 `rule_tier == "R0"` 判非法 |
| 只在 INFEASIBLE 之后进场 | 此时不存在待输出的方案，自治影响不到正确性 |

`probe_solve` 是 `CLAUDE.md` 铁律 4 中确定性边界的**唯一例外**——只读探针，
不产出交付方案，结果必须经 `validate_node` 才能进入输出。

## 没有 LLM 也能诊断，这不是降级路径的补丁

`solver/diagnose.py`（M2-A）已经把「冲突集 → 归因 → 提案 → 实证验证」四步做成
确定性函数。Agent 加的是**探测顺序与深度的自主性**，不是诊断能力本身。
所以 `harness=None`（或 LLM 挂了）时，诊断照常给出完整结果，只是少了那层自主
探测——`autonomous=False` 如实标着。

反过来说，**Agent 不得引入未经探针验证的提案**：模型可以说「去试试放宽约束11」，
但那条提案能不能呈现，由 `probe_solve` 的返回决定，不由模型决定。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from backend.core.config import Settings, get_settings
from backend.core.errors import FTSError
from backend.harness import AgentSpec, ContextBlock, Harness, structured_summary
from backend.harness.types import ToolHandler
from backend.nodes.compile_spec import SpecBundle
from backend.schemas.solver import ConflictItem, RelaxationProposal
from backend.solver.candidates import CandidateSet, enumerate_candidates
from backend.solver.diagnose import (
    ConflictCore,
    Diagnosis,
    ProbeBudget,
    attribute,
    diagnose,
    draft_proposals,
    probe_solve,
    verify_proposals,
)
from backend.solver.model import RelaxationSettings

#: v6 §7.2.2 给 Diagnosis 的四个工具。ACL 行还允许检索类，本次不暴露——
#: **少给可以，多给不行**，而排班取数一律从 PG 读，不走检索（§7.1.5）。
DIAGNOSIS_TOOLS: Final[tuple[str, ...]] = (
    "min_conflict_set",
    "blame_chain",
    "probe_solve",
    "rank_relaxations",
)

DIAGNOSIS_AGENT: Final[AgentSpec] = AgentSpec(name="diagnosis", tools=DIAGNOSIS_TOOLS)


@dataclass(frozen=True)
class DiagnosisOutcome:
    """一次诊断的完整产物。"""

    conflicts: tuple[ConflictItem, ...]
    proposals: tuple[RelaxationProposal, ...]
    escalate: bool
    escalation_reason: str
    rounds: int
    llm_calls: int
    autonomous: bool
    probe_budget: Mapping[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def verified_proposals(self) -> tuple[RelaxationProposal, ...]:
        return tuple(p for p in self.proposals if p.verified)

    def summary(self) -> str:
        mode = "自主探测" if self.autonomous else "确定性诊断（无 LLM）"
        return (
            f"{mode}：{len(self.conflicts)} 个冲突组，"
            f"{len(self.proposals)} 条提案（{len(self.verified_proposals)} 条已验证），"
            f"探测 {self.rounds} 轮"
        )


# ─────────────────────────────────────────────────────────────────────
# 工具接线（返回值必须可 JSON 序列化 —— 要进 trace 与 Redis 缓存）
# ─────────────────────────────────────────────────────────────────────
def diagnosis_tool_handlers(
    bundle: SpecBundle,
    budget: ProbeBudget,
    *,
    core: ConflictCore,
    cset: CandidateSet,
) -> dict[str, ToolHandler]:
    """把四个诊断工具接到 M2-A 的实现上。

    `core` 与 `cset` 由确定性底座那一步算好后传进来——**不在 handler 里重算**。
    重算一次冲突集是一次完整的不可行性证明（基准周量级下要几十秒），而模型
    完全可能连着调三次 `min_conflict_set`。

    `probe_solve` 的 handler **自己扣预算**：模型想探几次就调几次，池子空了
    返回一条「预算耗尽」的结果而不是抛异常——超限不是异常，是一种要如实标注的
    结果（v6 §3.9.2）。
    """

    def min_conflict_set(_: dict[str, Any]) -> Any:
        return {
            "status": core.status,
            "sat_core_ids": list(core.sat_core_ids),
            "structural_ids": list(core.structural_ids),
            "group_ids": list(core.group_ids),
        }

    def blame_chain(args: dict[str, Any]) -> Any:
        person_id = str(args.get("person_id", ""))
        mission_id = str(args.get("mission_id", ""))
        items = attribute(bundle, cset, core)
        return [
            item.model_dump(mode="json")
            for item in items
            if not person_id
            or person_id in item.subjects
            or (mission_id and mission_id in item.subjects)
        ]

    def run_probe(args: dict[str, Any]) -> Any:
        if budget.is_exhausted():
            return {
                "status": "BUDGET_EXHAUSTED",
                "note": "⚠ 预算耗尽，未验证",
                "budget": budget.snapshot(),
            }
        tier = _tier_from_relaxations(args.get("relaxations", []))
        result, _ = probe_solve(bundle, relaxation=RelaxationSettings(tier=tier), budget=budget)
        return {
            "status": result.status if result is not None else "UNKNOWN",
            "sorties": result.sorties if result is not None else 0,
            "debts": len(result.debts) if result is not None else 0,
            "tier": tier,
            "budget": budget.snapshot(),
        }

    def rank(args: dict[str, Any]) -> Any:
        prefer = str(args.get("prefer", "least_debt"))
        ids = [str(p) for p in args.get("proposals", [])]
        return {"prefer": prefer, "order": sorted(ids)}

    return {
        "min_conflict_set": min_conflict_set,
        "blame_chain": blame_chain,
        "probe_solve": run_probe,
        "rank_relaxations": rank,
    }


def _tier_from_relaxations(relaxations: Any) -> int:
    """把模型给的松弛项名译成档位。认不出就取 Tier 1 —— **最保守的那一档**。

    往高里猜是危险的：Tier 3 会放宽 R1（架次上限），而那需要训练主任授权。
    探针虽然只是试算，试出来的「可行」会直接变成呈现给人的提案。
    """
    items = [str(r).upper() for r in (relaxations or [])]
    for tier in (3, 2, 1):
        if any(f"TIER{tier}" in item or f"TIER {tier}" in item for item in items):
            return tier
    if any("C11" in i or "C10" in i or "C12" in i or "约束11" in i for i in items):
        return 3
    if any("C03" in i or "约束3" in i for i in items):
        return 2
    return 1


# ─────────────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────────────
def _blocks(base: Diagnosis, budget: ProbeBudget, round_no: int) -> list[ContextBlock]:
    summary: dict[str, Any] = {
        "冲突组": [c.group_id for c in base.conflicts] or ["（求解器未给出）"],
        "涉及规则": sorted({r for c in base.conflicts for r in c.rule_ids}),
        "已起草提案": len(base.proposals),
        "探针余额": f"{budget.max_calls - budget.calls} 次 / "
        f"{max(0.0, budget.total_s - budget.spent_s):.0f}s",
        "本轮": round_no,
    }
    return [
        ContextBlock(kind="summary", content=structured_summary("当前诊断状态", summary)),
        ContextBlock(
            kind="history",
            content=(
                "请决定下一步：需要看哪条归因链、要不要再跑一次探针验证某个松弛档。"
                "都清楚了就不要再调工具。"
            ),
            role="user",
        ),
    ]


def run_diagnosis(
    bundle: SpecBundle,
    *,
    harness: Harness | None = None,
    budget: ProbeBudget | None = None,
    settings: Settings | None = None,
) -> DiagnosisOutcome:
    """跑一次完整诊断。"""
    cfg = settings or get_settings()
    pool = budget or ProbeBudget.from_settings()

    # ① 确定性底座：冲突集 → 归因 → 起草提案 → 实证验证（M2-A 的四步）
    base = diagnose(bundle, budget=pool, session=None)
    notes: list[str] = []

    if harness is None:
        return DiagnosisOutcome(
            conflicts=base.conflicts,
            proposals=base.proposals,
            escalate=base.escalate,
            escalation_reason=base.escalation_reason,
            rounds=0,
            llm_calls=0,
            autonomous=False,
            probe_budget=pool.snapshot(),
            notes=("未配置 Harness，仅给出确定性诊断结果",),
        )

    # ② 自主探测：模型决定还要看什么、还要探几轮
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    harness.registry.register_many(diagnosis_tool_handlers(bundle, pool, core=base.core, cset=cset))
    rounds = 0
    llm_calls = 0
    autonomous = True
    for round_no in range(1, cfg.DIAGNOSIS_MAX_ROUNDS + 1):
        if pool.is_exhausted():
            notes.append("探针预算耗尽，停止自主探测；已验证的提案照常呈现")
            break
        try:
            out = harness.call(DIAGNOSIS_AGENT, _blocks(base, pool, round_no))
        except FTSError as exc:
            notes.append(f"自主探测中断（{exc.message}），回落到确定性诊断结果")
            autonomous = False
            break
        llm_calls += out.llm_calls
        rounds = round_no
        if out.degraded:
            notes.append(f"自主探测降级（{out.error_code}），回落到确定性诊断结果")
            autonomous = False
            break
        if not out.calls:
            break  # 模型自己决定停 —— 这正是它的自治所在
        notes.extend(_tool_notes(out.calls, out.results))

    # ③ 探测完再验一次：模型可能指出了新的松弛方向，但**能不能呈现由探针说了算**
    proposals = base.proposals
    if autonomous and not pool.is_exhausted():
        proposals = verify_proposals(draft_proposals(bundle, base.core), bundle, budget=pool)

    return DiagnosisOutcome(
        conflicts=base.conflicts,
        proposals=proposals,
        escalate=base.escalate,
        escalation_reason=base.escalation_reason,
        rounds=rounds,
        llm_calls=llm_calls,
        autonomous=autonomous,
        probe_budget=pool.snapshot(),
        notes=tuple(notes),
    )


def _tool_notes(calls: Any, results: Any) -> list[str]:
    out: list[str] = []
    for call, result in zip(calls, results, strict=False):
        payload = result.value if result.ok else result.error
        out.append(f"{call.name}: {json.dumps(payload, ensure_ascii=False)[:200]}")
    return out


__all__ = [
    "DIAGNOSIS_AGENT",
    "DIAGNOSIS_TOOLS",
    "DiagnosisOutcome",
    "diagnosis_tool_handlers",
    "run_diagnosis",
]
