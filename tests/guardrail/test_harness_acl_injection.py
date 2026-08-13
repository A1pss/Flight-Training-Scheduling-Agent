"""故障注入 ①：越权调用（v6 §7.7.2 / §12.5.1「越权拦截率 100%」）。

**30 条构造越权场景**，逐条断言被运行时拦截。分三组：

| 组 | 条数 | 构造方式 |
|---|---|---|
| A 六个确定性节点 × 五个组件轮转 | 6 | 让 LLM 组件点名 `solve` / `validate` / … |
| B 跨组件越权（拿别人行里的工具） | 18 | Planner 调检索、Knowledge 调 Planner 工具、… |
| C 写入越权与探针越权 | 6 | 非 extract 组件调 `memory.write`、非 diagnosis 调 `probe_solve` |

「拦截率 100%」这个数在本项目里是**确定性**的（v6 §12.5.1 特别标了「确定性」），
所以这组用例的正确形态是**逐条断言**，不是「跑 30 条统计一下比例」——比例低于
100% 的时候，需要知道的是**哪一条**漏了。最后那条汇总用例把 30/30 打印出来，
供收工报告贴证据。
"""

from __future__ import annotations

import pytest

from backend.core.errors import (
    ArchitecturalBanError,
    FTSError,
    ToolPermissionDeniedError,
)
from backend.harness.acl import FORBIDDEN_NODES
from backend.harness.types import AgentSpec, ComponentName
from backend.llm.mock import tool_response
from tests.fixtures.harness_fixtures import build_harness

pytestmark = pytest.mark.guardrail

#: 每个组件用一个**它确实有权**的工具开场，保证被拒的原因只可能是越权本身。
LEGIT_TOOL: dict[ComponentName, str] = {
    "route": "resolve_person",
    "planner": "estimate_scope",
    "extract": "classify_doc",
    "knowledge": "prereq_cte",
    "diagnosis": "min_conflict_set",
    "explain": "verify_claim",
}

# ── A 组：六个确定性节点（铁律 4 / §7.7.2 最后两行）──────────────────
NODE_CASES: list[tuple[ComponentName, str]] = [
    ("planner", "solve"),
    ("route", "validate"),
    ("planner", "compile_spec"),
    ("diagnosis", "resume_guard"),
    ("explain", "human_gate"),
    ("knowledge", "commit_plan"),
]

# ── B 组：跨组件越权 ─────────────────────────────────────────────────
CROSS_CASES: list[tuple[ComponentName, str]] = [
    ("route", "propose_solve_intent"),
    ("route", "sql_query"),
    ("route", "probe_solve"),
    ("route", "render_workbook"),
    ("planner", "sql_query"),
    ("planner", "vector_search"),
    ("planner", "parse_personnel"),
    ("planner", "render_workbook"),
    ("knowledge", "propose_solve_intent"),
    ("knowledge", "ask_user"),
    ("knowledge", "min_conflict_set"),
    ("knowledge", "compose_report"),
    ("diagnosis", "translate_revision"),
    ("diagnosis", "propose_change"),
    ("explain", "estimate_scope"),
    ("explain", "diff_snapshot"),
    ("extract", "check_authority"),
    ("extract", "rank_relaxations"),
]

# ── C 组：写入越权与探针越权 ─────────────────────────────────────────
WRITE_AND_PROBE_CASES: list[tuple[ComponentName, str]] = [
    ("route", "memory.write"),
    ("planner", "memory.write"),
    ("knowledge", "memory.write"),
    ("explain", "memory.write"),
    ("planner", "probe_solve"),
    ("explain", "probe_solve"),
]

ALL_CASES = NODE_CASES + CROSS_CASES + WRITE_AND_PROBE_CASES


def _attempt(component: ComponentName, tool: str) -> FTSError:
    """让模型点名一个越权工具，返回被抛出的异常。"""
    harness, provider, _ = build_harness([tool_response(tool, {})])
    agent = AgentSpec(name=component, tools=(LEGIT_TOOL[component],))
    with pytest.raises(ToolPermissionDeniedError) as exc:
        harness.call(agent)
    # 越权不重试：只发生了一次 LLM 请求
    assert provider.call_count == 1
    assert harness.stats.acl_denials == 1
    return exc.value


def test_case_count_is_thirty() -> None:
    assert len(ALL_CASES) == 30
    assert len(set(ALL_CASES)) == 30


@pytest.mark.parametrize(("component", "tool"), NODE_CASES)
def test_deterministic_nodes_are_unreachable(component: ComponentName, tool: str) -> None:
    """六个确定性节点逐个构造调用尝试，全部 CRITICAL 拦截。"""
    error = _attempt(component, tool)
    assert isinstance(error, ArchitecturalBanError)
    assert error.severity == "CRITICAL"
    assert error.details["violation"] == "architectural_ban"
    assert error.details["tool"] == tool


def test_all_six_nodes_are_covered() -> None:
    assert {tool for _, tool in NODE_CASES} == FORBIDDEN_NODES


@pytest.mark.parametrize(("component", "tool"), CROSS_CASES)
def test_cross_component_calls_are_denied(component: ComponentName, tool: str) -> None:
    error = _attempt(component, tool)
    assert error.details["violation"] == "acl"
    assert error.details["component"] == component
    assert tool not in error.details["allowed"]


@pytest.mark.parametrize(("component", "tool"), WRITE_AND_PROBE_CASES)
def test_write_and_probe_are_denied_outside_their_row(component: ComponentName, tool: str) -> None:
    error = _attempt(component, tool)
    assert error.details["tool"] == tool


def test_interception_rate_is_one_hundred_percent(capsys: pytest.CaptureFixture[str]) -> None:
    """汇总：30/30。**这条的输出就是收工报告里贴的证据。**"""
    intercepted = 0
    log: list[str] = []
    for component, tool in ALL_CASES:
        try:
            error = _attempt(component, tool)
        except AssertionError:  # pragma: no cover —— 漏拦时要看到是哪一条
            log.append(f"✗ {component} → {tool}：未被拦截")
            continue
        intercepted += 1
        log.append(f"✓ {component} → {tool}：{type(error).__name__}({error.severity})")

    with capsys.disabled():
        print("\n── 越权拦截逐条结果（v6 §12.5.1）──")
        for line in log:
            print("   " + line)
        print(f"   拦截率 = {intercepted}/{len(ALL_CASES)} = {intercepted / len(ALL_CASES):.0%}")

    assert intercepted == 30


def test_denial_never_leaks_a_tool_result() -> None:
    """被拦的那次调用不能有任何副作用——handler 一次都不许跑。"""
    harness, _, handlers = build_harness(
        [tool_response("memory.write", {"kind": "semantic", "key": "k", "content": "v"})]
    )
    with pytest.raises(ToolPermissionDeniedError):
        harness.call(AgentSpec(name="planner", tools=("estimate_scope",)))
    assert handlers["memory.write"].calls == 0


def test_extract_may_write_memory() -> None:
    """反面对照：唯一有权写记忆的组件必须真的能写，否则这组测的就不是权限。"""
    harness, _, handlers = build_harness(
        [tool_response("memory.write", {"kind": "semantic", "key": "k", "content": "v"})]
    )
    out = harness.call(AgentSpec(name="extract", tools=("memory.write",)))
    assert out.results[0].value == {"written": "k"}
    assert handlers["memory.write"].calls == 1


def test_diagnosis_may_probe() -> None:
    """反面对照：`probe_solve` 对 Diagnosis 是开放的（§7.7.2 的唯一例外）。"""
    harness, _, _ = build_harness([tool_response("probe_solve", {"iso_week": "2026W02"})])
    out = harness.call(AgentSpec(name="diagnosis", tools=("probe_solve",)))
    assert out.results[0].ok is True
    assert harness.usage().probe_calls == 1
