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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field

from backend.core.config import Settings, get_settings
from backend.core.errors import FTSError
from backend.harness import AgentSpec, ContextBlock, Harness, structured_summary
from backend.harness.types import ToolHandler
from backend.nodes.compile_spec import SpecBundle
from backend.schemas.intent import ConstraintSpec
from backend.schemas.solver import ConflictItem, RelaxationProposal, SolveStatus
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
from backend.solver.model import ConstraintGroup, RelaxationSettings

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


class ConstraintGroupCheckpoint(BaseModel):
    """诊断冲突组的 JSON 形态；不序列化 CP-SAT 句柄。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group_id: str
    rule_ids: tuple[int, ...]
    tier: str
    description: str
    relaxable: bool

    @classmethod
    def capture(cls, group: ConstraintGroup) -> ConstraintGroupCheckpoint:
        return cls(
            group_id=group.group_id,
            rule_ids=group.rule_ids,
            tier=group.tier,
            description=group.description,
            relaxable=group.relaxable,
        )

    def restore(self) -> ConstraintGroup:
        return ConstraintGroup(**self.model_dump())


class ConflictCoreCheckpoint(BaseModel):
    """严格重放所需的最小冲突集完整快照。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: SolveStatus
    group_ids: tuple[str, ...]
    groups: tuple[ConstraintGroupCheckpoint, ...]
    wall_time_s: float = Field(ge=0.0)
    num_candidates: int = Field(ge=0)
    sat_core_ids: tuple[str, ...] = ()
    structural_ids: tuple[str, ...] = ()

    @classmethod
    def capture(cls, core: ConflictCore) -> ConflictCoreCheckpoint:
        return cls(
            status=core.status,
            group_ids=core.group_ids,
            groups=tuple(ConstraintGroupCheckpoint.capture(group) for group in core.groups),
            wall_time_s=core.wall_time_s,
            num_candidates=core.num_candidates,
            sat_core_ids=core.sat_core_ids,
            structural_ids=core.structural_ids,
        )

    def restore(self) -> ConflictCore:
        return ConflictCore(
            status=self.status,
            group_ids=self.group_ids,
            groups=tuple(group.restore() for group in self.groups),
            wall_time_s=self.wall_time_s,
            num_candidates=self.num_candidates,
            sat_core_ids=self.sat_core_ids,
            structural_ids=self.structural_ids,
        )


class ProbeBudgetCheckpoint(BaseModel):
    """探针预算的完整状态；耗时只用于熔断，不进入 LLM 提示词。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    per_call_s: float = Field(ge=0.0)
    max_calls: int = Field(ge=0)
    total_s: float = Field(ge=0.0)
    calls: int = Field(ge=0)
    spent_s: float = Field(ge=0.0)

    @classmethod
    def capture(cls, budget: ProbeBudget) -> ProbeBudgetCheckpoint:
        return cls(
            per_call_s=budget.per_call_s,
            max_calls=budget.max_calls,
            total_s=budget.total_s,
            calls=budget.calls,
            spent_s=budget.spent_s,
        )

    def restore_into(self, budget: ProbeBudget) -> None:
        budget.per_call_s = self.per_call_s
        budget.max_calls = self.max_calls
        budget.total_s = self.total_s
        budget.calls = self.calls
        budget.spent_s = self.spent_s


class DiagnosisBaseCheckpoint(BaseModel):
    """Agent 自主循环开始前的确定性诊断底座。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: SolveStatus
    core: ConflictCoreCheckpoint
    conflicts: tuple[ConflictItem, ...]
    proposals: tuple[RelaxationProposal, ...]
    escalate: bool
    escalation_reason: str
    budget: ProbeBudgetCheckpoint

    @classmethod
    def capture(cls, base: Diagnosis, budget: ProbeBudget) -> DiagnosisBaseCheckpoint:
        return cls(
            status=base.status,
            core=ConflictCoreCheckpoint.capture(base.core),
            conflicts=base.conflicts,
            proposals=base.proposals,
            escalate=base.escalate,
            escalation_reason=base.escalation_reason,
            budget=ProbeBudgetCheckpoint.capture(budget),
        )

    def restore(self, budget: ProbeBudget) -> Diagnosis:
        self.budget.restore_into(budget)
        return Diagnosis(
            status=self.status,
            core=self.core.restore(),
            conflicts=self.conflicts,
            proposals=self.proposals,
            escalate=self.escalate,
            escalation_reason=self.escalation_reason,
            budget=budget.snapshot(),
        )


class DiagnosisOutcomeCheckpoint(BaseModel):
    """Agent 完成后的结构化产物；用于验证并恢复最终黑板状态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflicts: tuple[ConflictItem, ...]
    proposals: tuple[RelaxationProposal, ...]
    escalate: bool
    escalation_reason: str
    rounds: int = Field(ge=0)
    llm_calls: int = Field(ge=0)
    autonomous: bool
    probe_budget: dict[str, float]
    notes: tuple[str, ...]

    @classmethod
    def capture(cls, outcome: DiagnosisOutcome) -> DiagnosisOutcomeCheckpoint:
        return cls(**outcome.__dict__)

    def restore(self) -> DiagnosisOutcome:
        return DiagnosisOutcome(
            conflicts=self.conflicts,
            proposals=self.proposals,
            escalate=self.escalate,
            escalation_reason=self.escalation_reason,
            rounds=self.rounds,
            llm_calls=self.llm_calls,
            autonomous=self.autonomous,
            probe_budget=dict(self.probe_budget),
            notes=self.notes,
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
    allowed_proposal_ids: tuple[str, ...] = (),
) -> dict[str, ToolHandler]:
    """把四个诊断工具接到 M2-A 的实现上。

    `core` 与 `cset` 由确定性底座那一步算好后传进来——**不在 handler 里重算**。
    重算一次冲突集是一次完整的不可行性证明（基准周量级下要几十秒），而模型
    完全可能连着调三次 `min_conflict_set`。

    `probe_solve` 的 handler **自己扣预算**：模型想探几次就调几次，池子空了
    返回一条「预算耗尽」的结果而不是抛异常——超限不是异常，是一种要如实标注的
    结果（v6 §3.9.2）。
    """

    def assert_target_week(args: dict[str, Any]) -> None:
        actual = str(args.get("iso_week", ""))
        expected = bundle.spec.iso_week
        if actual != expected:
            raise ValueError(f"iso_week 必须是当前诊断目标 {expected}，实际收到 {actual!r}")

    def min_conflict_set(args: dict[str, Any]) -> Any:
        assert_target_week(args)
        actual_scope = list(args.get("scope_persons") or [])
        expected_scope = _tool_scope_persons(bundle.spec)
        if actual_scope != expected_scope:
            raise ValueError(
                f"scope_persons 必须是当前诊断范围 {expected_scope}，实际收到 {actual_scope}"
            )
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
        assert_target_week(args)
        if budget.is_exhausted():
            return {
                "status": "BUDGET_EXHAUSTED",
                "note": "⚠ 预算耗尽，未验证",
                "budget": _prompt_budget(budget),
            }
        tier = _tier_from_relaxations(args.get("relaxations", []))
        result, _ = probe_solve(bundle, relaxation=RelaxationSettings(tier=tier), budget=budget)
        return {
            "status": result.status if result is not None else "UNKNOWN",
            "sorties": result.sorties if result is not None else 0,
            "debts": len(result.debts) if result is not None else 0,
            "tier": tier,
            "budget": _prompt_budget(budget),
            # 重放器会绕过真实 handler；完整状态只供 Python 恢复预算熔断，
            # 下一轮提示词仍只读取 ``_prompt_budget`` 的确定性调用数字段。
            "budget_state": ProbeBudgetCheckpoint.capture(budget).model_dump(mode="json"),
        }

    def rank(args: dict[str, Any]) -> Any:
        prefer = str(args.get("prefer", "least_debt"))
        ids = [str(p) for p in args.get("proposals", [])]
        unknown = sorted(set(ids) - set(allowed_proposal_ids))
        if unknown:
            raise ValueError(
                f"proposals 只能引用当前确定性诊断已生成的提案 ID；未知 ID: {', '.join(unknown)}"
            )
        return {"prefer": prefer, "order": sorted(ids)}

    return {
        "min_conflict_set": min_conflict_set,
        "blame_chain": blame_chain,
        "probe_solve": run_probe,
        "rank_relaxations": rank,
    }


def _tier_from_relaxations(relaxations: Any) -> int:
    """把模型给的**规范**松弛档位译成整数。

    探针结果会影响最终呈现的提案，未知字符串不能再静默落到 Tier 1；否则
    ``C13_frequency`` 这类语义标签会被误当成已选择的档位。空列表保持历史
    默认 Tier 1，便于模型只表达「探测当前最保守方向」的调用。
    """
    if relaxations is None:
        items: list[Any] = []
    elif isinstance(relaxations, (list, tuple)):
        items = list(relaxations)
    else:
        raise ValueError("relaxations 必须是规范档位 ID 列表")

    aliases = {"TIER_1": 1, "TIER1": 1, "TIER_2": 2, "TIER2": 2, "TIER_3": 3, "TIER3": 3}
    tiers: set[int] = set()
    for raw in items:
        if not isinstance(raw, str):
            raise ValueError("relaxations 只能包含规范档位 ID：TIER_1、TIER_2 或 TIER_3")
        key = raw.strip().upper()
        if key not in aliases:
            raise ValueError(
                f"未知松弛档位 ID；只能使用 TIER_1、TIER_2 或 TIER_3，实际收到 {raw!r}"
            )
        tiers.add(aliases[key])
    if len(tiers) > 1:
        raise ValueError("一次 probe_solve 只能指定一个松弛档位")
    return next(iter(tiers), 1)


# ─────────────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────────────
def _blocks(
    base: Diagnosis,
    budget: ProbeBudget,
    round_no: int,
    *,
    spec: ConstraintSpec,
    required_tool: str | None = None,
    completed: set[str] | None = None,
    probe_history: list[dict[str, Any]] | None = None,
) -> list[ContextBlock]:
    prior_probes = probe_history or []
    tried_tiers = {
        str(entry.get("tier")) for entry in prior_probes if entry.get("tier") is not None
    }
    summary: dict[str, Any] = {
        "目标 ISO 周": spec.iso_week,
        "min_conflict_set.scope_persons": _tool_scope_persons(spec),
        "固定工具参数": json.dumps(
            {
                "iso_week": spec.iso_week,
                "scope_persons": _tool_scope_persons(spec),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "课目范围": spec.scope_missions,
        "冲突组": [c.group_id for c in base.conflicts] or ["（求解器未给出）"],
        "涉及规则": sorted({r for c in base.conflicts for r in c.rule_ids}),
        "已起草提案": len(base.proposals),
        "probe_solve.relaxations 合法档位": ["TIER_1", "TIER_2", "TIER_3"],
        "尚未探测的档位": [
            tier for tier in ("TIER_1", "TIER_2", "TIER_3") if tier not in tried_tiers
        ],
        "已探测档位与结果": prior_probes if prior_probes else cast(Any, ["（无）"]),
        "rank_relaxations.proposals 合法 ID": [p.proposal_id for p in base.proposals],
        # 实测耗时只用于确定性预算闸，不进入 LLM 请求。否则相同输入仅因机器
        # 负载不同就会改变 request_key，严格 record/replay 必然失配。
        "探针余额": f"{max(0, budget.max_calls - budget.calls)} 次",
        "本轮": round_no,
        "本轮必需工具": required_tool or "（无，可停止）",
        "已完成工具": sorted(completed or set()),
    }
    return [
        ContextBlock(kind="summary", content=structured_summary("当前诊断状态", summary)),
        ContextBlock(
            kind="history",
            content=(
                "先完成本轮标出的必需工具；需要时可同时查看归因链或追加探针。"
                "工具参数 iso_week 必须逐字复制“目标 ISO 周”，不得自行换算或猜测。"
                "probe_solve.relaxations 只能逐字复制合法档位中的一个；"
                "需要追加探针时选择尚未探测的档位，不重复无新证据的同档探针；"
                "rank_relaxations.proposals 只能逐字复制当前合法提案 ID。"
                "必需工具已完成且结论清楚后就停止。"
            ),
            role="user",
        ),
    ]


def _tool_scope_persons(spec: ConstraintSpec) -> list[str]:
    """把规格的 ``ALL`` 映射成工具契约采用的列表形态。"""
    return ["ALL"] if spec.scope_persons == "ALL" else list(spec.scope_persons)


def _prompt_budget(budget: ProbeBudget) -> dict[str, int]:
    """返回可进入 LLM 上下文的确定性预算字段。

    ``spent_s`` / 剩余秒数仍由 :class:`ProbeBudget` 精确维护并执行熔断，但它们
    是运行时观测量，不能作为下一次请求的语义输入。调用次数由轨迹本身决定，
    同一轨迹下稳定，足以让模型知道还可探几轮。
    """
    return {
        "calls": budget.calls,
        "max_calls": budget.max_calls,
        "remaining_calls": max(0, budget.max_calls - budget.calls),
    }


def run_diagnosis(
    bundle: SpecBundle,
    *,
    harness: Harness | None = None,
    budget: ProbeBudget | None = None,
    settings: Settings | None = None,
    base_checkpoint: DiagnosisBaseCheckpoint | None = None,
    outcome_checkpoint: DiagnosisOutcomeCheckpoint | None = None,
    checkpoint_observer: Callable[[str, BaseModel], None] | None = None,
) -> DiagnosisOutcome:
    """跑一次完整诊断。"""
    cfg = settings or get_settings()
    pool = budget or ProbeBudget.from_settings()

    # ① 确定性底座：冲突集 → 归因 → 起草提案 → 实证验证（M2-A 的四步）
    if base_checkpoint is None:
        base = diagnose(bundle, budget=pool, session=None)
    else:
        base = base_checkpoint.restore(pool)
    if checkpoint_observer is not None:
        checkpoint_observer("diagnosis_base", DiagnosisBaseCheckpoint.capture(base, pool))
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
    harness.registry.register_many(
        diagnosis_tool_handlers(
            bundle,
            pool,
            core=base.core,
            cset=cset,
            allowed_proposal_ids=tuple(p.proposal_id for p in base.proposals),
        )
    )
    rounds = 0
    llm_calls = 0
    autonomous = True
    completed: set[str] = set()
    probe_successes = 0
    probe_history: list[dict[str, Any]] = []
    #: 一旦自主探针确认有架次可排，下一轮必须收口到 rank；不能再以空响应
    #: 或重复 probe 静默结束。底座已有可呈现提案时，首轮自主 probe 成功同样
    #: 必须进入 rank。
    presentable_after_probe = False
    base_has_presentable = bool(base.useful_proposals)
    # 已经确认“现有松弛仍只能给出 0 架次”的场景，需要再探一个不同方向后再
    # 升级人工；有可用提案的场景至少补一轮自主探针即可进入排序。
    required_probe_count = 2 if base.escalate else 1
    for round_no in range(1, cfg.DIAGNOSIS_MAX_ROUNDS + 1):
        if "min_conflict_set" not in completed:
            required_tool = "min_conflict_set"
        elif presentable_after_probe and "rank_relaxations" not in completed:
            required_tool = "rank_relaxations"
        elif probe_successes < required_probe_count and not pool.is_exhausted():
            required_tool = "probe_solve"
        # 只给经过真实探针验证的提案排序。预算耗尽时 ``diagnose`` 会保留带
        # 「未验证」标记的提案；让模型给这类提案排名既没有证据基础，也会让
        # trajectory 的「预算已耗尽 → 只取冲突集」场景多出一次假动作。
        elif base.useful_proposals and "rank_relaxations" not in completed:
            required_tool = "rank_relaxations"
        else:
            break
        if pool.is_exhausted() and required_tool == "probe_solve":
            notes.append("探针预算耗尽，停止自主探测；已验证的提案照常呈现")
            break
        try:
            staged_agent = DIAGNOSIS_AGENT.model_copy(
                update={
                    # v6 §7.2.2 固定暴露 Diagnosis 的四个合法工具；
                    # required_tools 只约束本轮必须完成的工具，不改变 ACL schema 集合。
                    "tools": DIAGNOSIS_TOOLS,
                    "required_tools": (required_tool,),
                }
            )
            out = harness.call(
                staged_agent,
                _blocks(
                    base,
                    pool,
                    round_no,
                    spec=bundle.spec,
                    required_tool=required_tool,
                    completed=completed,
                    probe_history=probe_history,
                ),
            )
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
            notes.append(f"{required_tool} 未完成：模型未发出调用")
            if required_tool == "rank_relaxations":
                notes.append("已有可呈现提案但未完成排序，升级人工")
            break
        succeeded = {
            call.name for call, result in zip(out.calls, out.results, strict=False) if result.ok
        }
        completed.update(succeeded)
        if "probe_solve" in succeeded:
            _restore_probe_budget(out.calls, out.results, pool)
            probe_successes += 1
            probe_history.extend(_probe_observations(out.calls, out.results))
            presentable_after_probe = (
                presentable_after_probe
                or base_has_presentable
                or _probe_is_presentable(out.calls, out.results)
            )
        if required_tool not in succeeded:
            failed = next(
                (
                    result.error
                    for call, result in zip(out.calls, out.results, strict=False)
                    if call.name == required_tool and not result.ok
                ),
                "未得到成功的工具结果",
            )
            notes.append(f"{required_tool} 未完成：{failed}")
            continue
        notes.extend(_tool_notes(out.calls, out.results))
        if "rank_relaxations" in succeeded:
            break

    rank_pending = (base_has_presentable or presentable_after_probe) and (
        "rank_relaxations" not in completed
    )
    if rank_pending:
        notes.append("探针已确认存在可呈现提案，但排序未完成，升级人工")

    # ③ 探测完再验一次：模型可能指出了新的松弛方向，但**能不能呈现由探针说了算**
    proposals = base.proposals
    if outcome_checkpoint is not None:
        proposals = outcome_checkpoint.proposals
    elif autonomous and not pool.is_exhausted():
        proposals = verify_proposals(draft_proposals(bundle, base.core), bundle, budget=pool)

    outcome = DiagnosisOutcome(
        conflicts=base.conflicts,
        proposals=proposals,
        escalate=base.escalate or rank_pending,
        escalation_reason=(
            "已有可呈现提案但 rank_relaxations 未完成，升级人工"
            if rank_pending
            else base.escalation_reason
        ),
        rounds=rounds,
        llm_calls=llm_calls,
        autonomous=autonomous,
        probe_budget=pool.snapshot(),
        notes=tuple(notes),
    )
    if outcome_checkpoint is not None:
        _assert_replay_control(outcome, outcome_checkpoint)
        outcome = outcome_checkpoint.restore()
    if checkpoint_observer is not None:
        checkpoint_observer("diagnosis_outcome", DiagnosisOutcomeCheckpoint.capture(outcome))
    return outcome


def _restore_probe_budget(calls: Any, results: Any, budget: ProbeBudget) -> None:
    """从冻结的工具返回恢复预算；record 路径重复赋同值，replay 路径补齐短路副作用。"""
    for call, result in zip(calls, results, strict=False):
        if call.name != "probe_solve" or not result.ok or not isinstance(result.value, Mapping):
            continue
        raw = result.value.get("budget_state")
        if isinstance(raw, Mapping):
            ProbeBudgetCheckpoint.model_validate(raw).restore_into(budget)


def _assert_replay_control(actual: DiagnosisOutcome, expected: DiagnosisOutcomeCheckpoint) -> None:
    """冻结最终数据不能掩盖 Agent 控制流变化。"""
    observed = {
        "rounds": actual.rounds,
        "llm_calls": actual.llm_calls,
        "autonomous": actual.autonomous,
    }
    wanted = {
        "rounds": expected.rounds,
        "llm_calls": expected.llm_calls,
        "autonomous": expected.autonomous,
    }
    if observed != wanted:
        raise RuntimeError(f"Diagnosis Agent 重放控制流与录制不一致：{observed!r} != {wanted!r}")


def _tool_notes(calls: Any, results: Any) -> list[str]:
    out: list[str] = []
    for call, result in zip(calls, results, strict=False):
        payload = result.value if result.ok else result.error
        out.append(f"{call.name}: {json.dumps(payload, ensure_ascii=False, sort_keys=True)[:200]}")
    return out


def _probe_is_presentable(calls: Any, results: Any) -> bool:
    """判断本轮 probe 是否已经给出可呈现的解证据。"""
    for call, result in zip(calls, results, strict=False):
        if call.name != "probe_solve" or not result.ok or not isinstance(result.value, Mapping):
            continue
        if result.value.get("status") in {"OPTIMAL", "FEASIBLE"}:
            try:
                return int(result.value.get("sorties", 0)) > 0
            except (TypeError, ValueError):
                return False
    return False


def _probe_observations(calls: Any, results: Any) -> list[dict[str, Any]]:
    """抽取可安全回灌的探针摘要，避免 Agent 重复同档位无效探索。"""
    observations: list[dict[str, Any]] = []
    for call, result in zip(calls, results, strict=False):
        if call.name != "probe_solve" or not result.ok:
            continue
        relaxations = call.arguments.get("relaxations") or ["TIER_1"]
        tier = str(relaxations[0]) if isinstance(relaxations, list) and relaxations else "TIER_1"
        value = result.value if isinstance(result.value, Mapping) else {}
        observations.append(
            {
                "tier": tier,
                "status": str(value.get("status", "UNKNOWN")),
                "sorties": int(value.get("sorties", 0) or 0),
            }
        )
    return observations


__all__ = [
    "DIAGNOSIS_AGENT",
    "DIAGNOSIS_TOOLS",
    "DiagnosisBaseCheckpoint",
    "DiagnosisOutcome",
    "DiagnosisOutcomeCheckpoint",
    "ProbeBudgetCheckpoint",
    "diagnosis_tool_handlers",
    "run_diagnosis",
]
