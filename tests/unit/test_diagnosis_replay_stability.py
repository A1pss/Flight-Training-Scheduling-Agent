"""Diagnosis Agent 的严格 record/replay 稳定性。"""

from types import SimpleNamespace

from backend.agents.diagnosis import _blocks, _prompt_budget, _tool_notes
from backend.harness import ValidatedCall
from backend.harness.types import ToolResult
from backend.solver.diagnose import ConflictCore, Diagnosis, ProbeBudget


def _diagnosis() -> Diagnosis:
    core = ConflictCore(
        status="INFEASIBLE",
        group_ids=(),
        groups=(),
        wall_time_s=1.0,
        num_candidates=0,
    )
    return Diagnosis(
        status="INFEASIBLE",
        core=core,
        conflicts=(),
        proposals=(),
        escalate=False,
        escalation_reason="",
    )


def test_diagnosis_prompt_ignores_runtime_probe_seconds() -> None:
    """探针调用次数相同、仅耗时不同，下一轮 LLM 上下文必须逐字一致。"""
    fast = ProbeBudget(per_call_s=30.0, max_calls=5, total_s=120.0, calls=2, spent_s=3.0)
    slow = ProbeBudget(per_call_s=30.0, max_calls=5, total_s=120.0, calls=2, spent_s=59.0)

    spec = SimpleNamespace(iso_week="2026W02", scope_persons="ALL", scope_missions="ALL")
    assert _blocks(_diagnosis(), fast, 3, spec=spec) == _blocks(_diagnosis(), slow, 3, spec=spec)
    assert (
        _prompt_budget(fast)
        == _prompt_budget(slow)
        == {
            "calls": 2,
            "max_calls": 5,
            "remaining_calls": 3,
        }
    )


def test_diagnosis_tool_feedback_is_independent_of_mapping_insertion_order() -> None:
    """工具录制后的键排序不得制造一条新的 Diagnosis 请求。"""
    call = ValidatedCall(name="probe_solve", arguments={})
    live = ToolResult(tool="probe_solve", ok=True, value={"status": "OPTIMAL", "sorties": 1})
    replay = ToolResult(tool="probe_solve", ok=True, value={"sorties": 1, "status": "OPTIMAL"})

    assert _tool_notes((call,), (live,)) == _tool_notes((call,), (replay,))
