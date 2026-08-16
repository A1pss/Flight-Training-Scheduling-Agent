"""双模式选择、结果缓存、提示词版本治理（v6 §7.7.1 第 2、6、8 行）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.config import Settings
from backend.core.errors import RuleParseError
from backend.harness.cache import (
    InMemoryCacheBackend,
    ToolResultCache,
    cache_key,
)
from backend.harness.mode_selector import ModeSelector
from backend.harness.prompts import LOCK_FILENAME, PromptRegistry, parse_prompt
from backend.harness.tools import TOOL_CATALOG
from backend.harness.types import ALL_COMPONENTS

# ─────────────────────────────────────────────────────────────────────
# mode_selector：模式由统计驱动，不写死在配置里
# ─────────────────────────────────────────────────────────────────────


def test_starts_native() -> None:
    assert ModeSelector().pick("planner") == "native"


def test_no_switch_before_min_samples() -> None:
    """样本不够就不动——两次失败不能代表一个组件的失败率。"""
    selector = ModeSelector(min_samples=5, switch_threshold=0.3)
    for _ in range(4):
        selector.report_failure("planner")
    assert selector.pick("planner") == "native"
    assert selector.stats("planner").window_size == 4


def test_switches_to_constrained_json_when_failure_rate_crosses_threshold() -> None:
    selector = ModeSelector(window=10, min_samples=5, switch_threshold=0.30)
    for _ in range(3):
        selector.report_success("planner")
    assert selector.pick("planner") == "native"
    for _ in range(2):
        selector.report_failure("planner")
    # 5 个样本里 2 个失败 = 40% ≥ 30%
    assert selector.pick("planner") == "constrained_json"
    stats = selector.stats("planner")
    assert stats.failure_rate == pytest.approx(0.4)
    assert stats.switched is True


def test_recovers_only_below_the_lower_threshold() -> None:
    """滞回：不是掉到切换阈值以下就切回去，要掉到恢复阈值以下。"""
    selector = ModeSelector(window=20, min_samples=5, switch_threshold=0.30, recover_threshold=0.10)
    for _ in range(2):
        selector.report_failure("route")
    for _ in range(3):
        selector.report_success("route")
    assert selector.pick("route") == "constrained_json"  # 40% → 切了

    # 补到 20 个样本，失败率 2/20 = 10% ≤ 恢复阈值 → 切回
    for _ in range(15):
        selector.report_success("route")
    assert selector.stats("route").failure_rate == pytest.approx(0.10)
    assert selector.pick("route") == "native"


def test_hysteresis_prevents_flapping() -> None:
    """失败率停在两阈值之间时模式必须**不动**。"""
    selector = ModeSelector(window=10, min_samples=5, switch_threshold=0.30, recover_threshold=0.10)
    for _ in range(3):
        selector.report_failure("explain")
    for _ in range(7):
        selector.report_success("explain")
    assert selector.stats("explain").failure_rate == pytest.approx(0.30)
    assert selector.pick("explain") == "constrained_json"  # 切过去了

    selector.report_success("explain")  # 窗口滑走一个失败 → 20%，仍在两阈值之间
    assert selector.pick("explain") == "constrained_json"  # 不翻回去


def test_window_is_bounded() -> None:
    selector = ModeSelector(window=5)
    for _ in range(20):
        selector.report_success("knowledge")
    assert selector.stats("knowledge").window_size == 5


def test_components_are_independent() -> None:
    selector = ModeSelector(window=10, min_samples=2, switch_threshold=0.5)
    selector.report_failure("planner")
    selector.report_failure("planner")
    assert selector.pick("planner") == "constrained_json"
    assert selector.pick("route") == "native"


def test_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValueError, match="阈值必须满足"):
        ModeSelector(switch_threshold=0.1, recover_threshold=0.5)


def test_reset() -> None:
    selector = ModeSelector(min_samples=1, switch_threshold=0.5)
    selector.report_failure("planner")
    selector.reset("planner")
    assert selector.pick("planner") == "native"


def test_from_settings_reads_thresholds_not_mode() -> None:
    cfg = Settings(_env_file=None, HARNESS_MODE_SWITCH_THRESHOLD=0.5)  # type: ignore[call-arg]
    selector = ModeSelector.from_settings(cfg)
    for _ in range(5):
        selector.report_failure("extract")
    assert selector.pick("extract") == "constrained_json"


# ─────────────────────────────────────────────────────────────────────
# 结果缓存
# ─────────────────────────────────────────────────────────────────────


def test_cache_key_includes_snapshot() -> None:
    a = cache_key("prereq_cte", {"person_id": "P08"}, "snap_a")
    b = cache_key("prereq_cte", {"person_id": "P08"}, "snap_b")
    assert a != b
    assert "snap_a" in a


def test_cache_key_ignores_dict_order() -> None:
    assert cache_key("t", {"a": 1, "b": 2}, "s") == cache_key("t", {"b": 2, "a": 1}, "s")


def test_deterministic_tool_is_cached() -> None:
    cache = ToolResultCache(InMemoryCacheBackend(), ttl_s=60)
    calls = {"n": 0}

    def run() -> dict[str, int]:
        calls["n"] += 1
        return {"value": 42}

    spec = TOOL_CATALOG["prereq_cte"]
    first = cache.get_or_exec(spec, {"person_id": "P08"}, "snap", run)
    second = cache.get_or_exec(spec, {"person_id": "P08"}, "snap", run)
    assert first.cached is False and second.cached is True
    assert second.value == {"value": 42}
    assert calls["n"] == 1
    assert (cache.hits, cache.misses) == (1, 1)


def test_non_deterministic_tool_is_never_cached() -> None:
    cache = ToolResultCache(InMemoryCacheBackend())
    calls = {"n": 0}

    def run() -> int:
        calls["n"] += 1
        return calls["n"]

    spec = TOOL_CATALOG["ask_user"]
    assert cache.get_or_exec(spec, {"question": "?"}, "snap", run).value == 1
    assert cache.get_or_exec(spec, {"question": "?"}, "snap", run).value == 2


def test_different_snapshot_misses() -> None:
    cache = ToolResultCache(InMemoryCacheBackend())
    spec = TOOL_CATALOG["prereq_cte"]
    cache.get_or_exec(spec, {"person_id": "P08"}, "snap_a", lambda: 1)
    result = cache.get_or_exec(spec, {"person_id": "P08"}, "snap_b", lambda: 2)
    assert result.cached is False and result.value == 2


def test_invalidate_snapshot_drops_everything_under_it() -> None:
    """TTL 绑定快照生命周期：快照失效 → 它名下的缓存一把清掉。"""
    cache = ToolResultCache(InMemoryCacheBackend())
    spec = TOOL_CATALOG["prereq_cte"]
    for pid in ("P05", "P06", "P08"):
        cache.get_or_exec(spec, {"person_id": pid}, "snap_a", lambda: 1)
    cache.get_or_exec(spec, {"person_id": "P08"}, "snap_b", lambda: 1)

    assert cache.invalidate_snapshot("snap_a") == 3
    assert cache.get_or_exec(spec, {"person_id": "P08"}, "snap_a", lambda: 9).cached is False
    assert cache.get_or_exec(spec, {"person_id": "P08"}, "snap_b", lambda: 9).cached is True


# ─────────────────────────────────────────────────────────────────────
# 提示词版本治理
# ─────────────────────────────────────────────────────────────────────

GOOD_PROMPT = """---
component: route
prompt_key: system
prompt_version: v2
description: 测试用
---
你是意图路由。
"""


def test_parse_prompt() -> None:
    prompt = parse_prompt(GOOD_PROMPT)
    assert prompt.ref == "route/system"
    assert prompt.versioned == "route/system@v2"
    assert prompt.body == "你是意图路由。"
    assert len(prompt.sha256) == 64


@pytest.mark.parametrize(
    "text",
    [
        "没有 frontmatter 的正文",
        "---\ncomponent: route\n---\n缺 prompt_version",
        "---\ncomponent: 不存在的组件\nprompt_key: system\nprompt_version: v1\n---\n正文",
        "---\ncomponent: route\nprompt_key: system\nprompt_version: 1.0\n---\n版本号形态不对",
        "---\n: : :\n---\n非法 YAML",
    ],
)
def test_parse_prompt_rejects_bad_files(text: str) -> None:
    with pytest.raises(RuleParseError):
        parse_prompt(text)


def test_repo_prompts_cover_all_six_components() -> None:
    """六个 LLM 组件各自都得有 system 提示词，否则该组件根本跑不起来。

    ⚠️ **断言的是「六个 system 都在」，不是「一共只有六份」**（M5 改）：
    一个组件可以有多个 prompt_key —— `knowledge` 就有三份
    （`system` 走 ReAct 循环、`rewrite` 做查询改写、`answer` 做带引用生成，
    v6 §6.5.2 的第 ① 与第 ④ 阶段）。锁文件的完整性由
    `test_repo_lockfile_is_in_sync` 管，不该由这条兼管。
    """
    registry = PromptRegistry.load()
    assert registry.missing_components() == ()
    assert {f"{c}/system" for c in ALL_COMPONENTS} <= set(registry.versions())


def test_knowledge_has_the_three_keys_the_retrieval_pipeline_needs() -> None:
    """M5：改写与生成各有各的提示词，与 ReAct 的 system 分开治理。"""
    refs = set(PromptRegistry.load().versions())
    assert {"knowledge/system", "knowledge/rewrite", "knowledge/answer"} <= refs


def test_repo_lockfile_is_in_sync() -> None:
    """锁文件与正文一致——正文改了没换版本号，这条会红（这正是它存在的意义）。"""
    registry = PromptRegistry.load()
    lock = json.loads((Settings(_env_file=None).PROMPTS_DIR / LOCK_FILENAME).read_text("utf-8"))  # type: ignore[call-arg]
    assert registry.diff_lock(lock) == ()


def test_diff_lock_catches_edit_without_version_bump(tmp_path: Path) -> None:
    (tmp_path / "route").mkdir()
    (tmp_path / "route" / "system.md").write_text(GOOD_PROMPT, encoding="utf-8")
    registry = PromptRegistry.load(tmp_path)
    stale = {"route/system": {"prompt_version": "v2", "sha256": "0" * 64}}
    problems = registry.diff_lock(stale)
    assert problems and "版本号必须递增" in problems[0]


def test_diff_lock_catches_new_prompt(tmp_path: Path) -> None:
    (tmp_path / "planner").mkdir()
    (tmp_path / "planner" / "system.md").write_text(
        GOOD_PROMPT.replace("component: route", "component: planner"), encoding="utf-8"
    )
    problems = PromptRegistry.load(tmp_path).diff_lock({})
    assert problems and "没写进锁文件" in problems[0]


def test_readme_is_not_loaded_as_a_prompt(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# 说明\n没有 frontmatter", encoding="utf-8")
    assert PromptRegistry.load(tmp_path).refs() == ()


def test_missing_prompt_raises_with_hint(tmp_path: Path) -> None:
    with pytest.raises(RuleParseError, match="不存在"):
        PromptRegistry.load(tmp_path).get("planner")


def test_duplicate_ref_is_rejected(tmp_path: Path) -> None:
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "system.md").write_text(GOOD_PROMPT, encoding="utf-8")
    with pytest.raises(RuleParseError, match="重复定义"):
        PromptRegistry.load(tmp_path)
