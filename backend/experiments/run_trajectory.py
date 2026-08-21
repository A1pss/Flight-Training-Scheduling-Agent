"""实验五的批跑入口：`python -m backend.experiments.run_trajectory`。

## 两遍：先录制、后重放

§12.6.2 要求「全部走 §7.7 重放，零 LLM 调用」。但 `traces/` 交付时是**空的**
（`backend/llm/replay.py` 只有重放一侧，没有录制一侧 —— 本窗口补的
`experiments/recorder.py` 是那一层）。所以：

```bash
python -m backend.experiments.run_trajectory --mode record   # 真机，写 traces/
python -m backend.experiments.run_trajectory --mode replay   # 零 LLM，出指标
```

**指标以重放那一遍为准**，录制那一遍只为产出轨迹。两遍的判定结果必须一致，
不一致说明录制不完整 —— 那本身是 §12.5.2 的失败，会如实报告。

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
from typing import Any, cast

from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.orm import Session

from backend.core.config import Settings
from backend.core.db import get_session_factory
from backend.datasets.loader import load_eval_dataset
from backend.experiments.recorder import RecordingProvider
from backend.experiments.trajectory_eval import (
    TrajectoryOutcome,
    aggregate,
    path_is_correct,
    path_similarity,
    score_steps,
)
from backend.graph.graph import GraphDeps, build_graph
from backend.graph.state import initial_state
from backend.harness import Harness
from backend.ingestion.loader import active_snapshot_id
from backend.llm.provider import build_provider
from backend.llm.replay import ReplayProvider
from backend.routing.entities import directory_from_session
from backend.skills_loader import load_library

BASELINE_WEEK = date(2026, 1, 5)
TODAY = date(2026, 1, 2)


@dataclass
class CallLog:
    """按节点边界分段的工具调用记录。"""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def drain(self) -> list[tuple[str, dict[str, Any]]]:
        out = list(self.calls)
        self.calls.clear()
        return out


def instrument(harness: Harness, log: CallLog) -> Harness:
    """把已绑定的工具处理器换成「先记账再执行」的包装。

    不改 `ToolRegistry`、不改任何组件 —— 评测要观测工具调用，
    生产代码不该为此长出一个钩子。
    """
    registry = harness.registry
    for name in registry.bound_names():
        original = registry.handler(name)

        def wrapped(args: dict[str, Any], _n: str = name, _f: Any = original) -> Any:
            log.calls.append((_n, dict(args)))
            return _f(args)

        registry.register(name, wrapped)
    return harness


def run_graph_flow(
    utterance: str,
    *,
    session: Session,
    snapshot: str,
    cfg: Settings,
    provider: Any,
    thread_id: str,
) -> tuple[list[str], list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    """驱动一条图内流程，返回 (观测路径, 工具调用, 末状态)。"""
    log = CallLog()

    @contextmanager
    def shared() -> Iterator[Session]:
        yield session

    def harness_factory(_state: Any) -> Harness:
        return instrument(Harness(snapshot_id=snapshot, settings=cfg, provider=provider), log)

    deps = GraphDeps(
        session_factory=shared,
        directory=directory_from_session(session, snapshot),
        library=load_library(),
        today=TODAY,
        plans_root=Path(".data/m9b_plans"),
        harness_factory=harness_factory,
        settings=cfg,
        prompt_versions={},
    )
    app = build_graph(deps, checkpointer=InMemorySaver())
    state = initial_state(
        trace_id=thread_id,
        user_id="m9b",
        user_role="director",
        snapshot_id=snapshot,
        week_start=BASELINE_WEEK.isoformat(),
        messages=[{"role": "user", "content": utterance}],
    )

    path: list[str] = []
    calls: list[tuple[str, dict[str, Any]]] = []
    last: dict[str, Any] = {}
    config = cast(Any, {"configurable": {"thread_id": thread_id}})
    for chunk in app.stream(state, config=config, stream_mode="updates"):
        for node, update in chunk.items():
            if node == "__interrupt__":
                continue
            path.append(node)
            for tool, args in log.drain():
                path.append(f"tool:{tool}")
                calls.append((tool, args))
            if isinstance(update, dict):
                last = update
    path.append("END")
    return path, calls, last


def evaluate(
    item: dict[str, Any],
    path: Sequence[str],
    calls: Sequence[tuple[str, dict[str, Any]]],
) -> TrajectoryOutcome:
    """按 §12.6.1 判定一条轨迹。"""
    ok, reason = path_is_correct(
        path,
        item["expected_path"],
        item.get("acceptable_paths") or [],
        item.get("forbidden_paths") or [],
    )
    outcome = TrajectoryOutcome(
        item_id=str(item["item_id"]),
        flow=str(item["flow"]),
        observed_path=list(path),
        expected_path=list(item["expected_path"]),
        path_ok=ok,
        path_reason=reason,
        path_similarity=path_similarity(path, item["expected_path"]),
        steps=score_steps(item.get("steps") or [], calls),
    )
    # 无效回环：validate 之后又回到 solve（§12.6「无效回环率 = 0」）。
    for i in range(len(path) - 1):
        if path[i] == "validate" and path[i + 1] == "solve":
            outcome.invalid_loop = True
    if item["flow"] == "revision":
        outcome.revision_translation_ok = any(t == "translate_revision" for t, _ in calls)
    return outcome


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v6 §12.6 实验五：trajectory_100")
    parser.add_argument("--mode", choices=("record", "replay"), default="record")
    parser.add_argument("--flows", default="query,diagnosis,schedule,reschedule")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--traces", default="traces/m9b_trajectory")
    parser.add_argument("--out", default="reports/m9b/exp5_trajectory.jsonl")
    args = parser.parse_args(argv)

    cfg = Settings(_env_file=None, LLM_PROVIDER="ollama")
    trace_dir = Path(args.traces)
    wanted = {f.strip() for f in args.flows.split(",") if f.strip()}

    _manifest, rows = load_eval_dataset("trajectory_100", require_approved=True)
    items = [r.model_dump() for r in rows if r.flow in wanted]  # type: ignore[attr-defined]
    if args.limit:
        items = items[: args.limit]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("", encoding="utf-8")

    session = get_session_factory()()
    outcomes: list[TrajectoryOutcome] = []
    started = time.monotonic()
    try:
        snapshot = active_snapshot_id(session)
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
            else:
                replay_cfg = Settings(
                    _env_file=None, LLM_PROVIDER="replay", REPLAY_TRACE_DIR=path_file.parent
                )
                provider = ReplayProvider(replay_cfg, strict_order=False)

            try:
                path, calls, _last = run_graph_flow(
                    str(item["utterance"]),
                    session=session,
                    snapshot=snapshot,
                    cfg=cfg,
                    provider=provider,
                    thread_id=f"trj-{item_id}",
                )
                outcome = evaluate(item, path, calls)
            except Exception as exc:
                outcome = TrajectoryOutcome(
                    item_id=item_id,
                    flow=str(item["flow"]),
                    expected_path=list(item["expected_path"]),
                    error=f"{exc.__class__.__name__}: {exc}",
                )
                session.rollback()
            outcomes.append(outcome)
            with out.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(outcome.to_json(), ensure_ascii=False, sort_keys=True) + "\n")
            print(
                f"[{i:3d}/{len(items)}] {item_id:14s} {outcome.flow:11s} "
                f"path={'✅' if outcome.path_ok else '❌'} sim={outcome.path_similarity:.2f} "
                f"tools={outcome.steps.tool_hits}/{outcome.steps.expected_steps} "
                f"缺={outcome.steps.missing} 冗={outcome.steps.redundant} "
                f"| {(time.monotonic() - started) / 60:.1f}min"
                + (f" ⚠️{outcome.error[:50]}" if outcome.error else ""),
                flush=True,
            )
    finally:
        session.rollback()
        session.close()

    print("\n" + json.dumps(aggregate(outcomes), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    sys.exit(main())
