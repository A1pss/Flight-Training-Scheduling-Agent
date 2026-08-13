"""Harness 测试夹具：装一个「除了模型是假的、其它都是真的」的 Harness。

**工具实现分属别的里程碑**（检索是 M5、Planner 是 M4-B、诊断接 M2-A 的
`solver/diagnose.py`），所以这里给的是**完整的测试替身**——CLAUDE.md 铁律 1
允许的那种：接口已定稿（`ToolSpec` + `ToolHandler`），替身覆盖接口的全部形态。

替身刻意做成**确定性纯函数**：同参数同结果。缓存、重放、可复现性三条断言都
建立在这个前提上，替身自己先飘的话，测出来的绿是假的。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from backend.core.config import Settings
from backend.harness.cache import InMemoryCacheBackend, ToolResultCache
from backend.harness.context import ContextAssembler
from backend.harness.harness import Harness
from backend.harness.mode_selector import ModeSelector
from backend.harness.prompts import PromptRegistry
from backend.harness.recorder import TraceRecorder
from backend.harness.registry import ToolRegistry
from backend.harness.validation import StaticEntityIndex
from backend.llm.mock import MockProvider
from backend.llm.types import LLMResponse

#: 基准周实体（v6 §1.3）。**只用于测试**：生产端的索引从快照装配。
BASELINE_PERSONS = ("P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08")
BASELINE_AIRCRAFT = ("AC10", "AC27", "AC34", "AC49", "AC61", "AC73", "AC84", "AC95")
BASELINE_MISSIONS = (
    "missionA-1",
    "missionA-2",
    "missionB-1",
    "missionB-2",
    "missionC-1",
    "missionC-2",
    "missionD-1",
    "missionE-1",
    "missionE-2",
    "missionF-1",
    "missionG-1",
    "missionH-1",
)
BASELINE_WEEKS = ("2026W02", "2026W03")


def baseline_entity_index() -> StaticEntityIndex:
    return StaticEntityIndex(
        {
            "person": BASELINE_PERSONS,
            "aircraft": BASELINE_AIRCRAFT,
            "mission": BASELINE_MISSIONS,
            "week": BASELINE_WEEKS,
        }
    )


# ─────────────────────────────────────────────────────────────────────
# 工具替身
# ─────────────────────────────────────────────────────────────────────

_NAME_TO_ID = {
    "孙军": "P01",
    "王强": "P02",
    "吴鹏": "P03",
    "刘斌": "P04",
    "张勇": "P05",
    "陈伟": "P06",
    "高超": "P07",
    "何超": "P08",
}


class CountingHandler:
    """记调用次数的替身——缓存命中率靠它验（命中时次数不涨）。"""

    def __init__(self, fn: Any) -> None:
        self._fn = fn
        self.calls = 0

    def __call__(self, arguments: dict[str, Any]) -> Any:
        self.calls += 1
        return self._fn(arguments)


def stub_handlers() -> dict[str, CountingHandler]:
    """一组确定性替身，覆盖各组件 ACL 行里的代表性工具。"""
    return {
        "resolve_person": CountingHandler(
            lambda a: {"person_id": _NAME_TO_ID.get(a["surface"], ""), "surface": a["surface"]}
        ),
        "resolve_aircraft": CountingHandler(
            lambda a: {"aircraft_id": f"AC{a['surface'].strip('号机 ')}"}
        ),
        "resolve_week": CountingHandler(lambda a: {"iso_week": "2026W02", "surface": a["surface"]}),
        "ask_user": CountingHandler(
            lambda a: {"asked": a["question"], "resolution": a["resolution"]}
        ),
        "escalate": CountingHandler(lambda a: {"escalated": a["reason"]}),
        "estimate_scope": CountingHandler(
            lambda a: {"candidates": 2276, "iso_week": a["iso_week"]}
        ),
        "propose_solve_intent": CountingHandler(
            lambda a: {"accepted": True, "week": a["iso_week"]}
        ),
        "check_authority": CountingHandler(
            lambda a: {"granted": a["actor_role"] == "训练主任", "tier": a["requested_tier"]}
        ),
        "classify_doc": CountingHandler(lambda a: {"kind": "personnel", "file": a["filename"]}),
        "prereq_cte": CountingHandler(
            lambda a: {
                "person_id": a["person_id"],
                "mission_id": a["mission_id"],
                "eligible": False,
            }
        ),
        "sql_query": CountingHandler(lambda a: {"rows": [], "sql": a["sql"]}),
        "memory.search": CountingHandler(lambda a: {"hits": [], "query": a["query"]}),
        "memory.write": CountingHandler(lambda a: {"written": a["key"]}),
        "min_conflict_set": CountingHandler(lambda a: {"conflicts": ["约束3", "约束11"], **a}),
        "probe_solve": CountingHandler(lambda a: {"status": "INFEASIBLE", **a}),
        "verify_claim": CountingHandler(lambda a: {"supported": True, "claim": a["claim"]}),
        "compose_report": CountingHandler(lambda a: {"sections": len(a["sections"])}),
    }


def registry_with_test_handlers() -> tuple[ToolRegistry, dict[str, CountingHandler]]:
    registry = ToolRegistry()
    handlers = stub_handlers()
    registry.register_many(handlers)
    return registry, handlers


# ─────────────────────────────────────────────────────────────────────
# Harness 装配
# ─────────────────────────────────────────────────────────────────────


def harness_settings(tmp_path: Path | None = None, **overrides: Any) -> Settings:
    """一份不读 `.env` 的配置。"""
    values: dict[str, Any] = {"LLM_PROVIDER": "mock", "APP_ENV": "ci"}
    if tmp_path is not None:
        values["TRACES_DIR"] = tmp_path / "traces"
        values["MOCK_FIXTURE_DIR"] = tmp_path / "stubs"
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def build_harness(
    responses: Sequence[LLMResponse | str] = (),
    *,
    settings: Settings | None = None,
    trace_root: Path | None = None,
    entity_index: StaticEntityIndex | None = None,
    with_entities: bool = True,
    **harness_kwargs: Any,
) -> tuple[Harness, MockProvider, dict[str, CountingHandler]]:
    """装一个 mock provider + 真 ACL + 真校验 + 真预算的 Harness。"""
    cfg = settings or harness_settings()
    provider = MockProvider(cfg)
    if responses:
        provider.register_scenario("default", responses)
        provider.activate("default")

    registry, handlers = registry_with_test_handlers()
    index = entity_index or (baseline_entity_index() if with_entities else None)
    harness = Harness(
        provider,
        registry=registry,
        assembler=ContextAssembler(settings=cfg),
        cache=ToolResultCache(InMemoryCacheBackend(), settings=cfg),
        mode_selector=ModeSelector.from_settings(cfg),
        prompts=PromptRegistry.load(settings=cfg),
        entity_index=index,
        recorder=TraceRecorder(
            "trace_test",
            root=trace_root,
            provider="mock",
            model=cfg.LLM_MODEL,
            snapshot_id="snap_test",
        ),
        settings=cfg,
        trace_id="trace_test",
        snapshot_id="snap_test",
        **harness_kwargs,
    )
    return harness, provider, handlers


__all__ = [
    "BASELINE_AIRCRAFT",
    "BASELINE_MISSIONS",
    "BASELINE_PERSONS",
    "BASELINE_WEEKS",
    "CountingHandler",
    "baseline_entity_index",
    "build_harness",
    "harness_settings",
    "registry_with_test_handlers",
    "stub_handlers",
]
