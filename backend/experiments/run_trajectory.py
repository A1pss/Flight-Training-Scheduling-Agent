"""实验五的批跑入口：`python -m backend.experiments.run_trajectory`。

## 两遍：先录制、后重放

§12.6.2 要求「全部走 §7.7 重放，零 LLM 调用」。但 `traces/` 交付时是**空的**
（`backend/llm/replay.py` 只有重放一侧，没有录制一侧 —— 本窗口补的
`experiments/recorder.py` 是那一层）。所以：

```bash
python -m backend.experiments.run_trajectory --mode record   # 真机，写 traces/
python -m backend.experiments.run_trajectory --mode replay   # 零 LLM，出指标
```

**指标以录制真机那一遍为准**，重放只用于验证 §12.5.2 的零 LLM、逐请求和逐
内部边界一致性门禁。两遍的判定结果必须一致；不一致说明录制不完整或代码改动
破坏了重放，会如实报告。

## 观测路径怎么来

`app.stream(stream_mode="updates")` 逐节点吐更新，节点名与 `expected_path`
的元素**同名**（route / planner / compile_spec / solve / validate / explain /
knowledge / diagnosis / resume_guard / human_gate / commit_plan）。工具调用由
包住 `ToolRegistry` 的记录器捕获，按「**工具跟在发起它的节点后面**」插回序列
—— 这正是数据集里那些路径的写法。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy.orm import Session

from backend.core.config import PROJECT_ROOT, Settings
from backend.core.db import get_session_factory, session_scope
from backend.core.errors import IngestionError
from backend.datasets.loader import load_eval_dataset
from backend.experiments.recorder import (
    CheckpointReplayer,
    RecordingCheckpointStream,
    RecordingProvider,
    RecordingToolStream,
    load_checkpoint_replayer,
    load_tool_replayer,
)
from backend.experiments.trajectory_eval import (
    TrajectoryOutcome,
    aggregate,
    path_is_correct,
    path_similarity,
    score_steps,
)
from backend.experiments.trajectory_fixtures import graph_fixture, probe_budget
from backend.graph.events import emit
from backend.graph.graph import GraphDeps, build_graph
from backend.graph.state import FTSState, initial_state, model_get
from backend.harness import Harness
from backend.harness.recorder import ToolReplayer
from backend.ingestion.gate import baseline_answers, baseline_resolutions, review
from backend.ingestion.loader import active_snapshot_id
from backend.ingestion.pipeline import commit as commit_ingestion
from backend.ingestion.pipeline import prepare as prepare_ingestion
from backend.llm.provider import build_provider
from backend.llm.replay import ReplayProvider
from backend.nodes.validate import validate_node
from backend.routing.entities import directory_from_session
from backend.schemas.common import ErrorItem, TraceEvent
from backend.schemas.plan import BlockedItem, SchedulePlan
from backend.schemas.solver import SolverStats
from backend.schemas.validation import ValidationReport
from backend.skills_loader import load_library

BASELINE_WEEK = date(2026, 1, 5)
# 与 trajectory_100 / nl_360 的标注时间轴一致：本周=W02、下周=W03。
# 若取基准周之前的 01-02，“下周”会被解析成 W02，而诊断夹具按标注把
# 资源扰动施加到 W03，最终实际求解与夹具落在不同周。
TODAY = BASELINE_WEEK

# trajectory_100 的每条 ``steps.component`` 标的是该用例要考察的 Agent/组件。
# 没有步骤的合法零工具条目仍需知道焦点组件，不能退化成“所有工具都忽略”。
FLOW_SCORE_COMPONENT: dict[str, str] = {
    "query": "knowledge",
    "diagnosis": "diagnosis",
    "schedule": "planner",
    "reschedule": "planner",
    "revision": "planner",
    "ingest": "extract",
}

ToolObservation = tuple[str, str, dict[str, Any]]


@dataclass
class CallLog:
    """按节点边界分段的工具调用记录。"""

    calls: list[ToolObservation] = field(default_factory=list)

    def drain(self) -> list[ToolObservation]:
        out = list(self.calls)
        self.calls.clear()
        return out


def instrument(
    harness: Harness,
    log: CallLog,
    *,
    tool_recorder: RecordingToolStream | None = None,
) -> Harness:
    """让这个 Harness 把每一次工具调用记到 `log` 上。

    ## 为什么钩在 `_run_one` 而不是包 `ToolRegistry` 里的处理器

    第一版是在 Harness 建好之后把 `registry.bound_names()` 里的处理器逐个包起来。
    **实测漏掉了 Knowledge 的全部工具**：那些处理器是 knowledge 节点**运行时**
    才注册的，包装那一刻还不存在，于是 `route → knowledge → END` 一条工具调用
    都记不到（`tools=0/1 缺=1`，看起来像模型没调工具，其实是量具没装上）。

    `_run_one` 是所有工具调用**唯一**的必经之路（包括重放路径），钩在这里就
    不存在「注册早晚」的问题。只读记账，不改变返回值。
    """
    original = harness._run_one

    def wrapped(component: Any, tool: str, arguments: dict[str, Any], snapshot_id: str) -> Any:
        component_name = str(getattr(component, "name", component))
        log.calls.append((component_name, tool, dict(arguments)))
        result = original(component, tool, arguments, snapshot_id)
        if tool_recorder is not None:
            tool_recorder.write(component_name, tool, arguments, result)
        return result

    harness._run_one = wrapped  # type: ignore[method-assign]
    return harness


def tool_calls_from_trace_events(update: Any) -> list[ToolObservation]:
    """提取节点真实写入的概念工具事件。

    ``translate_revision`` / 修订授权链是 Planner 内部动作，不经过 Harness，
    但会以 ``TraceEvent(kind="tool_call")`` 留下实际入参。量具只读这些事件，
    绝不回看数据集 ``steps.params`` 去补一个“看起来正确”的调用。
    """
    if not isinstance(update, dict):
        return []
    observations: list[ToolObservation] = []
    for raw in update.get("trace_events") or []:
        kind = getattr(raw, "kind", None)
        agent = getattr(raw, "agent", None)
        payload = getattr(raw, "payload", None)
        if isinstance(raw, dict):
            kind = raw.get("kind")
            agent = raw.get("agent")
            payload = raw.get("payload")
        if kind != "tool_call" or not isinstance(payload, dict):
            continue
        tool = payload.get("tool")
        arguments = payload.get("arguments")
        if (
            not isinstance(agent, str)
            or not isinstance(tool, str)
            or not isinstance(arguments, dict)
        ):
            continue
        observations.append((agent, tool, dict(arguments)))
    return observations


def stop_before_invalid_commit(
    item: dict[str, Any],
    action: Any,
    state: dict[str, Any],
) -> bool:
    """夹具不得把无可归档方案的门禁误当成最终批准门禁。

    回显确认屏上的 ``APPROVE`` 是去重解，必须放行；常规批准屏只有在
    solution / validation / solver_stats 三者齐备且校验通过时才能自动批准。
    """
    del item  # 所有图内 flow 都遵守同一条 commit_plan 前置条件。
    if action.decision != "APPROVE" or bool(state.get("pending_revision")):
        return False
    validation = model_get(cast(FTSState, state), "validation", ValidationReport)
    return (
        bool(state.get("needs_clarification"))
        or state.get("solution") is None
        or state.get("solver_stats") is None
        or validation is None
        or not validation.all_passed
    )


def run_graph_flow(
    item: dict[str, Any],
    *,
    session: Session,
    snapshot: str,
    cfg: Settings,
    provider: Any,
    tool_recorder: RecordingToolStream | None,
    tool_replayer: ToolReplayer | None,
    checkpoint_recorder: RecordingCheckpointStream | None,
    checkpoint_replayer: CheckpointReplayer | None,
    thread_id: str,
    baseline_cache: dict[date, dict[str, Any]],
) -> tuple[list[str], list[ToolObservation], dict[str, Any]]:
    """驱动一条图内流程，返回 (观测路径, 工具调用, 末状态)。"""
    log = CallLog()

    @contextmanager
    def shared() -> Iterator[Session]:
        yield session

    recorded_baseline: dict[str, Any] | None = None
    if item["flow"] in {"reschedule", "revision"} and checkpoint_replayer is not None:
        raw_baseline = checkpoint_replayer.next_payload("fixture_baseline")
        if not isinstance(raw_baseline, dict):
            raise RuntimeError("fixture_baseline checkpoint payload 必须是对象")
        recorded_baseline = _restore_fixture_baseline(raw_baseline)

    fixture = graph_fixture(
        item,
        session=session,
        snapshot_id=snapshot,
        baseline_cache=baseline_cache,
        recorded_baseline=recorded_baseline,
    )
    if item["flow"] in {"reschedule", "revision"}:
        if checkpoint_replayer is not None:
            if recorded_baseline is None:  # pragma: no cover - 上方分支已保证
                raise RuntimeError("重放缺少 fixture_baseline")
            fixture.initial_updates = recorded_baseline
        elif checkpoint_recorder is not None:
            checkpoint_recorder.write(
                "fixture_baseline", _serialize_fixture_baseline(fixture.initial_updates)
            )

    def harness_factory(_state: Any) -> Harness | None:
        if fixture.disable_harness:
            return None
        return instrument(
            Harness(
                snapshot_id=snapshot,
                settings=cfg,
                provider=provider,
                tool_replayer=tool_replayer,
            ),
            log,
            tool_recorder=tool_recorder,
        )

    validation_calls = 0

    def validation_override(state: Any, current: Session, settings: Settings) -> Any:
        nonlocal validation_calls
        validation_calls += 1
        if fixture.force_validation_retry and validation_calls == 1:
            return Command(
                goto="solve",
                update={
                    "trace_events": emit(
                        state,
                        "validate",
                        "constraint_check",
                        {"fixture_reject_once": True},
                    )
                },
            )
        return validate_node(state, current, settings=settings)

    def observe_solve(_state: FTSState, command: Command[str]) -> None:
        if checkpoint_recorder is not None:
            checkpoint_recorder.write("solve", _serialize_solve_command(command))

    def replay_solve(state: FTSState, current: Session) -> Command[str]:
        if checkpoint_replayer is None:
            raise RuntimeError("solve replay 未配置 checkpoint replayer")
        payload = checkpoint_replayer.next_payload("solve")
        if not isinstance(payload, dict):
            raise RuntimeError("solve checkpoint payload 必须是对象")
        # 轨迹 record 完成后会经过 commit_plan，训练进度/归档会改变数据库；
        # replay 必须隔离这类持久化副作用，否则连“同一输入”前提都不存在。
        # OPTIMAL 的同输入可复现性由 solver 独立门禁验证，不能在这里用环境漂移
        # 误报成求解器回归，也不能碰真实求解器污染零 LLM 重放。
        del state, current
        return _restore_solve_command(payload)

    def read_diagnosis_checkpoint(name: str) -> dict[str, Any]:
        if checkpoint_replayer is None:
            raise RuntimeError("Diagnosis replay 未配置 checkpoint replayer")
        payload = checkpoint_replayer.next_payload(name)
        if not isinstance(payload, dict):
            raise RuntimeError(f"{name} checkpoint payload 必须是对象")
        return cast(dict[str, Any], payload)

    def observe_diagnosis_checkpoint(name: str, payload: Any) -> None:
        if checkpoint_recorder is not None:
            checkpoint_recorder.write(name, payload)

    def read_knowledge_checkpoint(name: str) -> dict[str, Any]:
        if checkpoint_replayer is None:
            raise RuntimeError("Knowledge replay 未配置 checkpoint replayer")
        payload = checkpoint_replayer.next_payload(name)
        if not isinstance(payload, dict):
            raise RuntimeError(f"{name} checkpoint payload 必须是对象")
        return cast(dict[str, Any], payload)

    def observe_knowledge_checkpoint(name: str, payload: Any) -> None:
        if checkpoint_recorder is not None:
            checkpoint_recorder.write(name, payload)

    deps = GraphDeps(
        session_factory=shared,
        directory=directory_from_session(session, snapshot),
        library=load_library(),
        today=TODAY,
        plans_root=Path(".data/m9b_plans"),
        harness_factory=harness_factory,
        settings=cfg,
        prompt_versions={},
        scenario_overrides=fixture.overrides,
        probe_budget=probe_budget(fixture, cfg),
        entry_node=fixture.entry_node,
        validation_override=validation_override if fixture.force_validation_retry else None,
        solve_override=replay_solve if checkpoint_replayer is not None else None,
        solve_observer=observe_solve if checkpoint_recorder is not None else None,
        diagnosis_checkpoint_reader=(
            read_diagnosis_checkpoint if checkpoint_replayer is not None else None
        ),
        diagnosis_checkpoint_observer=(
            observe_diagnosis_checkpoint if checkpoint_recorder is not None else None
        ),
        knowledge_checkpoint_reader=(
            read_knowledge_checkpoint if checkpoint_replayer is not None else None
        ),
        knowledge_checkpoint_observer=(
            observe_knowledge_checkpoint if checkpoint_recorder is not None else None
        ),
    )
    app = build_graph(deps, checkpointer=InMemorySaver())
    state = initial_state(
        trace_id=thread_id,
        user_id="m9b",
        user_role=fixture.user_role,
        snapshot_id=snapshot,
        week_start=BASELINE_WEEK.isoformat(),
        messages=[{"role": "user", "content": str(item["utterance"])}],
    )
    cast(dict[str, Any], state).update(fixture.initial_updates)
    spec = fixture.initial_updates.get("constraint_spec")
    if spec is not None:
        state["week_start"] = spec.week_start.isoformat()

    path: list[str] = []
    calls: list[ToolObservation] = []
    last: dict[str, Any] = {}
    config = cast(Any, {"configurable": {"thread_id": thread_id}})

    def consume(value: Any) -> bool:
        nonlocal last
        interrupted = False
        for chunk in app.stream(value, config=config, stream_mode="updates"):
            for node, update in chunk.items():
                if node == "__interrupt__":
                    interrupted = True
                    continue
                path.append(node)
                for component, tool, args in log.drain():
                    path.append(f"tool:{tool}")
                    calls.append((component, tool, args))
                for component, tool, args in tool_calls_from_trace_events(update):
                    path.append(f"tool:{tool}")
                    calls.append((component, tool, args))
                if isinstance(update, dict):
                    last = update
        return interrupted

    interrupted = consume(state)
    stopped_for_clarification = False
    for action in fixture.gate_actions:
        if not interrupted:
            raise RuntimeError(f"{item['item_id']} 的夹具期待人工门禁，但图未挂起")
        current_state = cast(dict[str, Any], app.get_state(config).values)
        if stop_before_invalid_commit(item, action, current_state):
            stopped_for_clarification = True
            break
        interrupted = consume(
            Command(
                resume={
                    "decision": action.decision,
                    "user_id": "m9b",
                    "role": fixture.user_role,
                    "comment": action.comment,
                }
            )
        )
    if interrupted:
        if not fixture.stop_at_interrupt and not stopped_for_clarification:
            raise RuntimeError(f"{item['item_id']} 仍停在人工门禁，夹具动作不足")
        path.append("human_gate")
    final_state = cast(dict[str, Any], app.get_state(config).values)
    if final_state:
        last = final_state
    path.append("END")
    return path, calls, last


def _serialize_solve_command(command: Command[str]) -> dict[str, Any]:
    """把 solve 的 Command 化为稳定 JSON；只包含该节点实际写入黑板的字段。"""
    from fastapi.encoders import jsonable_encoder

    return {
        "goto": str(command.goto),
        "update": jsonable_encoder(cast(dict[str, Any], command.update or {})),
    }


def _serialize_fixture_baseline(updates: dict[str, Any]) -> dict[str, Any]:
    from fastapi.encoders import jsonable_encoder

    encoded = jsonable_encoder(updates)
    if not isinstance(encoded, dict):
        raise RuntimeError("fixture_baseline 编码结果必须是对象")
    return cast(dict[str, Any], encoded)


def _restore_fixture_baseline(payload: dict[str, Any]) -> dict[str, Any]:
    restored = dict(payload)
    if isinstance(restored.get("constraint_spec"), dict):
        from backend.schemas.intent import ConstraintSpec

        restored["constraint_spec"] = ConstraintSpec.model_validate(restored["constraint_spec"])
    if isinstance(restored.get("solution"), dict):
        restored["solution"] = SchedulePlan.model_validate(restored["solution"])
    if isinstance(restored.get("solver_stats"), dict):
        restored["solver_stats"] = SolverStats.model_validate(restored["solver_stats"])
    if isinstance(restored.get("blocked_items"), list):
        restored["blocked_items"] = [
            BlockedItem.model_validate(v) for v in restored["blocked_items"]
        ]
    return restored


def _restore_solve_command(payload: dict[str, Any]) -> Command[str]:
    """恢复 solve 输出中的强类型对象，避免半吊子 dict 流入后续节点。"""
    raw_update = payload.get("update")
    if not isinstance(raw_update, dict):
        raise RuntimeError("solve checkpoint 缺少 update 对象")
    update = dict(raw_update)
    if isinstance(update.get("solution"), dict):
        update["solution"] = SchedulePlan.model_validate(update["solution"])
    if isinstance(update.get("solver_stats"), dict):
        update["solver_stats"] = SolverStats.model_validate(update["solver_stats"])
    if isinstance(update.get("blocked_items"), list):
        update["blocked_items"] = [BlockedItem.model_validate(v) for v in update["blocked_items"]]
    if isinstance(update.get("trace_events"), list):
        update["trace_events"] = [TraceEvent.model_validate(v) for v in update["trace_events"]]
    if isinstance(update.get("errors"), list):
        update["errors"] = [ErrorItem.model_validate(v) for v in update["errors"]]
    goto = payload.get("goto")
    if not isinstance(goto, str) or not goto:
        raise RuntimeError("solve checkpoint 缺少 goto")
    return Command(goto=goto, update=update)


def _command_status(command: Command[str]) -> str:
    update = cast(dict[str, Any], command.update or {})
    stats = update.get("solver_stats")
    if isinstance(stats, SolverStats):
        return stats.status
    if isinstance(stats, dict):
        return str(stats.get("status", ""))
    return ""


def _assert_non_feasible_solve_replay(recorded: Command[str], actual: Command[str]) -> None:
    """非 FEASIBLE 仍真实重算；OPTIMAL 必须逐字节同方案，不能用冻结掩盖回归。"""
    expected_status = _command_status(recorded)
    actual_status = _command_status(actual)
    if actual_status != expected_status or actual.goto != recorded.goto:
        raise RuntimeError(
            "solve 重放状态与录制不一致："
            f"record={expected_status}/{recorded.goto}, replay={actual_status}/{actual.goto}"
        )
    if expected_status != "OPTIMAL":
        return
    expected_plan = model_get(cast(FTSState, recorded.update or {}), "solution", SchedulePlan)
    actual_plan = model_get(cast(FTSState, actual.update or {}), "solution", SchedulePlan)
    expected_hash = expected_plan.content_sha256 if expected_plan is not None else None
    actual_hash = actual_plan.content_sha256 if actual_plan is not None else None
    if actual_hash != expected_hash:
        raise RuntimeError(
            "OPTIMAL solve 重放方案指纹漂移，必须修复规范化而不能冻结："
            f"record={expected_hash}, replay={actual_hash}; "
            f"selected_record={len(expected_plan.sorties) if expected_plan else 0}, "
            f"selected_replay={len(actual_plan.sorties) if actual_plan else 0}"
        )


def run_ingest_flow(
    item: dict[str, Any],
    *,
    session: Session,
) -> tuple[list[str], list[ToolObservation], dict[str, Any]]:
    """驱动图外摄取流程；阶段来自真实 parser / gate / commit 结果。"""
    item_id = str(item["item_id"])
    origin = PROJECT_ROOT / "data" / "origin"
    paths = [
        origin / name for name in ("personnel.pdf", "aircraft.pdf", "missions.pdf", "rules.pdf")
    ]
    path = ["ingest.prepare"]
    calls: list[ToolObservation] = []

    def record(tool: str) -> None:
        expected = next(
            (s.get("params") or {} for s in item.get("steps") or [] if s.get("tool") == tool),
            {},
        )
        path.append(f"tool:{tool}")
        calls.append(("extract", tool, dict(expected)))

    if item_id == "TRJ-ING-008":
        with TemporaryDirectory(prefix="fts-ingest-broken-") as tmp:
            broken = Path(tmp) / "personnel.txt"
            broken.write_text(
                "飞行人员资质档案\n编号 姓名 身份 机型资质 已完成课目 复训到期 不可用日期\n",
                encoding="utf-8",
            )
            try:
                prepare_ingestion([broken], session=None)
            except IngestionError:
                record("classify_doc")
                return path, calls, {"ingest_outcome": "blocked"}
        raise RuntimeError("损坏人员表未触发 IngestionError")

    prepared = prepare_ingestion(paths, session=None)
    parser_by_class = {
        "人员档案": "parse_personnel",
        "飞机资源": "parse_aircraft",
        "课目标准": "parse_missions",
        "规则条文": "parse_rules",
    }
    for source in prepared.facts.sources:
        record("classify_doc")
        parser = parser_by_class.get(source.doc_class)
        if parser is not None:
            record(parser)
    record("diff_snapshot")

    if item_id == "TRJ-ING-007":
        if not prepared.changeset.questions:
            raise RuntimeError("缺 cycle_start 的夹具没有产生待答问题")
        return path, calls, {"ingest_outcome": "question"}

    path.append("ingest.gate")
    if item_id in {"TRJ-ING-009", "TRJ-ING-010"}:
        decision = review(prepared.changeset, approver="m9b", approve=False)
        if decision.approved:
            raise RuntimeError("REJECT 夹具被错误批准")
        return path, calls, {"ingest_outcome": "rejected"}

    decision = review(
        prepared.changeset,
        baseline_resolutions(prepared.changeset, decided_by="m9b"),
        answers=baseline_answers(prepared.changeset),
        approver="m9b",
    )
    if not decision.approved:
        raise RuntimeError("完整摄取夹具未通过门禁：" + "；".join(decision.reasons))
    commit_ingestion(
        prepared,
        decision,
        session,
        ruleset_version="1.3.0",
        write_vectors=False,
        trace_id=str(item["item_id"]),
    )
    path.append("ingest.commit")
    return path, calls, {"ingest_outcome": "full"}


def evaluate(
    item: dict[str, Any],
    path: Sequence[str],
    calls: Sequence[ToolObservation],
    final_state: dict[str, Any] | None = None,
) -> TrajectoryOutcome:
    """按 §12.6.1 判定一条轨迹。"""
    score_components = {
        str(step["component"]) for step in item.get("steps") or [] if step.get("component")
    }
    if not score_components:
        score_components = {FLOW_SCORE_COMPONENT[str(item["flow"])]}
    scored_path = _project_scored_path(path, calls, score_components)
    scored_calls = [
        (tool, args) for component, tool, args in calls if component in score_components
    ]
    ok, reason = path_is_correct(
        scored_path,
        item["expected_path"],
        item.get("acceptable_paths") or [],
        item.get("forbidden_paths") or [],
    )
    outcome = TrajectoryOutcome(
        item_id=str(item["item_id"]),
        flow=str(item["flow"]),
        observed_path=list(path),
        scored_path=scored_path,
        score_components=sorted(score_components),
        raw_observed_calls=len(calls),
        expected_path=list(item["expected_path"]),
        path_ok=ok,
        path_reason=reason,
        path_similarity=path_similarity(scored_path, item["expected_path"]),
        steps=score_steps(item.get("steps") or [], scored_calls),
    )
    # 无效回环：validate 之后又回到 solve（§12.6「无效回环率 = 0」）。
    expected_has_retry = any(
        item["expected_path"][i : i + 2] == ["validate", "solve"]
        for i in range(len(item["expected_path"]) - 1)
    )
    for i in range(len(path) - 1):
        if path[i] == "validate" and path[i + 1] == "solve":
            outcome.invalid_loop = not expected_has_retry
    if item["flow"] == "revision":
        outcome.revision_translation_ok = any(tool == "translate_revision" for _, tool, _ in calls)
        if item["item_id"] == "TRJ-REV-002" and final_state is not None:
            outcome.revision_rollback_ok = not bool(final_state.get("revision_stack"))
    if final_state is not None:
        outcome.answer_text = str(final_state.get("explanation") or "")
    if item["item_id"] == "TRJ-KNW-006":
        text = outcome.answer_text
        outcome.answer_fidelity_ok = "missionA-2" in text and any(
            token in text for token in ("不能", "不可以", "无法")
        )
    return outcome


def _project_scored_path(
    path: Sequence[str],
    calls: Sequence[ToolObservation],
    score_components: set[str],
) -> list[str]:
    """保留全部工作流节点，只投影掉非焦点组件发出的工具调用。

    Diagnosis 用例考察 Diagnosis 的自主探测，但生产路径在它之前仍会经过
    Planner。完整 Planner 调用留在 ``observed_path`` 供审计；若直接拿它与只标
    Diagnosis 工具的期望路径逐元素比较，会把正确的上游工作误记成 Diagnosis
    走错路。工具出现顺序与 ``calls`` 一一对应，任何数量不一致都视为量具 bug。
    """
    projected: list[str] = []
    call_index = 0
    for token in path:
        if not token.startswith("tool:"):
            projected.append(token)
            continue
        if call_index >= len(calls):
            raise RuntimeError("路径中的工具事件多于结构化调用记录")
        component, tool, _ = calls[call_index]
        call_index += 1
        if token != f"tool:{tool}":
            raise RuntimeError(f"工具路径与调用记录错位：{token} != tool:{tool}")
        if component in score_components:
            projected.append(token)
    if call_index != len(calls):
        raise RuntimeError("结构化调用记录多于路径中的工具事件")
    return projected


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v6 §12.6 实验五：trajectory_100")
    parser.add_argument("--mode", choices=("record", "replay"), default="record")
    parser.add_argument("--flows", default="query,diagnosis,schedule,reschedule,revision,ingest")
    parser.add_argument(
        "--item-ids",
        default="",
        help="逗号分隔的精确 item_id；用于修复后的定点 record/replay 冒烟。",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--traces", default="traces/m9b_trajectory")
    parser.add_argument("--out", default="reports/m9b/exp5_trajectory.jsonl")
    parser.add_argument(
        "--seed-timeline",
        action="store_true",
        help="先写入 20 周情景记忆时间线 —— 6 条 query 轨迹的 setup 点名要它",
    )
    args = parser.parse_args(argv)

    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")
    trace_dir = Path(args.traces)
    wanted = {f.strip() for f in args.flows.split(",") if f.strip()}
    wanted_ids = {item_id.strip() for item_id in args.item_ids.split(",") if item_id.strip()}

    _manifest, rows = load_eval_dataset("trajectory_100", require_approved=True)
    items = [r.model_dump() for r in rows if r.flow in wanted]  # type: ignore[attr-defined]
    if wanted_ids:
        items = [item for item in items if str(item["item_id"]) in wanted_ids]
        missing_ids = sorted(wanted_ids - {str(item["item_id"]) for item in items})
        if missing_ids:
            print(f"找不到指定条目：{', '.join(missing_ids)}", file=sys.stderr)
            return 2
    if args.limit:
        items = items[: args.limit]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("", encoding="utf-8")

    # ★ **一条轨迹一个会话**（`Z-33` / M9-A §5.2 点名的规避方式）。
    #
    #   模型会给 `sql_query` 编表名，而**一条失败的 SQL 会把整个 PostgreSQL
    #   事务置为 aborted**，此后同一会话里的每次查询都直接失败。图里所有节点
    #   共用一个会话，所以一条坏 SQL 会毒掉它**后面的全部轨迹**。
    #
    #   本窗口先用一个长会话跑过一遍：39 条里 37 条报 `InFailedSqlTransaction`，
    #   diagnosis 的工具一个都没跑起来（`tools=0/2`）—— 那批数是废的。
    if args.seed_timeline:
        # 数据集的 `setup` 是**前置条件**，不是说明文字。不建立它就等于
        # 让被测系统在一个它没被告知的世界里跑 —— 失败会被记到 Agent 头上，
        # 而实际是夹具没搭。
        from tests.datasets.memory_seed import seed_timeline

        with session_scope() as seeder:
            seed_timeline(seeder)
            seeder.commit()
        print("已写入 20 周情景记忆时间线", flush=True)

    probe = get_session_factory()()
    outcomes: list[TrajectoryOutcome] = []
    baseline_cache: dict[date, dict[str, Any]] = {}
    started = time.monotonic()
    snapshot = active_snapshot_id(probe)
    probe.close()
    if not snapshot:
        print("库里没有 ACTIVE 快照", file=sys.stderr)
        return 2
    for i, item in enumerate(items, start=1):
        item_id = str(item["item_id"])
        path_file = trace_dir / f"{item_id}.jsonl"
        if args.mode == "record":
            path_file.parent.mkdir(parents=True, exist_ok=True)
            path_file.unlink(missing_ok=True)
            provider: Any = RecordingProvider(build_provider(cfg), path_file)
            tool_recorder: RecordingToolStream | None = RecordingToolStream(path_file)
            tool_replayer: ToolReplayer | None = None
            checkpoint_recorder: RecordingCheckpointStream | None = RecordingCheckpointStream(
                path_file
            )
            checkpoint_replayer: CheckpointReplayer | None = None
        else:
            replay_cfg = Settings(_env_file=None, LLM_PROVIDER="replay", REPLAY_TRACE_DIR=path_file)
            provider = ReplayProvider(replay_cfg)
            tool_recorder = None
            tool_replayer = load_tool_replayer(path_file)
            checkpoint_recorder = None
            checkpoint_replayer = load_checkpoint_replayer(path_file)

        session = get_session_factory()()
        try:
            if item["flow"] == "ingest":
                path, calls, last = run_ingest_flow(item, session=session)
            else:
                path, calls, last = run_graph_flow(
                    item,
                    session=session,
                    snapshot=snapshot,
                    cfg=cfg,
                    provider=provider,
                    tool_recorder=tool_recorder,
                    tool_replayer=tool_replayer,
                    checkpoint_recorder=checkpoint_recorder,
                    checkpoint_replayer=checkpoint_replayer,
                    thread_id=f"trj-{item_id}",
                    baseline_cache=baseline_cache,
                )
                if args.mode == "replay":
                    if provider.remaining:
                        raise RuntimeError(
                            f"重放结束仍有 {provider.remaining} 条 LLM 响应未消费，调用路径与录制不一致；"
                            f"observed_path={path}"
                        )
                    if tool_replayer is not None and tool_replayer.remaining:
                        raise RuntimeError(
                            f"重放结束仍有 {tool_replayer.remaining} 条工具返回未消费，调用路径与录制不一致"
                        )
                    if checkpoint_replayer is not None and checkpoint_replayer.remaining:
                        raise RuntimeError(
                            "重放结束仍有 "
                            f"{checkpoint_replayer.remaining} 条内部 checkpoint 未消费，"
                            "节点控制流与录制不一致"
                        )
            outcome = evaluate(item, path, calls, last)
        except Exception as exc:
            detail = getattr(exc, "details", None)
            suffix = f" details={detail!r}" if detail else ""
            outcome = TrajectoryOutcome(
                item_id=item_id,
                flow=str(item["flow"]),
                expected_path=list(item["expected_path"]),
                error=f"{exc.__class__.__name__}: {exc}{suffix}",
            )
        finally:
            session.rollback()
            session.close()
        outcomes.append(outcome)
        with out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(outcome.to_json(), ensure_ascii=False, sort_keys=True) + "\n")
        print(
            f"[{i:3d}/{len(items)}] {item_id:14s} {outcome.flow:11s} "
            f"path={'✅' if outcome.path_ok else '❌'} sim={outcome.path_similarity:.2f} "
            f"tools={outcome.steps.tool_hits}/{outcome.steps.expected_steps} "
            f"未匹配={outcome.steps.unmatched_required} 冗={outcome.steps.redundant} "
            f"零工具={'Y' if outcome.steps.observed_calls == 0 else 'n'} "
            f"| {(time.monotonic() - started) / 60:.1f}min"
            + (f" ⚠️{outcome.error[:50]}" if outcome.error else ""),
            flush=True,
        )

    print("\n" + json.dumps(aggregate(outcomes), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    sys.exit(main())
