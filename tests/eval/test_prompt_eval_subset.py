"""提示词 eval 子集（v6 §7.7.1 第 8 行）。

> 提示词是代码：进 Git，**改动触发 CI 跑该组件的 eval 子集**，指标劣化即阻断合并。

**先把这组测的是什么说清楚，免得被当成模型能力指标。**

| 这里测 | 这里**不**测 |
|---|---|
| 提示词的**结构性契约**：版本齐全、锁文件一致、关键红线写在里面、装进上下文后不撑爆窗口 | 真模型跟不跟得住这些提示词 |
| 提示词换了以后 Harness 链路仍能跑通（MockProvider 固定桩） | 一次通过率 / 最终通过率 / 任务完成率 |

后者要靠真机 + 360 条 NL 用例，是 W11 造数据集、W13 跑评测的事（v6 §12.2 /
§12.5.1）。**本窗口不报任何模型能力指标**（铁律 6）——但把「提示词改了必须重跑
什么」这条闸门先立起来，等 W13 的数据集到位，往这个 marker 下面加用例即可。

CI 的接法：`prompts/**` 有改动 → `pytest -m prompt_eval`。
"""

from __future__ import annotations

import pytest

from backend.harness.context import ContextAssembler, ContextBlock
from backend.harness.prompts import PromptRegistry
from backend.harness.tokens import estimate_tokens
from backend.harness.types import ALL_COMPONENTS, AgentSpec, ComponentName
from backend.llm.mock import text_response, tool_response
from tests.fixtures.harness_fixtures import build_harness

pytestmark = pytest.mark.prompt_eval

#: 每个组件的提示词里**必须**出现的红线关键词。
#: 这些不是措辞偏好，是 v6 里写死的架构约束：提示词被改得把红线删掉时，
#: 模型行为会立刻变（比如 Planner 开始自己编 person_id），而这类退化在
#: 端到端指标上要跑一整轮才看得出来。
REQUIRED_PHRASES: dict[ComponentName, tuple[str, ...]] = {
    "route": ("resolve_person", "不要自己编"),
    "planner": ("SolveIntent", "不能增删任何硬约束", "escalate"),
    "extract": ("classify_doc", "绝不猜一个值填上", "upload"),
    "knowledge": ("memory.search", "检索没查到就说没查到"),
    "diagnosis": ("probe_solve", "只读探针", "UNKNOWN"),
    "explain": ("verify_claim", "不改它"),
}

#: 每个组件的冒烟用例：一条**该组件真会发出**的工具调用。
SMOKE_CALLS: dict[ComponentName, tuple[tuple[str, ...], object]] = {
    "route": (("resolve_person",), tool_response("resolve_person", {"surface": "何超"})),
    "planner": (
        ("estimate_scope",),
        tool_response(
            "estimate_scope",
            {"iso_week": "2026W02", "scope_persons": "ALL", "scope_missions": "ALL"},
        ),
    ),
    "extract": (
        ("classify_doc",),
        tool_response("classify_doc", {"filename": "personnel.pdf", "text_head": "姓名 编号"}),
    ),
    "knowledge": (
        ("prereq_cte",),
        tool_response("prereq_cte", {"person_id": "P08", "mission_id": "missionB-1"}),
    ),
    "diagnosis": (
        ("min_conflict_set",),
        tool_response("min_conflict_set", {"iso_week": "2026W02"}),
    ),
    "explain": (
        ("verify_claim",),
        tool_response("verify_claim", {"claim": "本周 14 个架次", "evidence_refs": ["sheet1"]}),
    ),
}


@pytest.mark.parametrize("component", ALL_COMPONENTS)
def test_every_component_has_a_versioned_prompt(component: ComponentName) -> None:
    prompt = PromptRegistry.load().get(component)
    assert prompt.prompt_version.startswith("v")
    assert prompt.body.strip()
    assert prompt.versioned == f"{component}/system@{prompt.prompt_version}"


@pytest.mark.parametrize("component", ALL_COMPONENTS)
def test_prompt_keeps_its_red_lines(component: ComponentName) -> None:
    """红线关键词不许被改没。改措辞可以，删约束不行。"""
    body = PromptRegistry.load().get(component).body
    missing = [p for p in REQUIRED_PHRASES[component] if p not in body]
    assert not missing, f"{component} 的提示词丢了红线：{missing}"


@pytest.mark.parametrize("component", ALL_COMPONENTS)
def test_prompt_fits_the_context_window_with_room_to_spare(component: ComponentName) -> None:
    """提示词自己不能吃掉 8K 窗口——它是钉住不裁的那一类（§7.7.1 第 5 行）。"""
    prompt = PromptRegistry.load().get(component)
    tokens = estimate_tokens(prompt.body)
    budget = ContextAssembler(num_ctx=8192, reserve_output_tokens=1024).budget
    assert tokens < budget * 0.25, f"{component} 提示词 {tokens} token，占了输入预算的四分之一以上"


@pytest.mark.parametrize("component", ALL_COMPONENTS)
def test_prompt_drives_a_working_harness_call(component: ComponentName) -> None:
    """换了提示词以后整条链路还能跑通（模型是固定桩，验的是装配不是模型）。"""
    tools, response = SMOKE_CALLS[component]
    harness, _, _ = build_harness([response])  # type: ignore[list-item]
    out = harness.call(
        AgentSpec(name=component, tools=tools),
        [ContextBlock(kind="summary", content="快照：8 人 8 机 12 课目")],
    )
    assert out.degraded is False
    assert out.calls[0].name == tools[0]
    assert out.prompt_version.startswith(f"{component}/system@")


def test_text_only_component_path_still_works() -> None:
    """解释生成也走纯文本路径（不强制工具调用）。"""
    harness, _, _ = build_harness([text_response("本周 14 个架次，其中 9 个带飞。")])
    out = harness.call(AgentSpec(name="explain", tools=(), requires_tool_call=False))
    assert out.degraded is False and out.text


def test_lockfile_matches_the_prompts_on_disk() -> None:
    """与 `deploy/scripts/check_prompt_versions.sh` 同一判据，在 pytest 里再守一道。"""
    import json

    from backend.core.config import Settings
    from backend.harness.prompts import LOCK_FILENAME

    registry = PromptRegistry.load()
    lock_path = Settings(_env_file=None).PROMPTS_DIR / LOCK_FILENAME  # type: ignore[call-arg]
    problems = registry.diff_lock(json.loads(lock_path.read_text("utf-8")))
    assert problems == ()
