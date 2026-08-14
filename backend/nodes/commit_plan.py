"""确定性节点 ⑥：`commit_plan_node`（v6 §7.2.4）。

> **事务内**：归档计划 + 推进训练进度 + 结算欠账 + 写 `last_done_date` 锚点。
> 数据库事务，必须原子。

## `last_done_date` 是这一节最要紧的一件事

它是 **R19 的唯一缓解措施**。S-12 规定「锚点缺失 → 视为窗口从本周周一起算，
且不计欠账」——那条裁定只在**首次排班**成立，因为原始 PDF 里根本没有这一列。
**第二周起 `gap` 必须是真值**，否则每一周都被当成「从这周开始计时」，
真实欠账被 S-12 永久掩盖，而 Sheet 4 会一路显示「无欠账」。

所以归档时必须把本周实际飞过的日期写回去，且**必须有测试证明写进去了**
（`tests/integration/test_commit_plan_live.py`）。

## 「推进进度」推的是什么，不推什么

| 字段 | 怎么变 | 为什么 |
|---|---|---|
| `completed_count` | `+= 本周该 (人,课目) 的架次数` | 事实 |
| `last_done_date` | `= 本周该组合最后一次飞行的日期` | 事实，且是跨周锚点 |
| `debt_count` | `+= 本周结算出的欠账` | 事实 |
| `status` | `NOT_STARTED → IN_PROGRESS`；**攒满一个完整周期 → `COMPLETED`** | 业务方 2026-08-14 裁定（`Z-16`） |

## 「完成」的判据（业务方 2026-08-14 裁定，`Z-16`）

> **一门课飞完完整周期才算完成。**

周期长度是 `cycle_weeks` 周、周期内的要求是「每 `freq_days` 天 ≥1 次」，
所以完整周期需要 `(cycle_weeks × 7) // freq_days` 次
（:func:`backend.core.ruleset.cycle_required_for`）。基准数据下 A 类 28 次、
B~F 类 16 次、G/H 类 10 次。

**攒满就同时做两件事，缺一不可**：

1. `training_progress.status = "COMPLETED"`；
2. **往 `person_completed_missions` 插一行**。

第 2 件是关键。`person_completed_missions` 是先修判定的**事实来源**
（v6 §6.1：进度表由它物化而来，不是反过来），而
`retrieval.prereq_cte.evaluate_prereq` 读的就是它。只翻 `status` 不写事实表，
会出现「这门课显示已完成，但它作为先修的那几门课还是解锁不了」——
**同一个事实在两处不一致，而且不一致的那一侧恰好是排班真正用的那一侧。**

⚠️ **反过来不成立**：`COMPLETED` 不会被降回 `IN_PROGRESS`。摄取期从
「已完成课目」列读进来的行 `completed_count=1`，远小于一个完整周期，
但它是**业务方直接给的事实**——拿我们的计次公式去推翻它是本末倒置。

## 归档产物由 M3 的报表层负责

`archive_plan()` 一次落齐五件套（xlsx / json / manifest / 校验报告 / 求解日志），
闸门 3 不过就抛 FTS-5001 且一个文件都不落。本节点只负责把 `ReportBundle`
组装对，不重复实现归档。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from langgraph.graph import END
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import get_settings
from backend.core.ruleset import cycle_required_for
from backend.graph.events import emit
from backend.graph.state import FTSState, model_get
from backend.models.entities import Mission as MissionRow
from backend.models.entities import PersonCompletedMission as PersonCompletedMissionRow
from backend.models.planning import BlockedItem as BlockedItemRow
from backend.models.planning import Plan as PlanRow
from backend.models.planning import Sortie as SortieRow
from backend.models.planning import SortieCrew as SortieCrewRow
from backend.models.planning import TrainingDebt as TrainingDebtRow
from backend.models.progress import TrainingProgress
from backend.report.archive import ArchiveResult, archive_plan, code_version_from_git
from backend.report.bundle import ApprovalInfo, ProvenanceInfo, RelaxationRecord, ReportBundle
from backend.schemas.common import HumanDecision
from backend.schemas.plan import SchedulePlan
from backend.schemas.solver import SolverStats
from backend.schemas.validation import ValidationReport
from backend.validator import load_context


# ─────────────────────────────────────────────────────────────────────
# 进度推进
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ProgressAdvance:
    """一行 `training_progress` 的推进结果，供测试逐字段断言。"""

    person_id: str
    mission_id: str
    cycle_start: date
    flown: int
    last_done_date: date | None
    debt_delta: int
    status_before: str
    status_after: str
    #: 攒满一个完整周期所需的次数（`Z-16`）。0 表示课目已不在本快照里
    cycle_required: int = 0
    completed_count: int = 0

    @property
    def newly_completed(self) -> bool:
        """本次归档把它推到「完成」了。"""
        return self.status_before != "COMPLETED" and self.status_after == "COMPLETED"


def flown_counts(plan: SchedulePlan) -> dict[tuple[str, str], list[date]]:
    """本周每个 (人, 课目) 实际飞过的日期。

    机组两个人都算 —— 教员带飞的那一趟对教员也是一次执行。约束11/12 的架次
    上限本来就是这么数的，进度表若只记学员，两处口径立刻打架。
    """
    out: dict[tuple[str, str], list[date]] = {}
    for sortie in plan.sorties:
        for member in sortie.crew:
            out.setdefault((member.person_id, sortie.mission_id), []).append(sortie.date)
    for dates in out.values():
        dates.sort()
    return out


def advance_progress(
    session: Session,
    plan: SchedulePlan,
    *,
    snapshot_id: str,
) -> list[ProgressAdvance]:
    """推进训练进度并**写 `last_done_date` 锚点**（R19 的缓解措施）。

    只更新库里已有的行：`training_progress` 是物化视图，行的存在与否由摄取期
    决定（主键含 `cycle_start`，而 `cycle_start` 只能来自课目文件或用户回答，
    S-14）。这里凭空插一行等于发明一个 `cycle_start`。
    """
    counts = flown_counts(plan)
    debts = {(d.person_id, d.mission_id): d.debt for d in plan.debts}
    keys = set(counts) | set(debts)
    if not keys:
        return []

    rows = {
        (row.person_id, row.mission_id): row
        for row in session.execute(
            select(TrainingProgress).where(TrainingProgress.snapshot_id == snapshot_id)
        ).scalars()
    }
    freq_days = _freq_days(session, snapshot_id)
    already_completed = _completed_facts(session, snapshot_id)

    advances: list[ProgressAdvance] = []
    for key in sorted(keys):
        row = rows.get(key)
        if row is None:
            continue
        dates = counts.get(key, [])
        debt_delta = debts.get(key, 0)
        status_before = row.status
        required = _cycle_required(row, freq_days)

        if dates:
            row.completed_count += len(dates)
            # ★ 锚点：本周该组合**最后一次**飞行的日期。取 max 而不是 min ——
            # 下一周的 gap 要从最近一次算起
            last = max(dates)
            if row.last_done_date is None or last > row.last_done_date:
                row.last_done_date = last
            # ★ `Z-16`：攒满一个完整周期才算完成
            if row.status != "COMPLETED" and required and row.completed_count >= required:
                row.status = "COMPLETED"
                if key not in already_completed:
                    session.add(
                        PersonCompletedMissionRow(
                            person_id=key[0], snapshot_id=snapshot_id, mission_id=key[1]
                        )
                    )
                    already_completed.add(key)
            elif row.status == "NOT_STARTED":
                row.status = "IN_PROGRESS"
        if debt_delta:
            row.debt_count += debt_delta

        advances.append(
            ProgressAdvance(
                person_id=key[0],
                mission_id=key[1],
                cycle_start=row.cycle_start,
                flown=len(dates),
                last_done_date=row.last_done_date,
                debt_delta=debt_delta,
                status_before=status_before,
                status_after=row.status,
                cycle_required=required,
                completed_count=row.completed_count,
            )
        )
    session.flush()
    return advances


def _cycle_required(row: TrainingProgress, freq_days: Mapping[str, int]) -> int:
    """这一行攒满一个完整周期要多少次。课目不在本快照里就返回 0（不推断）。"""
    freq = freq_days.get(row.mission_id)
    if freq is None:
        return 0
    return cycle_required_for(row.cycle_weeks, freq)


def _freq_days(session: Session, snapshot_id: str) -> dict[str, int]:
    """本快照各课目的 `freq_days`。

    从 `missions` 读而不是从 `ConstraintSpec` 拿：`commit_plan` 归档的是**已经
    校验过的方案**，它该问的是「库里现在这门课的周期是多少」，而不是
    「编译那一刻规格里记的是多少」。两者一致时无差别，不一致时以库为准
    ——课目文件换过一版而方案是旧规格编的，那正是 `resume_guard` 要拦的场景。
    """
    return {
        row.mission_id: row.freq_days
        for row in session.execute(
            select(MissionRow).where(MissionRow.snapshot_id == snapshot_id)
        ).scalars()
    }


def _completed_facts(session: Session, snapshot_id: str) -> set[tuple[str, str]]:
    """本快照已登记的「已完成课目」事实。"""
    return {
        (row.person_id, row.mission_id)
        for row in session.execute(
            select(PersonCompletedMissionRow).where(
                PersonCompletedMissionRow.snapshot_id == snapshot_id
            )
        ).scalars()
    }


# ─────────────────────────────────────────────────────────────────────
# 计划落库
# ─────────────────────────────────────────────────────────────────────
def register_versions(session: Session, *, ruleset_version: str, semantics_version: str) -> None:
    """把本次用到的规则集/语义版本登记进 `rulesets` / `semantics_versions`。

    ## 为什么这一步在这里

    `plans.ruleset_version` 与 `plans.semantics_version` 上有外键。**M4-B 是
    `plans` 表的第一个写入方**，于是第一次归档就撞上了
    `ForeignKeyViolation: Key (ruleset_version)=(1.3.0) is not present`——
    两张登记表从来没人往里写过（M1 建了表，M2/M3 都只在内存里读 YAML）。

    **登记不是「发明版本」**：权威始终是 `rules/*.yaml`（v6 §1.1），这两张表
    记的是「哪一版在什么时候被这套系统加载过」——`loaded_at` / `source_path` /
    `content_sha256` 三个字段说的就是这件事。所以这里的语义是**首次使用即登记**，
    已登记的原样跳过，**绝不覆盖**已有行：同一个版本号对应过两份不同内容，
    正是 `content_sha256` 要防的事。
    """
    from backend.core.ruleset import get_ruleset, get_semantics
    from backend.models.versioning import Ruleset as RulesetRow
    from backend.models.versioning import SemanticsVersion as SemanticsRow

    if session.get(RulesetRow, ruleset_version) is None:
        rules = get_ruleset()
        source = Path(get_settings().RULESET_PATH)
        session.add(
            RulesetRow(
                ruleset_version=ruleset_version,
                effective_from=date.today(),
                rule_count=len(rules.rules),
                source_path=str(source),
                content_sha256=_file_sha256(source),
            )
        )
    if session.get(SemanticsRow, semantics_version) is None:
        semantics = get_semantics()
        source = Path(get_settings().SEMANTICS_PATH)
        session.add(
            SemanticsRow(
                semantics_version=semantics_version,
                decided_on=date.today(),
                decided_by="business_owner",
                switches=dict(semantics.snapshot()),
                content_sha256=_file_sha256(source),
            )
        )
    session.flush()


def _file_sha256(path: Path) -> str:
    """规则文件的内容指纹。文件不在就抛——**不编一个哈希**。"""
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def next_plan_version(session: Session, iso_week: str) -> int:
    """同一周的下一个版本号。`(iso_week, plan_version)` 上有唯一约束。"""
    existing = session.execute(
        select(PlanRow.plan_version).where(PlanRow.iso_week == iso_week)
    ).scalars()
    return int(max([*existing, 0])) + 1


def persist_plan(
    session: Session,
    plan: SchedulePlan,
    *,
    stats: SolverStats,
    decision: HumanDecision | None,
    approved_at: datetime,
) -> PlanRow:
    """把方案写进 `plans` / `sorties` / `sortie_crew` / `training_debts` / `blocked_items`。"""
    row = PlanRow(
        plan_id=plan.plan_id,
        iso_week=plan.iso_week,
        plan_version=next_plan_version(session, plan.iso_week),
        week_start=plan.week_start,
        week_end=plan.week_end,
        snapshot_id=plan.snapshot_id,
        ruleset_version=plan.ruleset_version,
        semantics_version=plan.semantics_version,
        status=stats.status,
        relax_tier=plan.relaxation_tier,
        seed=stats.random_seed,
        solver_stats=stats.model_dump(mode="json"),
        content_sha256=plan.content_sha256,
        approved_by=decision.user_id if decision is not None else None,
        approved_at=approved_at if decision is not None else None,
    )
    session.add(row)
    session.flush()

    for sortie in plan.sorties:
        session.add(
            SortieRow(
                sortie_id=sortie.sortie_id,
                plan_id=plan.plan_id,
                flight_date=sortie.date,
                weekday=sortie.weekday,
                takeoff=sortie.takeoff,
                landing=sortie.landing,
                mission_id=sortie.mission_id,
                mission_name=sortie.mission_name,
                airspace_id=sortie.airspace_id,
                aircraft_id=sortie.aircraft_id,
                runway_id=sortie.runway_id,
                is_recurrent=sortie.is_recurrent,
            )
        )
        for member in sortie.crew:
            session.add(
                SortieCrewRow(
                    sortie_id=sortie.sortie_id,
                    person_id=member.person_id,
                    name=member.name,
                    role=member.role,
                )
            )
    for debt in plan.debts:
        session.add(
            TrainingDebtRow(
                plan_id=plan.plan_id,
                person_id=debt.person_id,
                mission_id=debt.mission_id,
                required_count=debt.required,
                scheduled_count=debt.scheduled,
                debt_count=debt.debt,
                relaxed_by=debt.relaxed_by,
                reason="频率窗口松弛产生的欠账，下周优先补",
            )
        )
    for blocked in plan.blocked_items:
        session.add(
            BlockedItemRow(
                plan_id=plan.plan_id,
                person_id=blocked.person_id,
                mission_id=blocked.mission_id,
                reason=blocked.reason,
                missing_prereqs=list(blocked.missing_prereqs),
            )
        )
    session.flush()
    return row


# ─────────────────────────────────────────────────────────────────────
# 归档
# ─────────────────────────────────────────────────────────────────────
def build_report_bundle(
    session: Session,
    plan: SchedulePlan,
    *,
    validation: ValidationReport,
    stats: SolverStats,
    decision: HumanDecision | None,
    generated_at: datetime,
    prompt_versions: Mapping[str, str] | None = None,
    skill_version: str | None = None,
    relaxations: Sequence[RelaxationRecord] = (),
    conflict_summary: str | None = None,
    solver_log: str = "",
) -> ReportBundle:
    """组装 M3 的 `ReportBundle`。

    `prompt_versions` 与 `skill_version` 是 M3 留给 M4 的两个空位（当时写的是
    `null`）。现在有实体了：前者来自 `PromptRegistry.load().versions()`，
    后者来自 `SkillLibrary.fingerprint()`。**由调用方传进来**——本模块在
    `backend/nodes/` 下，不许 import `skills_loader`（铁律 3）。
    """
    ctx = load_context(session, snapshot_id=plan.snapshot_id, week_start=plan.week_start)
    return ReportBundle(
        plan=plan,
        ctx=ctx,
        validation=validation,
        stats=stats,
        generated_at=generated_at,
        plan_type="WEEKLY",
        plan_status="APPROVED" if decision is not None else "DRAFT",
        relaxations=list(relaxations),
        conflict_summary=conflict_summary,
        approval=ApprovalInfo(
            approver=decision.user_id if decision is not None else None,
            approved_at=generated_at if decision is not None else None,
        ),
        provenance=_provenance(prompt_versions, skill_version),
        solver_log=solver_log,
    )


def _provenance(
    prompt_versions: Mapping[str, str] | None, skill_version: str | None
) -> ProvenanceInfo:
    """LLM 三态与 GPU 绑定取真实配置；提示词版本与知识层指纹由调用方给。"""
    base = ProvenanceInfo.from_settings(
        code_version=code_version_from_git(), solver_version=_ortools_version()
    )
    return replace(
        base,
        prompt_versions=dict(prompt_versions or {}),
        skill_version=skill_version,
    )


def _ortools_version() -> str | None:
    """CP-SAT 版本。取不到就返回 None——**不编一个版本号进 manifest**。"""
    try:
        from importlib.metadata import version

        return version("ortools")
    except Exception:  # 版本取不到不该让归档失败
        return None


# ─────────────────────────────────────────────────────────────────────
# 节点
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CommitResult:
    """一次提交的完整结果，供测试与 API 层取用。"""

    plan_id: str
    plan_version: int
    advances: tuple[ProgressAdvance, ...]
    archive: ArchiveResult | None


def commit_plan(
    session: Session,
    state: FTSState,
    *,
    plans_root: Path | None = None,
    prompt_versions: Mapping[str, str] | None = None,
    skill_version: str | None = None,
    now: datetime | None = None,
    archive: bool = True,
) -> CommitResult:
    """事务内的四件事。**顺序刻意如此**。

    先落库再归档：归档会跑闸门 3（Excel 回读反解），不过就抛 FTS-5001。抛出时
    整个事务回滚，库里不留半条记录——**宁可什么都没提交，也不要「库里有计划、
    磁盘上没文件」**这种事后要人手工对账的状态。
    """
    plan = model_get(state, "solution", SchedulePlan)
    validation = model_get(state, "validation", ValidationReport)
    stats = model_get(state, "solver_stats", SolverStats)
    if plan is None or validation is None or stats is None:
        raise ValueError("commit_plan 需要 solution / validation / solver_stats 三者齐备")
    if not validation.all_passed:
        raise ValueError("校验未全绿的方案不得归档 —— 14 条必须全过，这是 v6 §0.3 的交付承诺")

    decision = model_get(state, "human_decision", HumanDecision)
    stamp = now or datetime.now()

    register_versions(
        session,
        ruleset_version=plan.ruleset_version,
        semantics_version=plan.semantics_version,
    )
    row = persist_plan(session, plan, stats=stats, decision=decision, approved_at=stamp)
    advances = advance_progress(session, plan, snapshot_id=plan.snapshot_id)

    result: ArchiveResult | None = None
    if archive:
        bundle = build_report_bundle(
            session,
            plan,
            validation=validation,
            stats=stats,
            decision=decision,
            generated_at=stamp,
            prompt_versions=prompt_versions,
            skill_version=skill_version,
        )
        result = archive_plan(bundle, root=plans_root, now=stamp)

    return CommitResult(
        plan_id=row.plan_id,
        plan_version=row.plan_version,
        advances=tuple(advances),
        archive=result,
    )


def commit_plan_node(
    state: FTSState,
    session: Session,
    *,
    plans_root: Path | None = None,
    prompt_versions: Mapping[str, str] | None = None,
    skill_version: str | None = None,
    now: datetime | None = None,
    archive: bool = True,
) -> Command[str]:
    """确定性节点 ⑥。"""
    result = commit_plan(
        session,
        state,
        plans_root=plans_root,
        prompt_versions=prompt_versions,
        skill_version=skill_version,
        now=now,
        archive=archive,
    )
    payload: dict[str, Any] = {
        "plan_id": result.plan_id,
        "plan_version": result.plan_version,
        "progress_rows_advanced": len(result.advances),
        "anchors_written": sum(1 for a in result.advances if a.last_done_date is not None),
        "debt_rows": sum(1 for a in result.advances if a.debt_delta),
    }
    update: dict[str, Any] = {
        "committed_plan_id": result.plan_id,
        "trace_events": emit(state, "commit_plan", "decision", payload),
    }
    if result.archive is not None:
        update["workbook_path"] = str(result.archive.xlsx)
    return Command(goto=END, update=update)


__all__ = [
    "CommitResult",
    "ProgressAdvance",
    "advance_progress",
    "build_report_bundle",
    "commit_plan",
    "commit_plan_node",
    "flown_counts",
    "next_plan_version",
    "persist_plan",
    "register_versions",
]
