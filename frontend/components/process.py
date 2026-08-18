"""运作过程页的三样纯逻辑：时间线、Graphviz 调用图、求解面板（v6 §8.2）。

## 三类节点必须视觉可分

v6 §8.2 的时间线一栏写着「Agent、LLM 节点、确定性节点用三种图标区分」，
调用图一栏写着「三类节点用不同配色」。这不是装饰：

- **确定性节点**（`compile_spec` / `solve` / `validate` / `resume_guard` /
  `human_gate` / `commit_plan`）是「LLM 碰不到的那六个」（铁律 4）；
- **LLM 节点**（`route` / `planner` / `explain` / `extract`）会调模型但不自主决定轮数；
- **Agent**（`knowledge` / `diagnosis`）自主决定循环轮数。

看一眼图就能确认「模型没有机会跳过 validate」——这正是 v6 §7.1「主体是确定性
工作流」在界面上的样子。混成一个颜色，这个保证在界面上就不可见了。

## 调用图为什么自己拼 DOT

`st.graphviz_chart()` 接受 DOT 源码字符串，**不需要 graphviz 这个 Python 包**
（本机也确实没装）。自己拼字符串还有一个好处：它是纯函数，
`tests/unit/test_frontend_process.py` 可以直接断言「回环次数标在边上」。
"""

from __future__ import annotations

import html
from collections import Counter
from itertools import pairwise
from typing import Any

from backend.schemas.api import RunResultView
from backend.schemas.common import TraceEvent

#: 三类节点（v6 §7.2 组件目录）。**这张表是唯一的分类来源**，图标与配色都查它。
DETERMINISTIC_NODES: frozenset[str] = frozenset(
    {"compile_spec", "solve", "validate", "resume_guard", "human_gate", "commit_plan"}
)
LLM_NODES: frozenset[str] = frozenset({"route", "planner", "explain", "extract"})
AGENT_NODES: frozenset[str] = frozenset({"knowledge", "diagnosis"})

NODE_ICONS: dict[str, str] = {
    "agent": "🤖",
    "llm": "▸",
    "deterministic": "⚙",
    "other": "·",
}

#: 三类节点的配色（浅底 + 深字，深浅主题下都读得出来）
NODE_COLORS: dict[str, tuple[str, str]] = {
    "agent": ("#fde68a", "#78350f"),
    "llm": ("#bfdbfe", "#1e3a8a"),
    "deterministic": ("#bbf7d0", "#14532d"),
    "other": ("#e5e7eb", "#111827"),
}

NODE_KIND_LABELS: dict[str, str] = {
    "agent": "Agent（自主决定循环轮数）",
    "llm": "LLM 节点（调模型，轮数固定）",
    "deterministic": "确定性节点（不经 Harness、不读 Skill）",
    "other": "其它",
}


def node_kind(agent: str) -> str:
    """节点名 → 三类之一。认不出的归 `other`，**不猜**。"""
    if agent in AGENT_NODES:
        return "agent"
    if agent in LLM_NODES:
        return "llm"
    if agent in DETERMINISTIC_NODES:
        return "deterministic"
    return "other"


def timeline_items(events: list[TraceEvent]) -> list[dict[str, Any]]:
    """按 `seq` 的时间线条目。**一个事件一条，不合并**。

    合并「同一个节点的多条事件」看起来更整齐，但会让步进回放的 slider 与
    时间线对不上——slider 的刻度是 `seq`，时间线要能一一对应到它。
    """
    items: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda e: e.seq):
        kind = node_kind(event.agent)
        items.append(
            {
                "seq": event.seq,
                "icon": NODE_ICONS[kind],
                "node_kind": kind,
                "agent": event.agent,
                "kind": event.kind,
                "ts": event.ts,
                "duration_ms": event.duration_ms,
                "payload": event.payload,
                "title": (
                    f"{NODE_ICONS[kind]} {event.agent} · {event.kind}"
                    + (f" · {event.duration_ms:.0f}ms" if event.duration_ms else "")
                ),
            }
        )
    return items


def replay_complete(events: list[TraceEvent]) -> bool:
    """`seq` 恰好是 `0..n-1`。**这就是「回放完整性 = 100%」的判据。**"""
    seqs = [e.seq for e in events]
    return bool(seqs) and sorted(seqs) == list(range(len(seqs)))


def call_path(events: list[TraceEvent]) -> list[str]:
    """本次实际走过的节点序列（相邻去重）。"""
    path: list[str] = []
    for event in sorted(events, key=lambda e: e.seq):
        if not path or path[-1] != event.agent:
            path.append(event.agent)
    return path


def loop_counts(path: list[str]) -> Counter[tuple[str, str]]:
    """每条边走过几次。**> 1 就是回环**（比如 validate → solve 的驳回回环）。"""
    return Counter(pairwise(path))


def call_graph_dot(events: list[TraceEvent]) -> str:
    """本次运行的调用图（DOT 源码），**含回环次数**。

    只画真的走过的节点与边（v6 §8.2「用 Graphviz 静态渲染本次实际走过的路径」），
    不画整张图 —— 画全图的话「是否发生了重解」这件事一眼看不出来。
    """
    path = call_path(events)
    if not path:
        return 'digraph fts {\n  rankdir=LR;\n  empty [label="本次运行没有事件"];\n}'

    lines = ["digraph fts {", "  rankdir=LR;", '  node [shape=box, style="rounded,filled"];']
    for name in dict.fromkeys(path):
        kind = node_kind(name)
        fill, font = NODE_COLORS[kind]
        label = html.escape(f"{NODE_ICONS[kind]} {name}")
        lines.append(f'  "{name}" [label="{label}", fillcolor="{fill}", fontcolor="{font}"];')
    for (src, dst), count in sorted(loop_counts(path).items()):
        label = f' [label="×{count}", color="#b91c1c", penwidth=2]' if count > 1 else ""
        lines.append(f'  "{src}" -> "{dst}"{label};')
    lines.append("}")
    return "\n".join(lines)


def solver_panel(run: RunResultView) -> list[dict[str, Any]]:
    """求解面板的八项 + 跑道分配统计（v6 §8.2 最后一行）。"""
    stats = run.solver.stats
    if stats is None:
        return [{"项": "求解", "值": "本次运行没有求解统计"}]
    allocation = run.solver.runway_allocation
    rows = [
        {"项": "候选数", "值": stats.num_candidates},
        {"项": "变量数", "值": stats.num_variables},
        {"项": "约束数", "值": stats.num_constraints},
        # ⚠️ 三态照原样显示：UNKNOWN ≠ INFEASIBLE（铁律 8），这里不做任何合并
        {"项": "求解状态", "值": stats.status},
        {"项": "目标值", "值": stats.objective_value if stats.objective_value is not None else "—"},
        {"项": "gap", "值": stats.gap if stats.gap is not None else "—"},
        {"项": "耗时", "值": f"{stats.wall_time_ms / 1000:.2f}s"},
        {"项": "worker / seed", "值": f"{stats.num_workers} / {stats.random_seed}"},
        {
            "项": "跑道分配统计",
            "值": "、".join(f"{k} {v} 架次" for k, v in allocation.items()) or "—",
        },
    ]
    return rows


def stage_progress(percent: int, stage: str, status: str) -> str:
    """`st.status` 的一行说明。**阶段与状态分开显示**，不合并成一个词。"""
    return f"{status} · {stage} · {percent}%"


__all__ = [
    "AGENT_NODES",
    "DETERMINISTIC_NODES",
    "LLM_NODES",
    "NODE_COLORS",
    "NODE_ICONS",
    "NODE_KIND_LABELS",
    "call_graph_dot",
    "call_path",
    "loop_counts",
    "node_kind",
    "replay_complete",
    "solver_panel",
    "stage_progress",
    "timeline_items",
]
