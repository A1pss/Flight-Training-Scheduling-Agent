"""Planner（v6 §7.3）：把模糊需求翻译成精确的求解输入。

四件事，四个模块：

| 模块 | 职责 | v6 |
|---|---|---|
| `intent` | `SolveIntent` 生成、三步协商 | §7.3.2 / §7.3.3 |
| `scope` | 影响面探测、自我降档、扰动评估 | §7.3.3 ① |
| `authority` | 松弛档位的授权门槛 | §3.10 / §7.3.3 ② |
| `revision` | 多轮修订翻译（六种 `kind`）、修订栈 | §7.3.4 |
| `calibration` | 置信度校准（self-consistency + Harness 侧特征） | §7.3.5、`Z-11` |

**本包不 import `backend.routing` 之外的编排层**，也不被求解链路 import：
它是 LLM 侧的，`SolveIntent` 是它与确定性半区之间唯一的接口。
"""

from backend.planner.authority import (
    RELAX_TIER_AUTHORITY,
    ROLE_RANK,
    AuthorityCheck,
    authorized_tiers,
    check_authority,
    normalize_role,
    required_role_for,
)
from backend.planner.calibration import (
    DEFAULT_CALIBRATOR,
    CalibrationFeatures,
    ConfidenceCalibrator,
    brier_score,
    consistency_ratio,
    expected_calibration_error,
    heuristic_confidence,
    reliability_bins,
)
from backend.planner.intent import (
    NEUTRAL_WEIGHTS,
    PLANNER_AGENT,
    PLANNER_TOOLS,
    ClarificationRequest,
    PlannerDecision,
    deterministic_intent,
    plan_solve_intent,
)
from backend.planner.revision import (
    FEW_SHOT,
    REVISION_KINDS,
    RevisionStack,
    RevisionTranslation,
    check_runway_feasibility,
    echo_text,
    few_shot_block,
    rule_translate,
    translate_revision,
)
from backend.planner.scope import (
    DisruptionReport,
    ScopeDecision,
    apply_scope_policy,
    assess_disruption,
    downgrade_freeze,
    estimate_scope,
)

__all__ = [
    "DEFAULT_CALIBRATOR",
    "FEW_SHOT",
    "NEUTRAL_WEIGHTS",
    "PLANNER_AGENT",
    "PLANNER_TOOLS",
    "RELAX_TIER_AUTHORITY",
    "REVISION_KINDS",
    "ROLE_RANK",
    "AuthorityCheck",
    "CalibrationFeatures",
    "ClarificationRequest",
    "ConfidenceCalibrator",
    "DisruptionReport",
    "PlannerDecision",
    "RevisionStack",
    "RevisionTranslation",
    "ScopeDecision",
    "apply_scope_policy",
    "assess_disruption",
    "authorized_tiers",
    "brier_score",
    "check_authority",
    "check_runway_feasibility",
    "consistency_ratio",
    "deterministic_intent",
    "downgrade_freeze",
    "echo_text",
    "estimate_scope",
    "expected_calibration_error",
    "few_shot_block",
    "heuristic_confidence",
    "normalize_role",
    "plan_solve_intent",
    "reliability_bins",
    "required_role_for",
    "rule_translate",
    "translate_revision",
]
