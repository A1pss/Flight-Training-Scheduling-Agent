"""集成测试：基准周 2026W02 在**真实快照**上求解（v6 §1.4 / §12.3）。

**直连裸装 PG（127.0.0.1:5433）**，读 `--baseline` 落下来的 ACTIVE 快照。
跑之前必须 `alembic upgrade head` 并跑过 `python -m backend.ingestion.cli --baseline`
（CLAUDE.md §6 的那条坑：集成测试不自带 schema，也不自带数据）。

## 本文件的用例分三类

1. **基准周实测**：状态 / 架次 / 阻塞项 / 跑道 / 合规性 / 可复现性
2. **S-11 专项**（v6 §12.3）：把刘斌 C 类到期日临时改到复训窗口落在周内的位置
3. **v6 §12.3 的 I1~I5 五族构造不可行场景**，全部断言 `INFEASIBLE` + 冲突集覆盖预期规则。
   其中 **I1 / I4 / I5 的构造已于 2026-08-11 换过**（v6 本版说明 `Z-2`）：旧构造
   （两名教员不可用 / 训练窗 06:00-09:00 / RWY-2 关 + 06:00-08:00）**实测都是可行的**，
   因为它们建立在被 D-1 推翻的「A 类需教员带飞」前提上。
   旧构造的用例**保留在本文件里**（`test_superseded_*`），作为「那条前提确实已被推翻」的
   回归证据 —— 哪天它们变成 INFEASIBLE 了，说明有人把 D-1 又改回去了。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, time, timedelta
from itertools import pairwise

import pytest
from sqlalchemy.orm import Session

from backend.core.db import session_scope
from backend.core.ruleset import IDENTITY_INSTRUCTOR, IDENTITY_MATURE, IDENTITY_STUDENT
from backend.ingestion.loader import active_snapshot_id
from backend.models.progress import TrainingProgress
from backend.nodes.compile_spec import compile_spec
from backend.solver.candidates import enumerate_candidates
from backend.solver.data import ScenarioOverrides
from backend.solver.diagnose import ProbeBudget, diagnose
from backend.solver.reschedule import Disruption, local_reschedule
from backend.solver.solve import SolveOutcome, solve
from tests.fixtures.solver_asserts import check_plan, format_violations

pytestmark = pytest.mark.integration

#: 基准周（SPEC_DECISIONS §C.3 / v6 §1.2.3）
BASELINE_WEEK = date(2026, 1, 5)

#: v6 §1.4.2 的 7 条阻塞项，逐条写死 —— 这是**测试期望值**，不是代码常量
EXPECTED_BLOCKED: tuple[tuple[str, str, str], ...] = (
    ("P06", "missionC-2", "missionC-1 未完成"),
    ("P07", "missionC-2", "missionC-1 未完成"),
    ("P08", "missionB-1", "missionA-2 未完成"),
    ("P08", "missionB-2", "missionA-2 未完成"),
    ("P08", "missionC-1", "missionA-2 未完成"),
    ("P08", "missionC-2", "missionC-1 未完成"),
    ("P08", "missionF-1", "missionA-2 未完成"),
)

#: v6 §1.4.3 的纸面推演：9 带飞 + 5 单飞
EXPECTED_SORTIES = 14


@pytest.fixture(scope="module")
def session() -> Iterator[Session]:
    with session_scope() as s:
        yield s
        s.rollback()


@pytest.fixture(scope="module")
def snapshot(session: Session) -> str:
    snap = active_snapshot_id(session)
    assert snap, "库里没有 ACTIVE 快照 —— 先跑 `python -m backend.ingestion.cli --baseline`"
    return snap


@pytest.fixture(scope="module")
def baseline(session: Session, snapshot: str) -> SolveOutcome:
    """基准周按**默认预算**（§3.11 的 30s）求解一次，全模块共用。

    ⚠️ **刻意不开 `capture_log`**：CP-SAT 的日志回调是从 C++ 调回 Python 的，
    在 coverage 插桩下每行日志都被计量，实测能让求解慢出 50%，把
    `test_baseline_week_is_optimal` 逼成 FEASIBLE。日志采集单独一个用例、
    单独给预算（见 `test_baseline_solver_log_can_be_captured`）。
    """
    bundle = compile_spec(session, snapshot_id=snapshot, week_start=BASELINE_WEEK)
    return solve(bundle)


# ─────────────────────────────────────────────────────────────────────
# 基准周
# ─────────────────────────────────────────────────────────────────────
def test_baseline_week_is_optimal(baseline: SolveOutcome) -> None:
    """v6 §1.4 的纸面推演预期 OPTIMAL —— 实测确认。

    若这条红了，按 CLAUDE.md §7 第 4 条立刻停下来报业务方，
    **绝不许通过放宽任何硬约束让它可解**。
    """
    assert baseline.status == "OPTIMAL", (
        f"基准周实测 {baseline.status}，与 v6 §1.4 预期不符 —— 停下来报业务方"
    )
    assert baseline.plan is not None
    assert baseline.stats.wall_time_ms <= baseline.bundle.spec.solver_time_limit_s * 1000 * 1.2


def test_baseline_entity_scale_matches_v6_section_1_3(baseline: SolveOutcome) -> None:
    """基准快照的实体规模（v6 §1.3）—— 作为**基准回归护栏**，不是系统上限。"""
    data = baseline.bundle.data
    assert len(data.persons) == 8
    assert len(data.aircraft) == 8
    assert len(data.missions) == 12
    assert len(data.airspaces) == 6
    assert len(data.runways) == 2
    identities = {pid: p.identity for pid, p in data.persons.items()}
    assert sum(1 for v in identities.values() if v == IDENTITY_INSTRUCTOR) == 3
    assert sum(1 for v in identities.values() if v == IDENTITY_MATURE) == 1
    assert sum(1 for v in identities.values() if v == IDENTITY_STUDENT) == 4
    # AC73 是 JL-8，不是 JL-9（v6 §1.3.2 的告警）
    assert data.aircraft["AC73"].aircraft_type == data.aircraft["AC10"].aircraft_type
    assert data.aircraft["AC84"].aircraft_type == data.aircraft["AC95"].aircraft_type
    assert data.aircraft["AC73"].aircraft_type != data.aircraft["AC84"].aircraft_type


def test_baseline_blocked_items_match_v6_1_4_2_exactly(baseline: SolveOutcome) -> None:
    """阻塞项必须**恰好** 7 条，且逐条与 v6 §1.4.2 一致。

    多一条通常意味着预筛顺序被改坏了（类别资质检查跑到先修检查后面）；
    少一条意味着 S-01 被读成了「该类任一课目完成」。
    """
    actual = tuple(
        (b.person_id, b.mission_id, b.reason)
        for b in sorted(baseline.blocked_items, key=lambda b: (b.person_id, b.mission_id))
    )
    assert actual == EXPECTED_BLOCKED


def test_baseline_blocked_combos_never_scheduled(baseline: SolveOutcome) -> None:
    assert baseline.plan is not None
    blocked = {(b.person_id, b.mission_id) for b in baseline.blocked_items}
    for sortie in baseline.plan.sorties:
        for member in sortie.crew:
            assert (member.person_id, sortie.mission_id) not in blocked


def test_baseline_sortie_count_matches_paper_estimate(baseline: SolveOutcome) -> None:
    """v6 §1.4.3 推演约 14 架次（9 带飞 + 5 单飞）。"""
    assert baseline.plan is not None
    sorties = baseline.plan.sorties
    assert len(sorties) == EXPECTED_SORTIES
    dual = [s for s in sorties if len(s.crew) == 2]
    solo = [s for s in sorties if len(s.crew) == 1]
    assert len(dual) == 9
    assert len(solo) == 5


def test_baseline_plan_is_fully_compliant(baseline: SolveOutcome) -> None:
    """14 条规则逐条查（用 tests/ 下的临时断言器，不是 `backend/validator/`）。"""
    assert baseline.plan is not None
    violations = check_plan(baseline.plan, baseline.bundle.data, baseline.bundle.ruleset)
    assert not violations, format_violations(violations)


def test_baseline_student_a_class_sorties_are_solo(baseline: SolveOutcome) -> None:
    """D-1：A-1/A-2 带飞列 = 否 → 学员 A 类**单飞**，机组 1 人，不占教员。"""
    assert baseline.plan is not None
    data = baseline.bundle.data
    a_class = [s for s in baseline.plan.sorties if data.missions[s.mission_id].mission_class == "A"]
    assert a_class
    for sortie in a_class:
        assert len(sortie.crew) == 1
        assert sortie.crew[0].role == "单飞"


def test_baseline_every_student_flies_a_class_once(baseline: SolveOutcome) -> None:
    """约束3 + S-02 + S-13：**全部 4 名学员**各 ≥1 次 A 类（整体计数）。"""
    assert baseline.plan is not None
    data = baseline.bundle.data
    students = [pid for pid, p in data.persons.items() if p.identity == IDENTITY_STUDENT]
    for pid in students:
        count = sum(
            1
            for s in baseline.plan.sorties
            if data.missions[s.mission_id].mission_class == "A"
            and any(m.person_id == pid for m in s.crew)
        )
        assert count >= 1, f"{pid} 本周没有 A 类架次（违反 S-13）"


def test_baseline_disturbances_are_respected(baseline: SolveOutcome) -> None:
    """基准周的三项已知扰动（v6 §1.2.3）。"""
    assert baseline.plan is not None
    # 吴鹏 P03 01-05 不可用
    for sortie in baseline.plan.sorties:
        if sortie.date == date(2026, 1, 5):
            assert all(m.person_id != "P03" for m in sortie.crew)
    # AC73 01-09 全天定检
    assert not [
        s for s in baseline.plan.sorties if s.aircraft_id == "AC73" and s.date == date(2026, 1, 9)
    ]


def test_baseline_has_no_debts_at_tier0(baseline: SolveOutcome) -> None:
    assert baseline.plan is not None
    assert baseline.plan.debts == []
    assert baseline.plan.relaxation_tier == 0


def test_baseline_solver_log_can_be_captured(session: Session, snapshot: str) -> None:
    """求解日志可采集（出口标准要贴 solver log）。

    单独给 90s 预算：日志回调本身有成本（见 `baseline` fixture 的说明），
    这条验的是「能采到日志」，不是「30s 内证到最优」——后者由
    `test_baseline_week_is_optimal` 用默认预算验。
    """
    bundle = compile_spec(
        session,
        snapshot_id=snapshot,
        week_start=BASELINE_WEEK,
        time_limit_s=90.0,
        materialize=False,
    )
    outcome = solve(bundle, capture_log=True)
    assert outcome.run.log_lines
    assert any("CP-SAT" in line for line in outcome.run.log_lines)
    assert any("#Done" in line or "#Bound" in line for line in outcome.run.log_lines)


# ─────────────────────────────────────────────────────────────────────
# 跑道分配正确性（出口标准专项）
# ─────────────────────────────────────────────────────────────────────
def test_runway_assignment_respects_type_mapping(baseline: SolveOutcome) -> None:
    """只服务于 RWY-1 的机型（基准数据里是 JL-9 的 AC84/AC95）必须全在 RWY-1。"""
    assert baseline.plan is not None
    data = baseline.bundle.data
    single_runway_types = {
        t
        for t in {ac.aircraft_type for ac in data.aircraft.values()}
        if len(data.allowed_runways(t)) == 1
    }
    for sortie in baseline.plan.sorties:
        ac_type = data.aircraft[sortie.aircraft_id].aircraft_type
        allowed = data.allowed_runways(ac_type)
        assert sortie.runway_id in allowed
        if ac_type in single_runway_types:
            assert sortie.runway_id == allowed[0]


def test_runway_20min_window_cap_per_runway(baseline: SolveOutcome) -> None:
    """同一跑道任意 20 分钟半开窗内起飞 ≤2 次（S-04 + S-05 per_runway）。"""
    assert baseline.plan is not None
    rules = baseline.bundle.ruleset
    buckets: dict[tuple[date, str], list[int]] = {}
    for s in baseline.plan.sorties:
        buckets.setdefault((s.date, s.runway_id), []).append(s.takeoff.hour * 60 + s.takeoff.minute)
    for (day, runway), times in buckets.items():
        for anchor in times:
            inside = [t for t in times if anchor <= t < anchor + rules.density_window_minutes]
            assert len(inside) <= rules.density_window_cap, f"{day} {runway} 窗口超限"


def test_takeoff_separation_is_airport_wide(baseline: SolveOutcome) -> None:
    """**全场**任意两次起飞间隔 ≥7 分钟 —— 跨跑道也算（D-2）。"""
    assert baseline.plan is not None
    rules = baseline.bundle.ruleset
    by_day: dict[date, list[int]] = {}
    for s in baseline.plan.sorties:
        by_day.setdefault(s.date, []).append(s.takeoff.hour * 60 + s.takeoff.minute)
    for day, times in by_day.items():
        ordered = sorted(times)
        for a, b in pairwise(ordered):
            assert b - a >= rules.separation_minutes, f"{day} 全场起飞间隔 {b - a} < 7"


# ─────────────────────────────────────────────────────────────────────
# 可复现性（铁律 9）
# ─────────────────────────────────────────────────────────────────────
def test_baseline_is_byte_reproducible(session: Session, snapshot: str) -> None:
    """同 snapshot + 同 seed=42 + 同 worker 数，连跑 3 次逐字节一致（铁律 9）。

    **刻意给 90s 而不是默认的 30s。** 可复现性是「求解**跑完了**」这个前提下的性质：
    靠证明结束的求解（`OPTIMAL`）逐字节可复现；被预算截断的求解（`FEASIBLE`）
    切在哪一步取决于这台机器当时有多忙，本来就不该保证一致 —— 而 `FEASIBLE`
    这个状态本身就在说「这不是最优解」。

    所以这条用例把「预算够不够」和「可复现性」两件事拆开：预算够不够由
    `test_baseline_week_is_optimal` 用默认 30s 预算验；这里给足预算，专验
    「跑完之后是不是同一个方案」。带 coverage 插桩时求解会慢出 50%（实测），
    两件事混在一条用例里会得到一个和被测性质无关的偶发红。

    ⚠️ **预算 90s → 240s（M2-D 调整）。** 90s 在开发机上绰绰有余（优化阶段实测
    10~12s），但 GitHub Actions 的 2 核 runner 叠加 coverage 插桩之后不够：
    2026-08-12 的 CI 上三次连跑出现 `['OPTIMAL', 'FEASIBLE', 'OPTIMAL']`，
    重跑即绿 —— 典型的贴着预算的抛硬币。

    **这不是放宽断言**：三次仍然必须全部 `OPTIMAL` 且 `content_sha256` 逐字节相同。
    改的只是「给求解器多少时间把最优性证完」，而「求解跑完」正是本用例明写的前提。
    证完就立刻返回，所以在快的机器上这个上限一秒都不会多花。
    """
    digests: set[str] = set()
    statuses: list[str] = []
    for _ in range(3):
        bundle = compile_spec(
            session,
            snapshot_id=snapshot,
            week_start=BASELINE_WEEK,
            time_limit_s=240.0,
            materialize=False,
        )
        outcome = solve(bundle)
        assert outcome.plan is not None
        statuses.append(outcome.status)
        digests.add(outcome.plan.content_sha256)
    assert statuses == ["OPTIMAL"] * 3, f"三次求解状态不一致：{statuses}"
    assert len(digests) == 1, f"三次求解得到 {len(digests)} 个不同方案：{digests}"


# ─────────────────────────────────────────────────────────────────────
# compile_spec 的两项额外职责
# ─────────────────────────────────────────────────────────────────────
def test_compile_spec_writes_s11_anchor(session: Session, snapshot: str) -> None:
    """S-11：成熟飞行员到期资质 → `is_recurrent=TRUE, recurrent_since=到期次日`。

    基准周下刘斌 C 类 2026-01-07 到期 → 锚点 2026-01-08（**本周不强制安排**，
    窗口 [01-08, 01-14] 跨出 W02，但锚点必须落库，让 W03 能接续）。
    """
    compile_spec(session, snapshot_id=snapshot, week_start=BASELINE_WEEK)
    rows = {
        (r.person_id, r.mission_id): r
        for r in session.query(TrainingProgress)
        .filter(TrainingProgress.snapshot_id == snapshot)
        .all()
    }
    recurrent = {k: v for k, v in rows.items() if v.is_recurrent}
    assert set(recurrent) == {("P04", "missionC-1"), ("P04", "missionC-2")}
    for row in recurrent.values():
        assert row.recurrent_since == date(2026, 1, 8)
    # 其余行一律不复训，且 recurrent_since 必须为空（CHECK 约束的语义）
    for key, row in rows.items():
        if key not in recurrent:
            assert row.is_recurrent is False and row.recurrent_since is None


def test_compile_spec_materialises_blocked_reasons(session: Session, snapshot: str) -> None:
    compile_spec(session, snapshot_id=snapshot, week_start=BASELINE_WEEK)
    rows = (
        session.query(TrainingProgress)
        .filter(TrainingProgress.snapshot_id == snapshot, TrainingProgress.prereq_met.is_(False))
        .all()
    )
    actual = tuple(sorted((r.person_id, r.mission_id, r.blocked_reason or "") for r in rows))
    assert actual == EXPECTED_BLOCKED


def test_compile_spec_rejects_non_monday(session: Session, snapshot: str) -> None:
    from backend.core.errors import RequiredInputMissingError

    with pytest.raises(RequiredInputMissingError, match="周一"):
        compile_spec(session, snapshot_id=snapshot, week_start=date(2026, 1, 6))


def test_compile_spec_last_done_date_is_null_and_not_a_debt(
    session: Session, snapshot: str
) -> None:
    """S-12 现场：`last_done_date` 全为 NULL，且**不计欠账**（不是 gap=999）。"""
    bundle = compile_spec(session, snapshot_id=snapshot, week_start=BASELINE_WEEK)
    assert all(p.last_done_date is None for p in bundle.data.progress.values())
    cset = enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )
    assert all(not b.is_debt for b in cset.debt_basis)
    # A 类（freq 3）的截止日应当是 2 而不是 0
    a_deadlines = [
        r.days[-1]
        for r in cset.requirements
        if r.kind == "FREQ_DEADLINE" and r.mission_id == "missionA-2"
    ]
    assert a_deadlines == [2]


# ─────────────────────────────────────────────────────────────────────
# S-11 专项（v6 §12.3）
# ─────────────────────────────────────────────────────────────────────
def test_s11_recurrent_sortie_appears_when_window_lands_in_week(
    session: Session, snapshot: str
) -> None:
    """把刘斌 C 类到期日临时改到 2026-01-04 → 复训窗口 [01-05, 01-11] 完全落在基准周内。

    断言（v6 §12.3 S-11 专项）：
    ① 方案中出现 ≥1 次刘斌的 C-1 或 C-2 架次；
    ② 该架次 `is_recurrent=True`；
    ③ 机组人数为 1（复训架次单飞）。
    """
    bundle = compile_spec(
        session,
        snapshot_id=snapshot,
        week_start=BASELINE_WEEK,
        overrides=ScenarioOverrides(qual_expiry={("P04", "C"): date(2026, 1, 4)}),
        time_limit_s=40.0,
        materialize=False,
    )
    outcome = solve(bundle)
    assert outcome.status in ("OPTIMAL", "FEASIBLE")
    assert outcome.plan is not None
    recurrent = [
        s
        for s in outcome.plan.sorties
        if s.is_recurrent and any(m.person_id == "P04" for m in s.crew)
    ]
    assert recurrent, "S-11 复训架次没有出现"
    assert {s.mission_id for s in recurrent} <= {"missionC-1", "missionC-2"}
    for sortie in recurrent:
        assert len(sortie.crew) == 1
        assert sortie.crew[0].role == "复训"
    violations = check_plan(outcome.plan, bundle.data, bundle.ruleset)
    assert not violations, format_violations(violations)


# ─────────────────────────────────────────────────────────────────────
# v6 §12.3 的 I1~I5
# ─────────────────────────────────────────────────────────────────────
def _student_fleet(bundle) -> list[str]:  # type: ignore[no-untyped-def]
    """学员机型对应的全部在册飞机（数据驱动，不写死机号）。"""
    types = set().union(
        *[p.aircraft_types for p in bundle.data.persons.values() if p.identity == IDENTITY_STUDENT]
    )
    return sorted(a for a, ac in bundle.data.aircraft.items() if ac.aircraft_type in types)


def _solve_scenario(
    session: Session, snapshot: str, overrides: ScenarioOverrides, *, limit: float = 60.0
) -> SolveOutcome:
    bundle = compile_spec(
        session,
        snapshot_id=snapshot,
        week_start=BASELINE_WEEK,
        overrides=overrides,
        time_limit_s=limit,
        materialize=False,
    )
    return solve(bundle)


def test_i2_whole_student_fleet_maintained_is_infeasible(session: Session, snapshot: str) -> None:
    """I2：学员机型的全部飞机整周维护 → INFEASIBLE，冲突集含约束6/7 与约束3/13。"""
    bundle = compile_spec(
        session, snapshot_id=snapshot, week_start=BASELINE_WEEK, materialize=False
    )
    fleet = _student_fleet(bundle)
    assert len(fleet) == 6, "基准数据里学员机型应有 6 架（v6 §12.3 的 I2 更正）"
    overrides = ScenarioOverrides(
        maintenance_all_day=tuple(
            (a, BASELINE_WEEK, BASELINE_WEEK + timedelta(days=6)) for a in fleet
        )
    )
    outcome = _solve_scenario(session, snapshot, overrides)
    assert outcome.status == "INFEASIBLE"
    result = diagnose(
        outcome.bundle, time_limit_s=60.0, budget=ProbeBudget(15.0, 5, 60.0), session=session
    )
    assert result.status == "INFEASIBLE"
    rule_ids = {rid for item in result.conflicts for rid in item.rule_ids}
    assert {"C03", "C13"} & rule_ids, f"冲突集缺少约束3/13：{rule_ids}"
    assert {"C06", "C07"} & rule_ids, f"冲突集缺少机队根因：{rule_ids}"
    # I2 允许「升级人工」作为合格输出
    assert result.escalate or result.useful_proposals


def test_i3_ifr_closed_all_week_is_infeasible(session: Session, snapshot: str) -> None:
    """I3：IFR Route 整周容量降为 0 → INFEASIBLE，且提案应给出 C 类顺延。"""
    outcome = _solve_scenario(session, snapshot, ScenarioOverrides(airspace_capacity={"IFR": 0}))
    assert outcome.status == "INFEASIBLE"
    result = diagnose(
        outcome.bundle, time_limit_s=60.0, budget=ProbeBudget(15.0, 5, 60.0), session=session
    )
    rule_ids = {rid for item in result.conflicts for rid in item.rule_ids}
    assert "C06" in rule_ids and "C13" in rule_ids
    assert result.useful_proposals, "I3 应当有经探针验证、且真的排出架次的松弛提案"
    best = result.useful_proposals[0]
    assert best.verified_result is not None and best.verified_result.debts


@pytest.mark.parametrize(
    ("name", "overrides_factory"),
    [
        # 旧 I1：孙军、高超整周不可用（只剩吴鹏，且其周一不可用）→ 单教员容量 12 ≥ 9
        ("旧I1", lambda _bundle: ScenarioOverrides(unavailable_all_week=frozenset({"P01", "P02"}))),
        # 旧 I4：训练窗压缩至 06:00-09:00 → 180 分钟装得下 2 架次/天
        ("旧I4", lambda _b: ScenarioOverrides(window_end=time(9, 0))),
        # 旧 I5：RWY-2 整周关闭 + 训练窗压缩至 06:00-08:00 → 单跑道仍可 12 次起飞/天
        (
            "旧I5",
            lambda _bundle: ScenarioOverrides(
                closed_runways=frozenset({"RWY-2"}), window_end=time(8, 0)
            ),
        ),
    ],
)
def test_superseded_i1_i4_i5_constructions_are_feasible(
    session: Session, snapshot: str, name: str, overrides_factory: object
) -> None:
    """§12.3 的**旧** I1/I4/I5 构造实测可行 —— 保留作为 D-1 已推翻旧前提的回归证据。

    算术很直接（详见收工报告）：本周真实需求只有 14 架次、摊到 7 天是 2 架次/天，
    而这三个构造留下的容量分别是

    - I1：单教员周上限 12 架次 ≥ 9 个带飞架次；
    - I4：180 分钟训练窗，最长课目 69 分钟，装得下；
    - I5：单跑道 20 分钟 ≤2 次起飞 → 120 分钟窗口仍可 12 次/天 ≫ 2 次/天。

    v6 自己给的推导（「两跑道合计 4×9=36 起飞」「单教员容量 12」）也支持这个结论 ——
    那三条预期建立在被 D-1 推翻的「A 类需教员带飞」前提上（16~24 个带飞架次）。

    业务方 2026-08-11 据此把 §12.3 的三条构造换成了现在的版本（见
    `test_i1_i4_i5_are_infeasible`）。**本用例不是"待修复的失败"** ——
    它断言的是「按旧构造确实排得出班」，一旦它变红，说明有人把 D-1 改回去了
    （A 类又要教员带飞），那才是真问题。
    """
    bundle = compile_spec(
        session, snapshot_id=snapshot, week_start=BASELINE_WEEK, materialize=False
    )
    outcome = _solve_scenario(session, snapshot, overrides_factory(bundle), limit=60.0)  # type: ignore[operator]
    assert outcome.status != "INFEASIBLE", (
        f"旧构造 {name} 变成不可行了 —— 检查 D-1（学员 A 类单飞）是否被改回去了"
    )
    assert outcome.plan is not None, f"{name} 没有求出方案"
    violations = check_plan(outcome.plan, outcome.bundle.data, outcome.bundle.ruleset)
    assert not violations, format_violations(violations)


@pytest.mark.parametrize(
    ("name", "overrides_factory", "expect_rules"),
    [
        # I1：三名教员全部整周不可用 → 9 个带飞架次一个也排不了
        (
            "I1",
            lambda _b: ScenarioOverrides(unavailable_all_week=frozenset({"P01", "P02", "P03"})),
            {"C03", "C13"},
        ),
        # I4：训练窗压到 06:00-06:30 → 时长 > 30 分钟的课目全部装不进去
        ("I4", lambda _b: ScenarioOverrides(window_end=time(6, 30)), {"C01", "C13"}),
        # I5：学员机型可用的跑道全部关闭 → 跑道模型必须进冲突集
        (
            "I5",
            lambda b: ScenarioOverrides(
                closed_runways=frozenset(
                    b.data.allowed_runways(b.data.aircraft["AC10"].aircraft_type)
                )
            ),
            {"C09"},
        ),
    ],
)
def test_i1_i4_i5_are_infeasible(
    session: Session,
    snapshot: str,
    name: str,
    overrides_factory: object,
    expect_rules: set[str],
) -> None:
    """v6 §12.3 的 I1 / I4 / I5（**2026-08-11 换过构造的现行版本**）。

    每条各自验的那条约束必须真的出现在最小冲突集里 —— I4 验约束1、I5 验约束9
    （后者正是 I5 当初的设计目的：确认跑道不是被建成了软约束）。
    判定必须是 `INFEASIBLE` 而不是 `UNKNOWN`（铁律 8），所以给足 300s 时限。
    """
    bundle = compile_spec(
        session, snapshot_id=snapshot, week_start=BASELINE_WEEK, materialize=False
    )
    outcome = _solve_scenario(
        session,
        snapshot,
        overrides_factory(bundle),
        limit=300.0,  # type: ignore[operator]
    )
    assert outcome.status == "INFEASIBLE", f"{name} 期望 INFEASIBLE，实测 {outcome.status}"
    result = diagnose(
        outcome.bundle, time_limit_s=300.0, budget=ProbeBudget(15.0, 5, 60.0), session=session
    )
    assert result.status == "INFEASIBLE"
    rule_ids = {rid for item in result.conflicts for rid in item.rule_ids}
    assert expect_rules <= rule_ids, f"{name} 冲突集 {rule_ids} 未覆盖期望 {expect_rules}"
    # 每个场景至少产出 1 个经 probe_solve 验证过的提案，或明确升级人工
    assert result.useful_proposals or result.escalate


# ─────────────────────────────────────────────────────────────────────
# 局部重排（基准周实数据）
# ─────────────────────────────────────────────────────────────────────
def test_local_reschedule_on_baseline_freezes_untouched_sorties(
    session: Session, snapshot: str, baseline: SolveOutcome
) -> None:
    """在基准周方案上叠加「某架飞机某天维修」，平衡档重排。"""
    assert baseline.plan is not None
    victim = baseline.plan.sorties[0]
    day = baseline.bundle.data.day_index(victim.date)
    disruption = Disruption(
        aircraft=frozenset({victim.aircraft_id}),
        days=frozenset({day}),
        reason=f"{victim.aircraft_id} day{day} 临时维修",
    )
    disturbed = compile_spec(
        session,
        snapshot_id=snapshot,
        week_start=BASELINE_WEEK,
        overrides=disruption.to_overrides(baseline.bundle.data),
        time_limit_s=60.0,
        materialize=False,
    )
    outcome, decision = local_reschedule(disturbed, baseline.plan, disruption, policy="BALANCED")
    assert decision.affected_ids
    if outcome.plan is None:
        pytest.fail(f"平衡档重排应当可行，实测 {outcome.status}")
    violations = check_plan(outcome.plan, disturbed.data, disturbed.ruleset)
    assert not violations, format_violations(violations)
    assert not [
        s
        for s in outcome.plan.sorties
        if s.aircraft_id == victim.aircraft_id and s.date == victim.date
    ]


def test_jl9_class_sortie_is_pinned_to_its_only_runway(session: Session, snapshot: str) -> None:
    """**JL-9 架次固定 RWY-1** 的非空验证（S-05 / v6 §1.3.5）。

    基准周的最优方案里一个 JL-9 架次都没有（学员无 JL-9 机型资质，刘斌本周又不受
    任何频率约束），所以「JL-9 全在 RWY-1」这条在基准方案上是**空断言**。
    这里用两步把它变成实的：

    1. 把刘斌 C 类到期日提前到 2026-01-04，S-11 复训窗口整周落在周内 → 必排一次 C 类；
    2. 用 `PIN_RESOURCE` 把刘斌钉到一架只有 RWY-1 能服务的飞机上。

    然后断言那个架次确实落在 RWY-1，且该机型的可用跑道集合就只有 RWY-1。
    """
    from backend.nodes.compile_spec import default_intent
    from backend.schemas.intent import IncrementalConstraint

    probe = compile_spec(session, snapshot_id=snapshot, week_start=BASELINE_WEEK, materialize=False)
    single = [
        aid
        for aid, ac in sorted(probe.data.aircraft.items())
        if probe.data.allowed_runways(ac.aircraft_type) == ("RWY-1",)
    ]
    assert single, "基准数据里应当存在只能用 RWY-1 的机型（JL-9）"

    intent = default_intent()
    intent = intent.model_copy(
        update={
            "incremental_constraints": [
                IncrementalConstraint(
                    kind="PIN_RESOURCE",
                    targets=["P04"],
                    params={"aircraft_id": single[0]},
                    origin_utterance=f"刘斌这次用 {single[0]}",
                    round_no=1,
                )
            ]
        }
    )
    bundle = compile_spec(
        session,
        snapshot_id=snapshot,
        week_start=BASELINE_WEEK,
        intent=intent,
        overrides=ScenarioOverrides(qual_expiry={("P04", "C"): date(2026, 1, 4)}),
        time_limit_s=60.0,
        materialize=False,
    )
    outcome = solve(bundle)
    assert outcome.plan is not None
    mine = [s for s in outcome.plan.sorties if any(m.person_id == "P04" for m in s.crew)]
    assert mine, "刘斌的 S-11 复训架次没有出现"
    for sortie in mine:
        assert sortie.aircraft_id == single[0]
        assert sortie.runway_id == "RWY-1"
        assert bundle.data.allowed_runways(
            bundle.data.aircraft[sortie.aircraft_id].aircraft_type
        ) == ("RWY-1",)
    violations = check_plan(outcome.plan, bundle.data, bundle.ruleset)
    assert not violations, format_violations(violations)
