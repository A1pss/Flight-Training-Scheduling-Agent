"""M7 第一步的评测器单测：渲染、三层 runner、指标聚合、提示词 token 差分。

**全程零真机调用**：valid 层用 `MockProvider` 的场景桩喂固定输出，
两个确定性层本来就不调模型。跑得动 CI，也跑得动没有 GPU 的机器。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from backend.core.errors import ErrorCode
from backend.datasets.loader import load_eval_dataset
from backend.harness.acl import ACL_MATRIX
from backend.harness.prompts import PromptRegistry
from backend.harness.tools import TOOL_CATALOG
from backend.harness.types import ALL_COMPONENTS
from backend.llm.mock import MockProvider, text_response, tool_response
from backend.llm.types import LLMResponse
from backend.training import cli, metrics, prompt_configs, prompt_tokens, rendering, toolcall_eval

# ─────────────────────────────────────────────────────────────────────
# 渲染
# ─────────────────────────────────────────────────────────────────────


def test_every_field_has_a_role() -> None:
    """工具目录里每个字段都要有中文角色短语。

    漏一个，渲染就会**露出字段名**（`changed_aircraft：AC84`），等于把
    「业务语义 → 契约字段」这一步直接抄给模型，一次通过率随之虚高。
    新增工具时这条会先红。
    """
    assert rendering.missing_roles() == ()


def test_render_puts_values_in_and_keeps_field_names_out() -> None:
    text = rendering.render_task(
        "assess_disruption", {"iso_week": "2026W04", "changed_aircraft": ["AC84"]}
    )
    assert "2026W04" in text
    assert "AC84" in text
    assert "changed_aircraft" not in text
    assert "iso_week" not in text


def test_render_trims_the_schema_aside_from_the_role_phrase() -> None:
    """`description` 里给模型的附注（分号之后那半句）不进任务陈述。"""
    assert rendering.role_of("assess_disruption", "baseline_plan_id") == "对比基线方案 ID"


def test_render_falls_back_through_three_levels() -> None:
    assert rendering.role_of("min_conflict_set", "scope_persons") == "限定在哪些人身上找冲突"
    assert rendering.role_of("min_conflict_set", "iso_week") == "目标周"
    assert rendering.role_of("no_such_tool", "whatever") == "whatever"


def test_render_expands_nested_objects_instead_of_dumping_json() -> None:
    text = rendering.render_task(
        "propose_solve_intent", {"intent": {"iso_week": "2026W02", "n": 3}}
    )
    assert "iso_week" in text  # 嵌套对象逐层展开，键名照留（模型要照契约重建）
    assert '{"iso_week"' not in text


def test_render_handles_empty_params() -> None:
    assert "（无额外条件）" in rendering.render_task("escalate", {})


def test_params_match_ignores_key_order_only() -> None:
    assert rendering.params_match({"a": 1, "b": 2}, {"b": 2, "a": 1})
    assert not rendering.params_match({"a": 1}, {"a": 1, "b": 2})
    assert not rendering.params_match({"a": 1}, {"a": "1"})


def test_render_batch_is_order_preserving() -> None:
    items = [
        {"tool": "resolve_person", "expected_params": {"surface": "何超"}},
        {"tool": "resolve_aircraft", "expected_params": {"surface": "AC73"}},
    ]
    rendered = rendering.render_batch(items)
    assert len(rendered) == 2
    assert "何超" in rendered[0]
    assert "AC73" in rendered[1]


# ─────────────────────────────────────────────────────────────────────
# 三种提示词配置
# ─────────────────────────────────────────────────────────────────────


def test_zero_shot_carries_no_project_knowledge() -> None:
    """下界参照必须真的是下界：不许漏进任何本项目的编号规则或禁令。"""
    body = prompt_configs.system_prompt_of("zero_shot", "route")
    assert body == prompt_configs.ZERO_SHOT_SYSTEM
    for leak in ("person_id", "AC", "missionA", "排班", "求解器"):
        assert leak not in body


def test_production_is_the_untouched_repo_prompt() -> None:
    for component in ALL_COMPONENTS:
        assert (
            prompt_configs.system_prompt_of("production", component)
            == PromptRegistry.load().get(component).body
        )


def test_optimized_is_production_plus_the_suffix() -> None:
    base = prompt_configs.system_prompt_of("production", "planner")
    tuned = prompt_configs.system_prompt_of("optimized", "planner")
    assert tuned.startswith(base)
    assert prompt_configs.OPTIMIZED_SUFFIX in tuned
    assert len(tuned) > len(base)


def test_prompt_version_is_never_rewritten() -> None:
    """版本号形态是 `^v\\d+$`，锁文件与 trace 都按它解析 —— 消融配置不许动它。"""
    for config in prompt_configs.ALL_PROMPT_CONFIGS:
        prompt = prompt_configs.registry_for(config).get("route")
        assert prompt.prompt_version == PromptRegistry.load().get("route").prompt_version
        assert config in prompt.description


def test_every_config_covers_every_component() -> None:
    for config in prompt_configs.ALL_PROMPT_CONFIGS:
        assert prompt_configs.registry_for(config).missing_components() == ()


# ─────────────────────────────────────────────────────────────────────
# valid 层 runner
# ─────────────────────────────────────────────────────────────────────


def _valid_item(tool: str = "resolve_person", **params: Any) -> dict[str, Any]:
    component = next(c for c in sorted(ACL_MATRIX) if tool in ACL_MATRIX[c])
    return {
        "item_id": "TOOL-VAL-001",
        "stratum": "valid",
        "component": component,
        "tool": tool,
        "expected_params": params or {"surface": "何超"},
        "expected_error_code": None,
    }


def _scripted(*steps: LLMResponse) -> MockProvider:
    provider = MockProvider()
    provider.register_scenario("s", list(steps))
    provider.activate("s")
    return provider


def _run_valid(item: dict[str, Any], provider: MockProvider) -> toolcall_eval.ToolCallOutcome:
    return toolcall_eval.run_valid_item(
        item,
        prompts=prompt_configs.registry_for("production"),
        config="production",
        round_index=0,
        registry=toolcall_eval._stub_registry(),
        provider=provider,
    )


def test_valid_item_first_pass() -> None:
    out = _run_valid(_valid_item(), _scripted(tool_response("resolve_person", {"surface": "何超"})))
    assert out.first_pass
    assert out.final_pass
    assert out.tool_correct
    assert out.params_exact
    assert out.llm_calls == 1
    assert out.first_failure_modes == ()


def test_valid_item_counts_a_retry_as_not_first_pass() -> None:
    """第一次填错、回灌后改对 —— 最终通过但**不算一次通过**。"""
    out = _run_valid(
        _valid_item(),
        _scripted(
            tool_response("resolve_person", {"surface": ""}),  # 空串 → missing_field
            tool_response("resolve_person", {"surface": "何超"}),
        ),
    )
    assert not out.first_pass
    assert out.final_pass
    assert out.llm_calls == 2
    assert out.first_failure_modes == ("missing_field",)


def test_valid_item_classifies_entity_hallucination() -> None:
    """编号格式合法、基准实体表里没有 —— 这是硬地板 x 的观测口，不能归到别的桶。

    用 `prereq_cte` 而不是随便挑一个工具：**只有带 `x-entity` 标注的字段**才会
    走成员校验（目前是 `prereq_cte` / `blame_chain` 的 person/mission 两组）。
    挑一个没标注的字段来测，测的是「校验器没管这个字段」，不是分类对不对。
    """
    fake = tool_response("prereq_cte", {"person_id": "P99", "mission_id": "missionC-1"})
    out = _run_valid(
        _valid_item("prereq_cte", person_id="P04", mission_id="missionC-1"),
        _scripted(fake, fake, fake),
    )
    assert out.first_failure_modes == ("entity_hallucination",)
    assert out.degraded


def test_valid_item_degrades_after_two_retries() -> None:
    bad = tool_response("resolve_person", {"surface": ""})
    out = _run_valid(_valid_item(), _scripted(bad, bad, bad))
    assert out.degraded
    assert not out.final_pass
    assert out.llm_calls == 3  # 首次 + 2 次重试，§7.7.1 的上限
    assert out.error_code == ErrorCode.LLM_SCHEMA_VIOLATION.value


def test_valid_item_marks_wrong_tool_as_selection_miss_but_still_contract_pass() -> None:
    """选错工具但参数合法 —— §12.5.1 的口径下**契约是过的**，只有诊断指标记为错。

    两个数分开报正是为了这件事：一次通过率不惩罚选错工具，
    看不出选错就得靠 `tool_selection_rate`。
    """
    item = _valid_item("resolve_person")
    out = _run_valid(item, _scripted(tool_response("resolve_week", {"surface": "下周"})))
    assert out.first_pass
    assert not out.tool_correct
    assert not out.params_exact


def test_valid_item_records_runtime_failure_without_counting_it() -> None:
    """模型侧不可用的那一条要留痕，但**不进分母**（否则运维事故记成能力下降）。"""
    provider = MockProvider()
    provider.register_scenario("empty", [])
    provider.activate("empty")
    out = _run_valid(_valid_item(), provider)
    assert not out.ok
    assert "LLMUnavailableError" in out.error
    assert out.first_pass is False


def test_valid_item_counts_a_model_side_acl_attempt_instead_of_crashing() -> None:
    """**模型自己点了 ACL 行之外的工具** —— 计一次数，不崩。

    口径 B 实测撞出来的：Diagnosis 拿不到已知条件，就去点了 `resolve_person`。
    Harness 按 §7.7.2 抛是对的；runner 把它记成一次「首次未通过 + 越权尝试」，
    因为它既是模型行为（要计数），也是 §12.5.1「越权拦截率 100%」的真实样本。

    **本窗口一开始把它写成「当场炸」，于是口径 B 跑到第 89 条崩了。**
    """
    item = _valid_item("min_conflict_set", iso_week="2026W02")
    assert item["component"] == "diagnosis"
    out = _run_valid(item, _scripted(tool_response("resolve_person", {"surface": "何超"})))
    assert out.acl_attempt
    assert not out.first_pass
    assert out.error_code == "FTS-4004"
    assert out.ok  # 不是运行时事故，照常进分母


def test_dataset_level_acl_drift_is_caught_at_load_time_not_here() -> None:
    """数据集自己越权是**加载期**的事，runner 不该重复判 —— 判错对象正是上面那个 bug。"""
    from backend.datasets.schemas import ToolCallItem

    with pytest.raises(ValidationError, match="valid 层却越权"):
        ToolCallItem.model_validate(
            {
                "item_id": "TOOL-VAL-001",
                "stratum": "valid",
                "component": "diagnosis",
                "tool": "resolve_person",
                "tool_exists": True,
                "prompt_context": "构造一条数据集自己越权的条目",
                "expected_params": {"surface": "何超"},
                "expectation": "accept",
                "expected_error_code": None,
                "rationale": "越权对不该出现在 valid 层",
            }
        )


def test_acl_attempts_are_counted_apart_from_the_five_contract_modes() -> None:
    """越权是权限失败，不是契约失败 —— 混进五类分布表，§15.2 ⑥ 会照着错表挑样。"""
    rows = [
        _outcome(item_id="a", acl_attempt=True),
        _outcome(item_id="b", first_pass=True),
    ]
    m = metrics.valid_metrics(rows, "production")
    assert m.acl_attempts == 1
    assert m.acl_attempt_rate == 0.5
    assert sum(m.first_failure_modes.values()) == 0


# ─────────────────────────────────────────────────────────────────────
# 两个确定性层
# ─────────────────────────────────────────────────────────────────────


def test_acl_layer_intercepts_a_forbidden_pair() -> None:
    pair = next(
        (c, t) for c in sorted(ACL_MATRIX) for t in sorted(TOOL_CATALOG) if t not in ACL_MATRIX[c]
    )
    item = {
        "item_id": "TOOL-ACL-001",
        "stratum": "acl_violation",
        "component": pair[0],
        "tool": pair[1],
        "expected_error_code": "FTS-4004",
    }
    out = toolcall_eval.run_acl_item(item, config="production", round_index=0)
    assert out.intercepted
    assert out.error_code == "FTS-4004"


def test_acl_layer_intercepts_an_invented_deterministic_node() -> None:
    """凭空编出来的确定性节点名走的是另一条拦截（架构禁令），同码不同档。"""
    item = {
        "item_id": "TOOL-ACL-025",
        "stratum": "acl_violation",
        "component": "planner",
        "tool": "commit_plan",
        "expected_error_code": "FTS-4004",
    }
    out = toolcall_eval.run_acl_item(item, config="production", round_index=0)
    assert out.intercepted
    assert out.error_code == "FTS-4004"


def test_budget_layer_trips_the_harness_pool() -> None:
    item = {
        "item_id": "TOOL-BGT-001",
        "stratum": "budget_exhaustion",
        "component": "route",
        "tool": "resolve_person",
        "expected_params": {"surface": "何超"},
        "expected_error_code": "FTS-4003",
    }
    out = toolcall_eval.run_budget_item(
        item,
        prompts=prompt_configs.registry_for("production"),
        config="production",
        round_index=0,
        registry=toolcall_eval._stub_registry(),
    )
    assert out.intercepted
    assert out.error_code == "FTS-4003"


def test_budget_layer_probe_pool_does_not_raise() -> None:
    """探针池是**另一个池、另一种行为**：耗尽不抛错，所以没有错误码。"""
    item = {
        "item_id": "TOOL-BGT-025",
        "stratum": "budget_exhaustion",
        "component": "diagnosis",
        "tool": "probe_solve",
        "expected_params": {},
        "expected_error_code": None,
    }
    out = toolcall_eval.run_budget_item(
        item,
        prompts=prompt_configs.registry_for("production"),
        config="production",
        round_index=0,
        registry=toolcall_eval._stub_registry(),
    )
    assert out.intercepted
    assert out.error_code == ""
    assert out.expected_error_code is None


# ─────────────────────────────────────────────────────────────────────
# 指标聚合
# ─────────────────────────────────────────────────────────────────────


def _outcome(**kwargs: Any) -> toolcall_eval.ToolCallOutcome:
    base: dict[str, Any] = {
        "config": "production",
        "round_index": 0,
        "item_id": "TOOL-VAL-001",
        "stratum": "valid",
        "component": "route",
        "tool": "resolve_person",
    }
    return toolcall_eval.ToolCallOutcome(**{**base, **kwargs})


def test_metrics_exclude_errored_rows_from_the_denominator() -> None:
    rows = [
        _outcome(item_id="a", first_pass=True, final_pass=True, llm_calls=1),
        _outcome(item_id="b", first_pass=False, final_pass=True, llm_calls=2),
        _outcome(item_id="c", error="LLMUnavailableError: 掉线了"),
    ]
    m = metrics.valid_metrics(rows, "production")
    assert m.calls == 2
    assert m.errored == 1
    assert m.first_pass_rate == 0.5
    assert m.retry_coefficient == 1.5


def test_metrics_split_first_attempt_and_all_attempt_failures() -> None:
    rows = [
        _outcome(
            first_failure_modes=("missing_field",),
            all_failure_modes=("missing_field", "type_error"),
        )
    ]
    m = metrics.valid_metrics(rows, "production")
    assert m.first_failure_modes["missing_field"] == 1
    assert m.first_failure_modes["type_error"] == 0
    assert m.all_failure_modes["type_error"] == 1


def test_metrics_report_every_failure_mode_column_even_at_zero() -> None:
    """五列固定 —— 缺列的表跨配置对不齐，肉眼比对就废了。"""
    m = metrics.valid_metrics([_outcome(first_pass=True)], "production")
    assert tuple(m.first_failure_modes) == metrics.FAILURE_MODE_ORDER


def test_metrics_group_by_round_component_and_tool() -> None:
    rows = [
        _outcome(round_index=0, component="route", tool="resolve_person", first_pass=True),
        _outcome(round_index=1, component="route", tool="resolve_person", first_pass=False),
        _outcome(round_index=0, component="planner", tool="estimate_scope", first_pass=True),
    ]
    m = metrics.valid_metrics(rows, "production")
    assert m.per_round_first_pass == {"0": 1.0, "1": 0.0}
    assert m.per_component_first_pass["planner"] == 1.0
    assert m.per_tool_first_pass["resolve_person"] == 0.5


def test_worst_tools_lists_only_imperfect_ones_lowest_first() -> None:
    rows = [
        _outcome(tool="a", first_pass=True),
        _outcome(tool="b", first_pass=False),
        _outcome(tool="c", first_pass=False),
        _outcome(tool="c", first_pass=True),
    ]
    m = metrics.valid_metrics(rows, "production")
    assert metrics.worst_tools(m) == (("b", 0.0), ("c", 0.5))


def test_guardrail_metrics_require_the_expected_code() -> None:
    """拦住了但报了别的码不算对 —— 3004 与 4003 对用户是两种下一步。"""
    rows = [
        _outcome(
            stratum="acl_violation",
            intercepted=True,
            error_code="FTS-4004",
            expected_error_code="FTS-4004",
        ),
        _outcome(
            stratum="acl_violation",
            intercepted=True,
            error_code="FTS-4002",
            expected_error_code="FTS-4004",
        ),
    ]
    g = metrics.guardrail_metrics(rows, "production")
    assert g.acl_intercept_rate == 0.5


def test_metrics_on_an_empty_set_do_not_divide_by_zero() -> None:
    m = metrics.valid_metrics([], "production")
    assert m.calls == 0
    assert m.first_pass_rate == 0.0
    assert m.retry_coefficient == 0.0


# ─────────────────────────────────────────────────────────────────────
# 提示词 token 的三次差分
# ─────────────────────────────────────────────────────────────────────


class _CountingProvider:
    """按「system 长度 + schema 条数」编造 `prompt_tokens` 的假 provider。

    差分法本身是算术，用真机验证是浪费；这里要验的是**减对了没有**。
    """

    def __init__(self) -> None:
        self.seen: list[tuple[int, int]] = []

    def chat(self, request: Any) -> LLMResponse:
        system = next(m["content"] for m in request.messages if m["role"] == "system")
        self.seen.append((len(system), len(request.tools)))
        return LLMResponse(prompt_tokens=100 + len(system) + 10 * len(request.tools))

    def complete(self, messages: Any, **kwargs: Any) -> str:  # pragma: no cover —— 契约占位
        return ""


def test_prompt_token_differencing_splits_system_from_schema() -> None:
    provider = _CountingProvider()
    m = prompt_tokens.measure_component(provider, "production", "route")  # type: ignore[arg-type]
    body = prompt_configs.system_prompt_of("production", "route")
    tools = len(ACL_MATRIX["route"])
    assert m.system_tokens == len(body) - len(prompt_tokens.BLANK_SYSTEM)
    assert m.schema_tokens == 10 * tools
    assert m.gate_tokens == m.system_tokens + m.schema_tokens


def test_prompt_token_baseline_uses_a_blank_system_not_a_missing_one() -> None:
    """三次探针都要带 system 消息 —— 少给一次，Qwen 模板会自己塞一句默认的进去。"""
    provider = _CountingProvider()
    prompt_tokens.measure_component(provider, "production", "route")  # type: ignore[arg-type]
    assert len(provider.seen) == 3
    assert all(length > 0 for length, _ in provider.seen)
    assert [tools for _, tools in provider.seen] == [6, 6, 0]


def test_weighted_gate_tokens_follows_the_item_counts() -> None:
    ms = [
        prompt_tokens.PromptTokenMeasurement(
            config="production", component="route", pe_full=300, pe_nosys=200, pe_notools=100
        ),
        prompt_tokens.PromptTokenMeasurement(
            config="production", component="planner", pe_full=900, pe_nosys=500, pe_notools=100
        ),
    ]
    # route 1 条、planner 3 条 → (200×1 + 800×3) / 4 = 650
    assert prompt_tokens.weighted_gate_tokens(ms, {"route": 1, "planner": 3}) == 650.0
    assert prompt_tokens.weighted_gate_tokens(ms, {}) == 0.0


def test_component_weights_count_only_the_valid_stratum() -> None:
    items = [
        {"stratum": "valid", "component": "route"},
        {"stratum": "valid", "component": "route"},
        {"stratum": "acl_violation", "component": "route"},
    ]
    assert prompt_tokens.component_weights(items) == {"route": 2}


# ─────────────────────────────────────────────────────────────────────
# 落盘与断点续跑
# ─────────────────────────────────────────────────────────────────────


def test_outcomes_round_trip_through_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "out.jsonl"
    rows = [_outcome(item_id="a", first_pass=True), _outcome(item_id="b")]
    path.write_text("\n".join(r.model_dump_json() for r in rows) + "\n", encoding="utf-8")
    assert [o.item_id for o in toolcall_eval.load_outcomes(path)] == ["a", "b"]
    assert toolcall_eval.load_outcomes(tmp_path / "missing.jsonl") == ()


def test_resume_skips_what_is_already_on_disk(tmp_path: Path) -> None:
    """中断后原样重跑要能续上 —— 200×3×3 在降级显存下跑一次就是几个小时。"""
    path = tmp_path / "out.jsonl"
    path.write_text(_outcome(item_id="a").model_dump_json() + "\n", encoding="utf-8")
    assert toolcall_eval._done_keys(path) == {("production", "task", 0, "a")}
    assert toolcall_eval._done_keys(tmp_path / "missing.jsonl") == set()


def test_stub_registry_binds_every_tool() -> None:
    """少绑一个，执行器会抛 `ToolNotBoundError`，把「模型答对了」记成崩溃。"""
    registry = toolcall_eval._stub_registry()
    assert set(registry.bound_names()) == set(TOOL_CATALOG)


def test_entity_index_covers_the_four_indexed_kinds() -> None:
    """没有这份索引，`entity_hallucination` 一条都统计不到（硬地板 x 就没了）。"""
    index = toolcall_eval._entity_index()
    assert "P04" in index.known("person")
    assert "AC73" in index.known("aircraft")
    assert "missionC-1" in index.known("mission")
    assert "RWY-2" in index.known("runway")
    assert index.known("airspace") == frozenset()  # 空域编号由上传数据决定，不设索引


def test_mock_text_response_is_not_mistaken_for_a_tool_call() -> None:
    """模型光说话不调工具 —— 要求必须调工具时判 `json_malformed`，不是「通过」。"""
    chatter = text_response("我先解释一下……")
    out = _run_valid(_valid_item(), _scripted(chatter, chatter, chatter))
    assert not out.first_pass
    assert "json_malformed" in out.first_failure_modes
    assert out.degraded


def test_dataset_items_render_without_leaking_field_names() -> None:
    """拿真数据集扫一遍：260 条渲染出来都不许出现契约字段名。"""
    raw = Path("datasets/tool_calls_200/v1/items.jsonl").read_text(encoding="utf-8")
    leaked: list[str] = []
    for line in raw.splitlines():
        item = json.loads(line)
        if item["stratum"] != "valid":
            continue
        text = rendering.render_item(item)
        leaked.extend(
            f"{item['item_id']}:{field}"
            for field in (item.get("expected_params") or {})
            if f"{field}：" in text
        )
    assert leaked == []


# ─────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────


def test_cli_report_renders_every_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`report` 子命令要把四节都渲染出来，且不调模型。"""
    monkeypatch.setattr(cli, "RESULT_DIR", tmp_path)
    rows = [
        _outcome(item_id="a", first_pass=True, final_pass=True, llm_calls=1, tool="resolve_person"),
        _outcome(
            item_id="b",
            first_pass=False,
            final_pass=True,
            llm_calls=2,
            tool="sql_query",
            first_failure_modes=("type_error",),
            all_failure_modes=("type_error",),
        ),
        _outcome(
            item_id="c",
            stratum="acl_violation",
            intercepted=True,
            error_code="FTS-4004",
            expected_error_code="FTS-4004",
        ),
    ]
    (tmp_path / "toolcall_production.jsonl").write_text(
        "\n".join(r.model_dump_json() for r in rows) + "\n", encoding="utf-8"
    )

    out = tmp_path / "baseline.md"
    assert cli.main(["report", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "一、三种配置的主指标" in text
    assert "二、失败模式分布" in text
    assert "三、确定性两层" in text
    assert "四、一次通过率最低的工具" in text
    assert "50.0%" in text  # 一次通过率 1/2
    assert "`sql_query` 0.0%" in text


def test_cli_report_refuses_when_there_is_nothing_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有结果文件就退非零 —— 渲染一张空表比报错更容易被当成「跑过了」。"""
    monkeypatch.setattr(cli, "RESULT_DIR", tmp_path)
    assert cli.main(["report", "--out", str(tmp_path / "x.md")]) == 1


def test_cli_rejects_an_unknown_config() -> None:
    with pytest.raises(SystemExit):
        cli.main(["toolcall", "--config", "no_such_config"])


def test_cli_paths_are_per_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "RESULT_DIR", tmp_path)
    assert cli.outcome_path("production") != cli.outcome_path("zero_shot")
    assert cli.outcome_path("production").parent == tmp_path


def test_run_config_writes_incrementally_and_resumes(tmp_path: Path) -> None:
    """批跑循环 + 断点续跑：越权层不调模型，用它跑通整条落盘路径。"""
    out = tmp_path / "acl.jsonl"
    first = toolcall_eval.run_config(
        "production", out_path=out, rounds=1, limit=3, strata=("acl_violation",)
    )
    assert first == 3
    assert len(toolcall_eval.load_outcomes(out)) == 3

    # 原样再跑一次：一条都不该重写
    assert (
        toolcall_eval.run_config(
            "production", out_path=out, rounds=1, limit=3, strata=("acl_violation",)
        )
        == 0
    )
    assert len(toolcall_eval.load_outcomes(out)) == 3

    # 加一轮：只补新增的那一轮
    assert (
        toolcall_eval.run_config(
            "production", out_path=out, rounds=2, limit=3, strata=("acl_violation",)
        )
        == 3
    )
    assert {o.round_index for o in toolcall_eval.load_outcomes(out)} == {0, 1}


def test_run_config_filters_by_stratum() -> None:
    """`--strata` 真的在过滤 —— 不然「只跑越权层」会顺带把 200 条 valid 也跑了。"""
    _manifest, items = load_eval_dataset(toolcall_eval.DATASET, require_approved=True)
    assert {i.stratum for i in items} == {"valid", "acl_violation", "budget_exhaustion"}


# ─────────────────────────────────────────────────────────────────────
# 口径 B（`context`）
# ─────────────────────────────────────────────────────────────────────


def test_context_rendering_uses_the_dataset_field_verbatim() -> None:
    item = {
        "tool": "assess_disruption",
        "prompt_context": "planner 组件需要「评估相对基线方案的影响面」，第 1 个变体",
        "expected_params": {"iso_week": "2026W04"},
    }
    assert rendering.render_item(item, "context") == item["prompt_context"]


def test_context_rendering_leaks_no_expected_value() -> None:
    """口径 B 的全部意义：`expected_params` 的取值**一个都不能**出现在提示词里。

    漏进去一个，这一组就不再是「模型自己产出参数」的那一侧，
    §15.2 ⑥ 要的失败模式分布也就还是测不出来。
    """
    leaked: list[str] = []
    for line in (
        Path("datasets/tool_calls_200/v1/items.jsonl").read_text(encoding="utf-8").splitlines()
    ):
        item = json.loads(line)
        if item["stratum"] != "valid":
            continue
        text = rendering.render_item(item, "context")
        for value in (item.get("expected_params") or {}).values():
            if isinstance(value, str) and len(value) >= 4 and value in text:
                leaked.append(f"{item['item_id']}:{value}")
    assert leaked == []


def test_two_renderings_differ_on_every_valid_item() -> None:
    items = [
        {
            "tool": "resolve_person",
            "prompt_context": "route 组件需要「把人员表述解析为 person_id」，第 1 个变体",
            "expected_params": {"surface": "何超"},
        }
    ]
    a, b = rendering.render_batch(items, "task")[0], rendering.render_batch(items, "context")[0]
    assert a != b
    assert "何超" in a
    assert "何超" not in b


def test_outcome_defaults_to_task_so_old_rows_still_load(tmp_path: Path) -> None:
    """口径 B 之前写下的行没有 `rendering` 字段 —— 必须照常读得回来。

    读不回来 = 那两个小时的 A 组结果作废重跑。
    """
    path = tmp_path / "old.jsonl"
    legacy = {
        "config": "production",
        "round_index": 0,
        "item_id": "TOOL-VAL-001",
        "stratum": "valid",
        "component": "route",
        "tool": "resolve_person",
        "first_pass": True,
    }
    path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    loaded = toolcall_eval.load_outcomes(path)
    assert loaded[0].rendering == "task"
    assert toolcall_eval._done_keys(path) == {("production", "task", 0, "TOOL-VAL-001")}


def test_metrics_never_mix_the_two_renderings() -> None:
    rows = [
        _outcome(item_id="a", rendering="task", first_pass=True),
        _outcome(item_id="a", rendering="context", first_pass=False),
    ]
    assert metrics.valid_metrics(rows, "production", "task").first_pass_rate == 1.0
    assert metrics.valid_metrics(rows, "production", "context").first_pass_rate == 0.0
    assert metrics.valid_metrics(rows, "production", "task").calls == 1


def test_guardrail_metrics_never_double_count_across_renderings() -> None:
    """两种口径各写了一份确定性层 —— 不按口径过滤，拦截率的分母会凭空翻倍。"""
    rows = [
        _outcome(
            item_id="x",
            stratum="acl_violation",
            rendering=r,
            intercepted=True,
            error_code="FTS-4004",
            expected_error_code="FTS-4004",
        )
        for r in ("task", "context")
    ]
    assert metrics.guardrail_metrics(rows, "production", "task").acl_total == 1


def test_outcome_paths_separate_the_two_renderings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "RESULT_DIR", tmp_path)
    assert cli.outcome_path("production", "task").name == "toolcall_production.jsonl"
    assert cli.outcome_path("production", "context").name == "toolcall_production__context.jsonl"


def test_run_config_records_the_rendering_it_ran(tmp_path: Path) -> None:
    out = tmp_path / "b.jsonl"
    toolcall_eval.run_config(
        "production",
        out_path=out,
        rounds=1,
        limit=2,
        strata=("acl_violation",),
        rendering="context",
    )
    assert {o.rendering for o in toolcall_eval.load_outcomes(out)} == {"context"}
