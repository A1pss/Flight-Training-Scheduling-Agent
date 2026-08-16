"""多轮计划修订的真链路实测（v6 §7.3.4）。

出口标准那一条：**五种典型表述各自翻译为正确的 `IncrementalConstraint` 并成功
重解**，另测一条 `PIN_RUNWAY × JL-9` 的负例（应不可行并按 FTS-3005 回滚），
以及「连做 3 轮修订后 undo 两次，方案回到 v2」。

## 会话策略与 `test_graph_live.py` 一致

全图共用一个**不提交**的会话：断言能看见写进去的行，库不被污染。

## 为什么每轮都真求解

v6 §7.3.4 第 1 条硬性设计：

> 增量约束是求解器输入，不是结果修改。翻译完仍走 `solve → validate` 全流程。

拿一版缓存的方案改几个字段冒充「重解」，正是这条设计要杜绝的事。所以本文件
慢（每轮几十秒），这是它该有的样子。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy.orm import Session

from backend.core.db import get_session_factory, session_scope
from backend.core.errors import ErrorCode
from backend.graph.graph import GraphDeps, build_graph
from backend.graph.state import initial_state
from backend.planner.revision import RevisionStack, for_solver, translate_revision
from backend.routing.entities import directory_from_session
from backend.schemas.intent import ConstraintSpec, IncrementalConstraint, ObjectiveWeights
from backend.schemas.plan import SchedulePlan
from backend.skills_loader import load_library
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot

pytestmark = pytest.mark.integration

BASELINE_WEEK = date(2026, 1, 5)
TODAY = date(2026, 1, 2)


@pytest.fixture(scope="module")
def snapshot() -> str:
    with session_scope() as session:
        return ensure_baseline_snapshot(session)


@contextmanager
def shared_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def graph_deps(session: Session, snapshot_id: str, tmp_path: Path) -> GraphDeps:
    @contextmanager
    def factory() -> Iterator[Session]:
        yield session

    return GraphDeps(
        session_factory=factory,
        directory=directory_from_session(session, snapshot_id),
        library=load_library(),
        today=TODAY,
        plans_root=tmp_path / "plans",
        prompt_versions={},
    )


def runway_spec(session: Session, snapshot_id: str) -> ConstraintSpec:
    """一份只为 `PIN_RUNWAY` 预检服务的最小规格：跑道 → 服务机型。

    **映射从 PG 读**，不写死 —— 跑道与机型都由上传数据决定（CLAUDE.md §11）。
    """
    from datetime import date as date_type

    from sqlalchemy import select

    from backend.models.entities import RunwayAircraftType

    runways: dict[str, list[str]] = {}
    for row in session.scalars(
        select(RunwayAircraftType).where(RunwayAircraftType.snapshot_id == snapshot_id)
    ):
        runways.setdefault(row.runway_id, []).append(row.aircraft_type)
    return ConstraintSpec(
        snapshot_id=snapshot_id,
        ruleset_version="1.3.0",
        semantics_version="1.0.0",
        iso_week="2026W02",
        week_start=date_type(2026, 1, 5),
        week_end=date_type(2026, 1, 11),
        scope_persons="ALL",
        scope_missions="ALL",
        relaxation_tier=0,
        objective_weights=ObjectiveWeights(progress=1.0, disruption=1.0, balance=1.0),
        runway_model="dual_runway",
        runways={rid: sorted(types) for rid, types in sorted(runways.items())},
    )


def schedule_state(snapshot_id: str) -> Any:
    return initial_state(
        trace_id="m5-revision",
        user_id="tester",
        user_role="director",
        snapshot_id=snapshot_id,
        week_start=BASELINE_WEEK.isoformat(),
        messages=[{"role": "user", "content": "给所有人排班"}],
    )


def resume(app: Any, config: Any, decision: str, comment: str = "") -> dict[str, Any]:
    return cast(
        dict[str, Any],
        app.invoke(
            Command(
                resume={
                    "decision": decision,
                    "user_id": "tester",
                    "role": "director",
                    "comment": comment,
                }
            ),
            config=config,
        ),
    )


def revise(app: Any, config: Any, utterance: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """走一轮修订，**两次门禁往返**（v6 §7.3.4 第 4 条）：

    1. `REVISE` + 原话 → planner 翻译 → 回门禁展示「我理解为：…」；
    2. `APPROVE` → solve → validate → explain → 回门禁展示新方案。

    返回 `(回显那一屏, 重解之后那一屏)`。**第一屏必须在求解之前** ——
    这正是「用户确认后才重解」那句话在测试里的样子。
    """
    echoed = resume(app, config, "REVISE", utterance)
    resolved = resume(app, config, "APPROVE")
    return echoed, resolved


def fingerprint(state: dict[str, Any]) -> str:
    plan = state.get("solution")
    return plan.content_sha256 if isinstance(plan, SchedulePlan) else ""


def sortie_rows(state: dict[str, Any]) -> set[tuple[str, str, str, str, str]]:
    """方案的可比形态：(架次, 星期, 起飞, 飞机, 跑道)。diff 用。"""
    plan = state.get("solution")
    if not isinstance(plan, SchedulePlan):
        return set()
    return {
        (s.sortie_id, s.weekday, s.takeoff.isoformat(), s.aircraft_id, s.runway_id)
        for s in plan.sorties
    }


# ─────────────────────────────────────────────────────────────────────
# ① 五种典型表述的翻译（不求解，逐条核对 kind 与线格式）
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("utterance", "kind"),
    [
        ("周三上午太挤了，挪两个到下午", "REDUCE_DENSITY"),
        ("何超那个换成 AC49", "PIN_RESOURCE"),
        ("刘斌周五别排了", "FORBID"),
        ("早点飞", "SHIFT_WINDOW"),
        ("这几个都走 2 号跑道", "PIN_RUNWAY"),
    ],
)
def test_the_five_canonical_utterances_translate_to_the_right_kind(
    snapshot: str, utterance: str, kind: str
) -> None:
    """v6 §7.3.4 映射表的五行，逐行对。**规则路径**（`harness=None`）也必须认得。"""
    with shared_session() as session:
        directory = directory_from_session(session, snapshot)
        translation = translate_revision(
            utterance, round_no=1, harness=None, plan=None, directory=directory
        )
    assert translation.constraint.kind == kind
    assert translation.constraint.origin_utterance == utterance
    assert translation.echo, "回显文案不能为空（第 4 条硬性设计）"


def test_translation_echo_states_what_it_understood(snapshot: str) -> None:
    """第 4 条硬性设计：UI 先展示「我理解为：……」，用户确认后才重解。"""
    with shared_session() as session:
        directory = directory_from_session(session, snapshot)
        translation = translate_revision(
            "刘斌周五别排了", round_no=1, harness=None, plan=None, directory=directory
        )
    assert "我理解为" in translation.echo
    assert "周五" in translation.echo


def test_human_params_and_wire_params_are_two_shapes(snapshot: str) -> None:
    """人话形状进审计与回显，线格式进求解器（M4-B §3.7）。绕过转换就是静默失效。"""
    from datetime import time

    with shared_session() as session:
        directory = directory_from_session(session, snapshot)
        translation = translate_revision(
            "刘斌周五别排了", round_no=1, harness=None, plan=None, directory=directory
        )
    human = translation.constraint
    wire = for_solver(human, window_start=time(6, 0), plan=None, horizon_minutes=720)
    assert human.params["day"] == "周五"
    assert wire.params["day_index"] == 4, "线格式按 0~6 的日索引说话"


# ─────────────────────────────────────────────────────────────────────
# ② 五轮修订各自重解（真求解，慢）
# ─────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def revision_rounds(tmp_path_factory: pytest.TempPathFactory, snapshot: str) -> dict[str, Any]:
    """首轮排班 + 五轮修订，每轮都走完整 `solve → validate`。

    **模块级 fixture**：六次真求解，不该被每个断言各跑一遍。
    """
    tmp_path = tmp_path_factory.mktemp("revision")
    utterances = [
        "周三上午太挤了，挪两个到下午",
        "何超那个换成 AC49",
        "刘斌周五别排了",
        "早点飞",
        "这几个都走 2 号跑道",
    ]
    with shared_session() as session:
        app = build_graph(graph_deps(session, snapshot, tmp_path), checkpointer=InMemorySaver())
        config = cast(Any, {"configurable": {"thread_id": "rev-1"}})

        v1 = app.invoke(schedule_state(snapshot), config=config)
        states = [v1]
        echoes = []
        for utterance in utterances:
            echoed, resolved = revise(app, config, utterance)
            echoes.append(echoed)
            states.append(resolved)
        return {"utterances": utterances, "states": states, "echoes": echoes}


def test_first_round_produces_the_baseline_plan(revision_rounds: dict[str, Any]) -> None:
    v1 = revision_rounds["states"][0]
    assert "__interrupt__" in v1
    assert v1["solver_stats"].status in ("OPTIMAL", "FEASIBLE")
    assert len(v1["solution"].sorties) == 14


@pytest.mark.parametrize("index", [1, 2, 3, 4, 5])
def test_each_revision_round_translates_and_resolves(
    revision_rounds: dict[str, Any], index: int
) -> None:
    """每一轮都要：翻译入栈 + 回显 + 重解出方案。"""
    state = revision_rounds["states"][index]
    utterance = revision_rounds["utterances"][index - 1]

    stack = [
        c if isinstance(c, IncrementalConstraint) else IncrementalConstraint.model_validate(c)
        for c in state["revision_stack"]
    ]
    assert len(stack) == index, "每轮入栈一条"
    assert stack[-1].origin_utterance == utterance, "原话原样保留，供撤销与审计"
    assert isinstance(state["solution"], SchedulePlan), "这一轮要有重解出来的方案"

    echo = revision_rounds["echoes"][index - 1]
    assert echo["pending_revision"] is True, "求解之前必须先停下来问一次"
    assert echo["revision_echo"], "回显文案必须有"
    assert "我理解为" in echo["revision_echo"]


def test_the_revision_stack_records_every_utterance_in_order(
    revision_rounds: dict[str, Any],
) -> None:
    final = revision_rounds["states"][-1]
    stack = RevisionStack.from_state(
        [
            c if isinstance(c, IncrementalConstraint) else IncrementalConstraint.model_validate(c)
            for c in final["revision_stack"]
        ]
    )
    assert stack.utterances() == revision_rounds["utterances"]
    assert stack.version_no() == 6, "首轮 v1 + 五轮修订 = v6"


def test_revisions_actually_change_the_plan(revision_rounds: dict[str, Any]) -> None:
    """**方案 diff 非空** —— 否则「重解」只是走了个过场。"""
    states = revision_rounds["states"]
    changed = [
        i for i in range(1, len(states)) if sortie_rows(states[i]) != sortie_rows(states[i - 1])
    ]
    assert changed, "五轮修订里至少要有一轮改变了方案"


# ─────────────────────────────────────────────────────────────────────
# ③ 负例：对 JL-9 架次下 PIN_RUNWAY(RWY-2)
# ─────────────────────────────────────────────────────────────────────
def test_pinning_a_jl9_sortie_to_runway_two_is_pre_checked_in_the_echo(
    snapshot: str,
) -> None:
    """预检把话说在前面：**RWY-2 只服务 JL-8**（v6 §1.3.5），AC84 是 JL-9。

    预检**不改变翻译结果** —— 约束照常产出、照常求解、照常按第 3 条回滚。
    """
    from tests.fixtures.graph_fixtures import plan as fake_plan
    from tests.fixtures.graph_fixtures import sortie

    with shared_session() as session:
        directory = directory_from_session(session, snapshot)
        spec = runway_spec(session, snapshot)
        jl9 = fake_plan([sortie("S000001", aircraft_id="AC84", runway_id="RWY-1")])
        translation = translate_revision(
            "AC84 那班也走 2 号跑道",
            round_no=1,
            harness=None,
            plan=jl9,
            directory=directory,
            spec=spec,
        )
    # 判据来自数据，不是写死的「RWY-2 只服务 JL-8」
    assert spec.runways["RWY-2"] == ["JL-8"]
    assert translation.constraint.kind == "PIN_RUNWAY"
    assert translation.constraint.params.get("runway") == "RWY-2"
    assert translation.warnings, "预检必须给出警告"
    assert any("AC84" in w and "JL-9" in w for w in translation.warnings)


def test_an_infeasible_revision_rolls_back_with_fts_3005(
    tmp_path_factory: pytest.TempPathFactory, snapshot: str
) -> None:
    """第 3 条硬性设计：不可行即回滚并解释，**不静默丢弃**。

    ## 构造法：把学员的架次钉到一架 JL-9 上

    本来想用「所有架次都走 2 号跑道」，实测**它是可行的** —— 基准周的 14 个
    架次全部落在 JL-8 上（见下一个用例），而 RWY-2 正好服务 JL-8。

    改用同一个 JL-9 根因、但在基准数据上真能触发的构造：何超只持 JL-8 机型
    资质，把他的架次钉到 AC84（JL-9）违反约束5/6，必然不可行。
    """
    tmp_path = tmp_path_factory.mktemp("infeasible")
    with shared_session() as session:
        app = build_graph(graph_deps(session, snapshot, tmp_path), checkpointer=InMemorySaver())
        config = cast(Any, {"configurable": {"thread_id": "rev-infeasible"}})

        v1 = app.invoke(schedule_state(snapshot), config=config)
        before = fingerprint(v1)
        before_rows = sortie_rows(v1)

        echoed = resume(app, config, "REVISE", "何超那个换成 AC84")
        assert echoed["pending_revision"] is True, "不可行也要先回显 —— 用户先确认再重解"
        after = resume(app, config, "APPROVE")

    codes = [e.code for e in after["errors"]]
    assert ErrorCode.REVISION_INFEASIBLE in codes, f"应报 FTS-3005，实际 {codes}"

    failure = next(e for e in after["errors"] if e.code is ErrorCode.REVISION_INFEASIBLE)
    assert "回滚" in failure.message
    assert "何超那个换成 AC84" in failure.message, "冲突说明里要有用户的原话"

    # 回滚正确率必须 100%：方案退回上一版，逐字节相同
    assert fingerprint(after) == before, "回滚后的方案指纹必须与上一版一致"
    assert sortie_rows(after) == before_rows
    assert after["revision_stack"] == [], "那条约束已从栈上弹掉"


def test_the_baseline_plan_has_no_jl9_sortie_so_the_runway_negative_case_is_pre_check_only(
    revision_rounds: dict[str, Any], snapshot: str
) -> None:
    """一条**数据层面的发现**，写进收工报告。

    `PIN_RUNWAY(RWY-2)` 对 JL-9 架次不可满足（v6 §1.3.5：RWY-2 只服务 JL-8）。
    但基准周排不出任何 JL-9 架次：14 个架次全都涉及学员，而**学员只持 JL-8
    机型资质**；刘斌虽持 JL-9，他的 C 类复训窗口 `[01-08, 01-14]` 跨出 W02，
    本周不强制，所以他一个架次都没有。

    因此这条负例只能在**预检层**验（上一个用例已验），图级的不可行回滚改用
    同一个 JL-9 根因的可达构造。业务方 2026-08-15 确认：预检层验到即可。
    """
    v1 = revision_rounds["states"][0]
    directory_types = {s.aircraft_id for s in v1["solution"].sorties}
    with shared_session() as session:
        from sqlalchemy import select

        from backend.models.entities import Aircraft

        jl9 = {
            row.aircraft_id
            for row in session.scalars(
                select(Aircraft).where(
                    Aircraft.snapshot_id == snapshot, Aircraft.aircraft_type == "JL-9"
                )
            )
        }
    assert jl9, "基准数据里确实有 JL-9 机（AC84/AC95）"
    assert not (directory_types & jl9), "但方案里一架都没用上 —— 所以图级负例造不出来"


# ─────────────────────────────────────────────────────────────────────
# ④ undo：连做 3 轮后撤两次，回到 v2
# ─────────────────────────────────────────────────────────────────────
def test_three_revisions_then_two_undos_returns_to_v2(
    tmp_path_factory: pytest.TempPathFactory, snapshot: str
) -> None:
    """出口标准那一条。**撤销后照常重解**，不是把旧方案取回来。"""
    tmp_path = tmp_path_factory.mktemp("undo")
    with shared_session() as session:
        app = build_graph(graph_deps(session, snapshot, tmp_path), checkpointer=InMemorySaver())
        config = cast(Any, {"configurable": {"thread_id": "rev-undo"}})

        app.invoke(schedule_state(snapshot), config=config)
        _, v2 = revise(app, config, "刘斌周五别排了")
        revise(app, config, "何超那个换成 AC49")
        _, v4 = revise(app, config, "早点飞")
        assert len(v4["revision_stack"]) == 3

        undo_echoed, undone = revise(app, config, "撤销两次")
        assert "撤销最近 2 条修订" in undo_echoed["revision_echo"], "撤销也要先回显"

    stack = [
        c if isinstance(c, IncrementalConstraint) else IncrementalConstraint.model_validate(c)
        for c in undone["revision_stack"]
    ]
    assert len(stack) == 1, "3 条撤 2 条，剩 1 条"
    assert stack[0].origin_utterance == "刘斌周五别排了", "剩下的是第一轮那条"
    assert RevisionStack.from_state(stack).version_no() == 2, "回到 v2"

    spec = undone["constraint_spec"]
    assert [c.round_no for c in spec.incremental_constraints] == [1], (
        "求解器侧的增量约束要同步弹掉 —— 只弹栈不改 spec 就是静默失效"
    )
    # ⚠️ **不断言「方案与当初那版 v2 逐字节相同」**，因为它不该相同：
    #
    # 撤销的语义是「去掉那条约束**再解一次**」（第 1 条硬性设计），而目标函数
    # 里的最小扰动项锚定的是**当前**方案（此刻是 v4），不是当初那版 v2。于是
    # 求解器给出的是「满足 v2 那套约束、且离 v4 最近」的解 —— 与当初从 v1 出发
    # 解出来的 v2 可以不同，两者都是 OPTIMAL。
    #
    # 这正是「撤销 ≠ 把旧方案取回来」的实际后果。要断言的是**约束集回到了 v2**，
    # 而不是像素级还原（M5 实测发现，写进收工报告）。
    assert isinstance(undone["solution"], SchedulePlan)
    assert len(undone["solution"].sorties) == len(v2["solution"].sorties)
    assert undone["validation"].all_passed, "撤销后重解出来的方案照样要过 14 条校验"
