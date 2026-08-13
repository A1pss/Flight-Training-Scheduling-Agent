"""图组装（v6 §7.5）。

```
排班路径（主体为固定边，非自主路由）：
  route → planner → compile_spec → solve → validate → explain
        → [修订循环(无界): planner.translate_revision → solve → validate → explain]
        → resume_guard → human_gate → commit_plan
动态跳转只发生在三处：route 的意图分流、planner 的追问回退、validate 的驳回回环
```

## 三处动态跳转，一处都不多

| # | 位置 | 触发 | 说明 |
|---|---|---|---|
| 1 | `route` 的意图分流 | 六类意图 | 排班/重排走 planner，其余三类图外承接，unknown 去人工门禁 |
| 2 | `planner` 的追问回退 | `open_questions` 非空 | 回 `route` 组织追问，**不重新分类** |
| 3 | `validate` 的驳回回环 | 14 条没全过 | **触发即 CRITICAL（FTS-3003）**，这是自检不是常规路径 |

其余全是固定边。**这正是「主体是确定性工作流」这句话在代码里的样子**——
模型没有机会跳过 `validate`，也没有机会绕开 `human_gate` 直接归档。

## 依赖靠 `GraphDeps` 注入，不靠模块级单例

六个确定性节点的签名是 `(state, session, **opts)`，四个 LLM 组件要 `Harness`
与知识层。图在这里把它们闭包进去。这样做的两个好处：

- 节点本身能脱离 LangGraph 单测（`tests/unit/test_nodes_*.py` 就是直接调它们）；
- `backend.nodes` 不必 import 本模块，于是 `.importlinter` 禁令二
  （`backend.nodes ↛ backend.skills_loader`）成立——本模块是读 skill 的那一侧。

## 会话边界：一个节点一个事务

`session_factory` 每次调用开一个新会话。`commit_plan` 的四件事必须在**同一个**
事务里（归档 + 推进进度 + 结算欠账 + 写锚点），所以它自己 `commit()`；
其余节点只读或只写临时物化，会话随节点结束而关闭。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.types import Command
from sqlalchemy.orm import Session

from backend.agents.diagnosis import run_diagnosis
from backend.components.explain import explain as run_explain
from backend.components.planner import planner_node, rollback_revision
from backend.components.route import route_node
from backend.core.config import Settings, get_settings
from backend.core.db import session_scope
from backend.graph.events import emit
from backend.graph.state import FTSState, model_get
from backend.graph.state import get as state_get
from backend.harness import Harness, PromptRegistry
from backend.nodes.commit_plan import commit_plan_node
from backend.nodes.compile_spec import bundle_from_spec, compile_spec_node
from backend.nodes.human_gate import human_gate
from backend.nodes.resume_guard import resume_guard
from backend.nodes.solve import solve_node
from backend.nodes.validate import validate_node
from backend.planner.calibration import DEFAULT_CALIBRATOR, ConfidenceCalibrator
from backend.routing.entities import EntityDirectory
from backend.schemas.intent import ConstraintSpec
from backend.schemas.plan import SchedulePlan
from backend.schemas.solver import SolverStats
from backend.schemas.validation import ValidationReport
from backend.skills_loader import SkillLibrary, load_library

#: 图里全部节点名。**与 v6 §7.5 的节点集一致**：4 个 LLM 组件 + 6 个确定性节点
#: + 1 个 Agent（diagnosis）。`knowledge` 不在图内——问答链路由 W8 承接。
NODE_NAMES: tuple[str, ...] = (
    "route",
    "planner",
    "compile_spec",
    "solve",
    "validate",
    "explain",
    "diagnosis",
    "resume_guard",
    "human_gate",
    "commit_plan",
)


@dataclass
class GraphDeps:
    """图运行需要的外部依赖。**全部可替换**，测试里换成假的即可。"""

    session_factory: Callable[[], Any] = session_scope
    #: 每个请求一个 `Harness`（M4-A §8 第 4 条：一个请求一本预算账）。
    #: 返回 None 即「不用 LLM」——FTS-4001 的降级路径与 CI 都走这一支。
    harness_factory: Callable[[FTSState], Harness | None] = lambda _state: None
    directory: EntityDirectory = field(default_factory=EntityDirectory)
    library: SkillLibrary | None = None
    calibrator: ConfidenceCalibrator = DEFAULT_CALIBRATOR
    settings: Settings | None = None
    #: 「今天」由外部给，不在图里调 `date.today()`——重放时它必须是同一个值
    today: date = field(default_factory=date.today)
    #: 训练窗起点，修订约束的分钟数以它为原点
    window_start: time = time(6, 0)
    horizon_minutes: int = 720
    plans_root: Path | None = None
    prompt_versions: dict[str, str] = field(default_factory=dict)

    def config(self) -> Settings:
        return self.settings or get_settings()

    def skill_version(self) -> str | None:
        """知识层指纹，进 manifest。空库返回 None 而不是空串。"""
        if self.library is None or self.library.empty:
            return None
        return self.library.fingerprint()

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.session_factory() as session:
            yield cast(Session, session)


def default_deps(*, load_skills: bool = True, **kwargs: Any) -> GraphDeps:
    """常用装配：加载知识层与提示词版本表。"""
    deps = GraphDeps(**kwargs)
    if load_skills and deps.library is None:
        deps.library = load_library()
    if not deps.prompt_versions:
        deps.prompt_versions = dict(PromptRegistry.load().versions())
    return deps


# ─────────────────────────────────────────────────────────────────────
# 节点包装
# ─────────────────────────────────────────────────────────────────────
#
# 每个包装函数的签名都是 `(state, deps) -> Command`，**不是**「返回一个闭包的
# 工厂」。两个理由：
#
# 1. 这样它们能脱离 LangGraph 直接单测（给一个假的 `GraphDeps` 就能跑）；
# 2. `StateGraph.add_node` 在 mypy --strict 下**只接受真正的函数对象**，
#    显式标注成 `Callable[[FTSState], Command[str]]` 的值会让那组 overload
#    全部匹配失败（langgraph 1.2 的 `_Node` 是带具名形参的 Protocol）。
#    `build_graph` 里因此用嵌套 `def` 把 deps 闭包进去，而不是传闭包变量。
def _route(state: FTSState, deps: GraphDeps) -> Command[str]:
    return _retarget(
        route_node(
            state,
            directory=deps.directory,
            today=deps.today,
            harness=deps.harness_factory(state),
            calibrator=deps.calibrator,
            settings=deps.config(),
        )
    )


def _planner(state: FTSState, deps: GraphDeps) -> Command[str]:
    return _retarget(
        planner_node(
            state,
            directory=deps.directory,
            harness=deps.harness_factory(state),
            settings=deps.config(),
            window_start=deps.window_start,
            horizon_minutes=deps.horizon_minutes,
        )
    )


def _compile_spec(state: FTSState, deps: GraphDeps) -> Command[str]:
    with deps.session() as session:
        return compile_spec_node(state, session)


def _solve(state: FTSState, deps: GraphDeps) -> Command[str]:
    with deps.session() as session:
        command = solve_node(state, session)
    # ★ 修订使问题不可行 → 回滚上一版并解释，**不静默丢弃**
    #   （v6 §7.3.4 第 3 条硬性设计 / FTS-3005）。
    #   判据是「本次带着修订栈」：首轮排班的 INFEASIBLE 该去诊断，
    #   而修订轮的 INFEASIBLE 是**这一条修订**造成的，该回滚。
    if command.goto == "diagnosis" and state_get(state, "revision_stack", []):
        rollback = rollback_revision(state, reason="移除本轮增量约束后可回到上一版方案")
        update = {**cast(dict[str, Any], command.update or {}), **rollback}
        update["solution"] = state_get(state, "solution", None)
        update["needs_human"] = True
        return Command(goto="human_gate", update=update)
    return command


def _validate(state: FTSState, deps: GraphDeps) -> Command[str]:
    with deps.session() as session:
        return validate_node(state, session, settings=deps.config())


def _explain(state: FTSState, deps: GraphDeps) -> Command[str]:
    plan = model_get(state, "solution", SchedulePlan)
    if plan is None:
        return Command(goto="resume_guard")
    harness = deps.harness_factory(state)
    if harness is None:
        from backend.components.explain import fallback_text

        validation = model_get(state, "validation", ValidationReport)
        return Command(
            goto="resume_guard",
            update={
                "explanation": fallback_text(plan, validation),
                "trace_events": emit(
                    state, "explain", "agent_end", {"degraded": True, "llm_calls": 0}
                ),
            },
        )
    result = run_explain(
        plan,
        harness=harness,
        validation=model_get(state, "validation", ValidationReport),
        stats=model_get(state, "solver_stats", SolverStats),
        library=deps.library,
        settings=deps.config(),
    )
    return Command(
        goto="resume_guard",
        update={
            "explanation": result.text,
            "grounding_report": result.report,
            "trace_events": emit(
                state,
                "explain",
                "agent_end",
                {
                    "rewrites": result.rewrites,
                    "llm_calls": result.llm_calls,
                    "supported_ratio": round(result.report.supported_ratio, 4),
                    "unsupported": result.report.unsupported_claims,
                    "skills": list(result.skills_used),
                    "degraded": result.degraded,
                },
            ),
        },
    )


def _diagnosis(state: FTSState, deps: GraphDeps) -> Command[str]:
    spec = model_get(state, "constraint_spec", ConstraintSpec)
    if spec is None:
        return Command(goto="human_gate", update={"needs_human": True})
    with deps.session() as session:
        bundle = bundle_from_spec(session, spec)
        outcome = run_diagnosis(bundle, harness=deps.harness_factory(state), settings=deps.config())
    return Command(
        goto="human_gate",
        update={
            "needs_human": True,
            "conflict_set": list(outcome.conflicts),
            "relaxation_proposals": list(outcome.proposals),
            "trace_events": emit(
                state,
                "diagnosis",
                "agent_end",
                {
                    "summary": outcome.summary(),
                    "autonomous": outcome.autonomous,
                    "rounds": outcome.rounds,
                    "llm_calls": outcome.llm_calls,
                    "escalate": outcome.escalate,
                    "probe_budget": dict(outcome.probe_budget),
                    "notes": list(outcome.notes),
                },
            ),
        },
    )


def _resume_guard(state: FTSState, deps: GraphDeps) -> Command[str]:
    with deps.session() as session:
        return resume_guard(state, session)


def _commit_plan(state: FTSState, deps: GraphDeps) -> Command[str]:
    with deps.session() as session:
        return commit_plan_node(
            state,
            session,
            plans_root=deps.plans_root,
            prompt_versions=deps.prompt_versions,
            skill_version=deps.skill_version(),
        )


def _retarget(command: Command[str]) -> Command[str]:
    """把节点里写的 `"END"` 翻译成 LangGraph 的终止符。

    节点不 import `langgraph.graph.END` 是刻意的：`routing/rules.py` 是一张
    纯数据的路由表，让它去依赖图运行时既没必要，也会让那张表没法脱离
    LangGraph 单测。
    """
    if command.goto == "END":
        return Command(goto=END, update=command.update)
    return command


# ─────────────────────────────────────────────────────────────────────
# 组装
# ─────────────────────────────────────────────────────────────────────
def build_graph(
    deps: GraphDeps | None = None,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    store: BaseStore | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """组装并编译图。

    `checkpointer=None` 时图**不能跨进程恢复**——单测里可以这么用，生产路径
    必须传 `PostgresSaver`（v6 §9.2：人工确认可以隔天再来，状态在 PG 里而非内存）。
    """
    d = deps or default_deps()
    g = StateGraph(FTSState)

    # 十个嵌套 `def` 而不是十个闭包变量：见上方「节点包装」一节第 2 条。
    def route(state: FTSState) -> Command[str]:
        return _route(state, d)

    def planner(state: FTSState) -> Command[str]:
        return _planner(state, d)

    def compile_spec(state: FTSState) -> Command[str]:
        return _compile_spec(state, d)

    def solve(state: FTSState) -> Command[str]:
        return _solve(state, d)

    def validate(state: FTSState) -> Command[str]:
        return _validate(state, d)

    def explain(state: FTSState) -> Command[str]:
        return _explain(state, d)

    def diagnosis(state: FTSState) -> Command[str]:
        return _diagnosis(state, d)

    def resume(state: FTSState) -> Command[str]:
        return _resume_guard(state, d)

    def commit(state: FTSState) -> Command[str]:
        return _commit_plan(state, d)

    g.add_node("route", route, destinations=("planner", "human_gate", END))
    g.add_node("planner", planner, destinations=("compile_spec", "solve", "route", "human_gate"))
    g.add_node("compile_spec", compile_spec, destinations=("solve",))
    g.add_node("solve", solve, destinations=("validate", "diagnosis", "human_gate"))
    g.add_node("validate", validate, destinations=("explain", "solve", "diagnosis"))
    g.add_node("explain", explain, destinations=("resume_guard",))
    g.add_node("diagnosis", diagnosis, destinations=("human_gate",))
    g.add_node("resume_guard", resume, destinations=("human_gate", "planner"))
    g.add_node("human_gate", human_gate, destinations=("commit_plan", "planner", END))
    g.add_node("commit_plan", commit, destinations=(END,))

    g.add_edge(START, "route")
    return g.compile(checkpointer=checkpointer, store=store)


__all__ = ["NODE_NAMES", "GraphDeps", "build_graph", "default_deps"]
