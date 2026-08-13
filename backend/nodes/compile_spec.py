"""`compile_spec` 确定性节点：`ruleset.yaml + semantics.yaml + SolveIntent → ConstraintSpec`。

**本节点不经 Harness、不读 Skill、不注册为任何 LLM 组件的工具**（CLAUDE.md 铁律 4）。
冲突时以 ruleset 为准（v6 §7.3.2）—— `SolveIntent` 只能调四类旋钮
（范围 / 冻结策略 / 目标权重 / 松弛档位），碰不到任何一条硬约束的参数。

## 两项额外职责（v6 §7.2.4 / §6.3）

1. **S-01 类别先修展开**：`mission_prereq.prereq_ref` 若是类别（如「A类」）在此
   展开为「该类全部课目」，不在 SQL 里做（v6 §6.1）。展开只调
   :func:`backend.retrieval.prereq_cte.evaluate_prereq`，**不另写一份**——
   同一个语义两份实现必然漂移。
2. **S-11 复训标记写入**：把成熟飞行员的到期资质写成
   `is_recurrent=TRUE, recurrent_since=到期次日`（v6 §6.3）。

## `training_progress` 是物化视图，不是独立真源（v6 §6.3.2）

它的主键 `(person_id, mission_id, cycle_start)` **不含 `snapshot_id`**，所以：

- 全库唯一，两个快照没法各存一份；
- 重算覆盖时必须按**主键**清旧行。只按 `snapshot_id` 清，会在「内容变了 →
  snapshot_id 变了 → 主键没变」时撞唯一约束（M1 实测踩过这个坑）；
- 真源是 `person_completed_missions` 等事实表，本表由它们物化而来。

**`cycle_start` 不在这里发明**（S-14）。它只有两个来源：课目文件的「课程开始
日期」列，或用户对 `Q_cycle_start` 的回答，两者都在摄取期落库。本节点读不到某个
(人, 课目) 的进度行时抛 `FTS-1004` 提示补摄取，**绝不编一个日期**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from langgraph.types import Command
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.core.errors import DataConflictError, RequiredInputMissingError
from backend.core.ruleset import Ruleset, Semantics, get_ruleset, get_semantics, req_max_for
from backend.graph.events import emit
from backend.graph.state import FTSState, model_get
from backend.graph.state import get as state_get
from backend.models.progress import TrainingProgress
from backend.retrieval.prereq_cte import evaluate_prereq
from backend.schemas.intent import ConstraintSpec, ObjectiveWeights, SolveIntent
from backend.solver.data import (
    NO_OVERRIDES,
    WEEK_DAYS,
    ProblemData,
    ScenarioOverrides,
    load_problem_data,
)


@dataclass(frozen=True)
class SpecBundle:
    """`compile_spec` 的产物：规格 + 该规格对应的实体快照。

    两者一起交给求解器，避免「规格按一个快照编、实体按另一个快照读」这类
    最难查的错。
    """

    spec: ConstraintSpec
    data: ProblemData
    ruleset: Ruleset
    semantics: Semantics


def default_intent(
    *,
    freeze_policy: str = "BALANCED",
    freeze_reason: str = "默认档：受影响架次 + 同日同机同人的关联架次",
    weights: ObjectiveWeights | None = None,
) -> SolveIntent:
    """一个不带任何 LLM 参与的 `SolveIntent`（全量排班的中性默认值）。

    真实链路里它由 Planner 生成（W7/M4-B）。本函数存在的意义是让求解器可以
    **脱离 LLM 单独跑**：CI 与本窗口的全部测试都走这条路径。
    """
    return SolveIntent(
        scope_persons="ALL",
        scope_missions="ALL",
        freeze_policy=freeze_policy,  # type: ignore[arg-type]
        freeze_reason=freeze_reason,
        objective_weights=weights or ObjectiveWeights(progress=1.0, disruption=1.0, balance=1.0),
        pre_authorized_tiers=[0],
        incremental_constraints=[],
        estimated_blast_radius=0,
        open_questions=[],
    )


def compile_spec(
    session: Session,
    *,
    snapshot_id: str,
    week_start: date,
    intent: SolveIntent | None = None,
    relaxation_tier: int = 0,
    overrides: ScenarioOverrides = NO_OVERRIDES,
    time_limit_s: float | None = None,
    workers: int | None = None,
    seed: int | None = None,
    ruleset: Ruleset | None = None,
    semantics: Semantics | None = None,
    materialize: bool = True,
) -> SpecBundle:
    """编译一次排班的完整规格。

    `materialize=False` 用于只读场景（探针、诊断复跑）——它们不该反复重写
    `training_progress`。
    """
    from backend.core.config import get_settings

    rules = ruleset or get_ruleset()
    sem = semantics or get_semantics()
    settings = get_settings()
    the_intent = intent or default_intent()

    if week_start.weekday() != 0:
        raise RequiredInputMissingError(
            f"排班周起点必须是周一，实际 {week_start}（{week_start.strftime('%A')}）"
        )

    data = load_problem_data(
        session,
        snapshot_id=snapshot_id,
        week_start=week_start,
        window_start=rules.window_start,
        window_end=rules.window_end,
        overrides=overrides,
    )

    if materialize:
        materialize_progress(
            session, data=data, semantics=sem, ruleset=rules, snapshot_id=snapshot_id
        )
        # 重新读一遍：后续候选枚举要用刚写进去的 is_recurrent / prereq_met
        data = load_problem_data(
            session,
            snapshot_id=snapshot_id,
            week_start=week_start,
            window_start=rules.window_start,
            window_end=rules.window_end,
            overrides=overrides,
        )

    airspace_capacity = {aid: data.capacity_of(aid) for aid in sorted(data.airspaces)}
    diffs = rules.cross_check_airspace_capacity(
        {aid: space.capacity for aid, space in data.airspaces.items()}
    )
    if diffs and overrides.is_empty():
        # 只是记账用的交叉核对：PG 是真源，用户换一批数据时空域本来就会变。
        # 差异写进 spec 的 semantics_switches 会污染 sha256，故只在日志层面体现。
        from backend.core.logging import get_logger

        get_logger(__name__).warning(
            "空域容量与 ruleset 抄录值不一致（以 PG 为准）", extra={"diffs": list(diffs)}
        )

    spec = ConstraintSpec(
        snapshot_id=snapshot_id,
        ruleset_version=rules.version,
        semantics_version=sem.version,
        semantics_switches=sem.snapshot(),
        iso_week=data.iso_week,
        week_start=data.week_start,
        week_end=data.week_end,
        scope_persons=the_intent.scope_persons,
        scope_missions=the_intent.scope_missions,
        relaxation_tier=relaxation_tier,
        objective_weights=the_intent.objective_weights,
        incremental_constraints=list(the_intent.incremental_constraints),
        runway_model="dual_runway" if sem.s05_dual_runway else "single_runway",
        runways={
            rid: sorted(rwy.aircraft_types)
            for rid, rwy in sorted(data.runways.items())
            if rid not in overrides.closed_runways
        },
        density_scope=dict(sem.s05_density_scope),
        airspace_capacity=airspace_capacity,
        freq_days={mid: m.freq_days for mid, m in sorted(data.missions.items())},
        req_max={mid: req_max_for(m.freq_days) for mid, m in sorted(data.missions.items())},
        solver_seed=settings.SOLVER_SEED if seed is None else seed,
        solver_workers=settings.SOLVER_WORKERS if workers is None else workers,
        solver_time_limit_s=(
            settings.SOLVER_TIME_LIMIT_S if time_limit_s is None else time_limit_s
        ),
    )
    return SpecBundle(spec=spec, data=data, ruleset=rules, semantics=sem)


# ─────────────────────────────────────────────────────────────────────
# training_progress 物化
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ProgressUpdate:
    """一行 `training_progress` 的重算结果（只覆盖本节点负责的四个字段）。"""

    person_id: str
    mission_id: str
    cycle_start: date
    prereq_met: bool
    blocked_reason: str | None
    is_recurrent: bool
    recurrent_since: date | None


def recurrent_since_for(
    *,
    identity: str,
    expiry: date | None,
    semantics: Semantics,
) -> date | None:
    """S-11：成熟飞行员的到期资质，自**到期次日**起进入复训周期。

    返回 None 表示不适用（S-11 关闭、身份不适用、或该资质没有到期日）。
    学员与教员对约束2 按字面执行，不进复训（v6 §1.2.4）。
    """
    if not semantics.s11_enabled or expiry is None:
        return None
    if identity not in semantics.s11_identities:
        return None
    return expiry + timedelta(days=semantics.s11_start_offset_days)


def compute_progress_updates(
    data: ProblemData, *, semantics: Semantics, existing: dict[tuple[str, str], date]
) -> list[ProgressUpdate]:
    """重算 `prereq_met` / `blocked_reason` / `is_recurrent` / `recurrent_since`。

    先修判定直接调 :func:`backend.retrieval.prereq_cte.evaluate_prereq`（S-01
    类展开的唯一实现）。
    """
    mission_ids = sorted(data.missions)
    updates: list[ProgressUpdate] = []
    for (person_id, mission_id), cycle_start in sorted(existing.items()):
        person = data.persons.get(person_id)
        mission = data.missions.get(mission_id)
        if person is None or mission is None:
            continue
        met, missing = evaluate_prereq(mission.prereqs, person.completed, mission_ids)
        qual = person.qual(mission.mission_class)
        since = recurrent_since_for(
            identity=person.identity,
            expiry=qual.expiry if qual else None,
            semantics=semantics,
        )
        # 复训周期在本排班周结束前已经开始 → 落锚点（本周是否强制安排由 §3.5 决定）
        is_recurrent = since is not None and since <= data.week_end
        updates.append(
            ProgressUpdate(
                person_id=person_id,
                mission_id=mission_id,
                cycle_start=cycle_start,
                prereq_met=met,
                blocked_reason=None if met else blocked_reason_text(missing),
                is_recurrent=is_recurrent,
                recurrent_since=since if is_recurrent else None,
            )
        )
    return updates


def blocked_reason_text(missing: tuple[str, ...]) -> str:
    """阻塞原因的统一措辞。

    v6 §12.3 要求 Sheet 4 区块 4 的「缺失先修」列逐字为「missionA-2 未完成」，
    所以这里就产出那个措辞，**不再让报表层二次拼装**（拼两遍必然对不上）。
    """
    return "、".join(f"{m} 未完成" for m in missing)


def materialize_progress(
    session: Session,
    *,
    data: ProblemData,
    semantics: Semantics,
    ruleset: Ruleset,  # noqa: ARG001 - 保留签名对称性，规则参数目前不参与本步
    snapshot_id: str,
) -> list[ProgressUpdate]:
    """重算并**按主键**覆盖 `training_progress`（v6 §6.3.2）。

    步骤：读出本快照涉及的 (人, 课目) 现有行 → 重算四个字段 → 按主键 delete →
    重新 insert。**不是 UPDATE**：这样「内容变了 → snapshot_id 变了 → 主键没变」
    的场景也能干净覆盖，不会撞唯一约束。
    """
    rows = list(
        session.execute(
            select(TrainingProgress).where(TrainingProgress.snapshot_id == snapshot_id)
        ).scalars()
    )
    if not rows:
        raise RequiredInputMissingError(
            f"快照 {snapshot_id} 下没有任何 training_progress 行 —— "
            "cycle_start 只能来自课目文件的「课程开始日期」列或用户对 Q_cycle_start 的回答"
            "（S-14），本节点不发明日期。请先跑摄取管线。"
        )

    existing = {(r.person_id, r.mission_id): r.cycle_start for r in rows}
    keep = {
        (r.person_id, r.mission_id): (
            r.status,
            r.completed_count,
            r.last_done_date,
            r.cycle_weeks,
            r.debt_count,
        )
        for r in rows
    }
    updates = compute_progress_updates(data, semantics=semantics, existing=existing)

    for upd in updates:
        session.execute(
            delete(TrainingProgress).where(
                TrainingProgress.person_id == upd.person_id,
                TrainingProgress.mission_id == upd.mission_id,
                TrainingProgress.cycle_start == upd.cycle_start,
            )
        )
    session.flush()

    for upd in updates:
        status, completed_count, last_done, cycle_weeks, debt_count = keep[
            upd.person_id, upd.mission_id
        ]
        session.add(
            TrainingProgress(
                person_id=upd.person_id,
                mission_id=upd.mission_id,
                cycle_start=upd.cycle_start,
                status=status,
                completed_count=completed_count,
                last_done_date=last_done,
                cycle_weeks=cycle_weeks,
                debt_count=debt_count,
                prereq_met=upd.prereq_met,
                blocked_reason=upd.blocked_reason,
                is_recurrent=upd.is_recurrent,
                recurrent_since=upd.recurrent_since,
                snapshot_id=snapshot_id,
            )
        )
    session.flush()
    return updates


def week_dates(week_start: date) -> tuple[date, ...]:
    return tuple(week_start + timedelta(days=i) for i in range(WEEK_DAYS))


# ─────────────────────────────────────────────────────────────────────
# 图节点（M4-B）
# ─────────────────────────────────────────────────────────────────────
def intent_from_spec(spec: ConstraintSpec) -> SolveIntent:
    """从已编译的 `ConstraintSpec` 反推出等价的 `SolveIntent`。

    **为什么需要它**：`SpecBundle` 里的 `ProblemData` 不进黑板——它有几万个
    字段，塞进 checkpoint 既撑爆存储，也让「跨日恢复」变成「跨日反序列化一个
    快照的全量实体」。所以黑板上只留 `ConstraintSpec`（一个扁平的 Pydantic
    模型），要用时由 :func:`bundle_from_spec` 按它重新装配。

    反推是**无损**的：`compile_spec` 从 `SolveIntent` 里只取范围、权重、增量
    约束三样，三样都原样落在了 `ConstraintSpec` 上。冻结策略与预授权档位不
    参与编译（前者在 Planner 侧决定冻结集，后者已折算成 `relaxation_tier`），
    所以这里给的是中性值，且**不会影响重新装配的结果**。
    """
    return SolveIntent(
        scope_persons=spec.scope_persons,
        scope_missions=spec.scope_missions,
        freeze_policy="BALANCED",
        freeze_reason="由 ConstraintSpec 反推（冻结策略不参与编译，见 intent_from_spec）",
        objective_weights=spec.objective_weights,
        pre_authorized_tiers=[spec.relaxation_tier],
        incremental_constraints=list(spec.incremental_constraints),
        estimated_blast_radius=0,
        open_questions=[],
    )


def bundle_from_spec(
    session: Session,
    spec: ConstraintSpec,
    *,
    overrides: ScenarioOverrides = NO_OVERRIDES,
    materialize: bool = False,
) -> SpecBundle:
    """按黑板上的 `ConstraintSpec` 重新装配 `SpecBundle`。

    `materialize=False` 是默认值：`training_progress` 已经在
    :func:`compile_spec_node` 那一步物化过了，重复物化既慢又会在同一次运行里
    写两遍同样的行。

    编译是确定性的，所以重新装配出来的 `spec` 必须与传进来的**逐字段相等**；
    不等就说明快照在两次编译之间变了。那种情况下继续跑，会得到一个「按新数据
    求解、按旧规格记账」的方案 —— 本函数直接抛，不猜。
    """
    rebuilt = compile_spec(
        session,
        snapshot_id=spec.snapshot_id,
        week_start=spec.week_start,
        intent=intent_from_spec(spec),
        relaxation_tier=spec.relaxation_tier,
        overrides=overrides,
        time_limit_s=spec.solver_time_limit_s,
        workers=spec.solver_workers,
        seed=spec.solver_seed,
        materialize=materialize,
    )
    if rebuilt.spec != spec:
        raise DataConflictError(
            "按黑板上的 ConstraintSpec 重新编译，得到的规格与原规格不一致 —— "
            "快照或规则集在两次编译之间变了。继续跑会按新数据求解、按旧规格记账",
            details={
                "snapshot_id": spec.snapshot_id,
                "iso_week": spec.iso_week,
                "ruleset_version": (spec.ruleset_version, rebuilt.spec.ruleset_version),
                "semantics_version": (spec.semantics_version, rebuilt.spec.semantics_version),
            },
            suggestions=["回到 planner 基于当前快照重解（resume_guard 的 FTS-3004 路径）"],
        )
    return rebuilt


def compile_spec_node(
    state: FTSState,
    session: Session,
    *,
    overrides: ScenarioOverrides = NO_OVERRIDES,
) -> Command[str]:
    """确定性节点 ①：`ruleset.yaml + semantics.yaml + SolveIntent → ConstraintSpec`。

    **不经 Harness、不读 Skill、不注册为任何 LLM 组件的工具**（铁律 4）。
    两项额外职责（S-01 类别先修展开、S-11 复训标记写入）由
    :func:`compile_spec` 承担，本节点只负责取参数、落黑板、决定下一跳。
    """
    snapshot_id = state_get(state, "snapshot_id", "")
    week_start_text = state_get(state, "week_start", "")
    if not snapshot_id:
        raise RequiredInputMissingError(
            "没有数据快照，无法编译规格。请先完成摄取并激活一个快照",
            details={"stage": "compile_spec"},
            suggestions=["运行 `python -m backend.ingestion.cli --baseline` 或上传数据文件"],
        )
    if not week_start_text:
        raise RequiredInputMissingError(
            "没有排班周起点。请指明要排哪一周（如 2026W02）",
            details={"stage": "compile_spec"},
            suggestions=["在请求里给出周次，或改用 POST /api/v1/schedule 传入 week_start"],
        )

    intent = model_get(state, "solve_intent", SolveIntent) or default_intent()
    tier = int(state_get(state, "relaxation_tier", 0))
    bundle = compile_spec(
        session,
        snapshot_id=snapshot_id,
        week_start=date.fromisoformat(week_start_text),
        intent=intent,
        relaxation_tier=tier,
        overrides=overrides,
        materialize=True,
    )
    spec = bundle.spec
    return Command(
        goto="solve",
        update={
            "constraint_spec": spec,
            "ruleset_version": spec.ruleset_version,
            "semantics_version": spec.semantics_version,
            "trace_events": emit(
                state,
                "compile_spec",
                "decision",
                {
                    "iso_week": spec.iso_week,
                    "relaxation_tier": spec.relaxation_tier,
                    "incremental_constraints": len(spec.incremental_constraints),
                    "semantics_version": spec.semantics_version,
                    "ruleset_version": spec.ruleset_version,
                },
            ),
        },
    )


__all__ = [
    "ProgressUpdate",
    "SpecBundle",
    "blocked_reason_text",
    "bundle_from_spec",
    "compile_spec",
    "compile_spec_node",
    "compute_progress_updates",
    "default_intent",
    "intent_from_spec",
    "materialize_progress",
    "recurrent_since_for",
    "week_dates",
]
