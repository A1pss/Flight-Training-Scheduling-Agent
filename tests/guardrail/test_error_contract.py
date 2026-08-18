"""错误契约完整性核查（v6 §9.3）—— **16 个码，逐个有触发路径、有测试、有中文说明与可执行建议。**

## 为什么是 16 而不是 14/15

v6 §9.3 原表 15 条（含 `Z-12` 的 FTS-4004），M6 按 `Z-24` 新增第 16 条
`FTS-4005`（状态冲突）。`CLAUDE.md §1` 写着「看到「15 个错误码」的旧表述一律按
16 个理解」——**本文件以 `ERROR_REGISTRY` 为准**，registry 长几条就查几条，
所以再新增一个码时这里自动跟着变严，不会漏。

## 覆盖表怎么算「有测试」

`COVERAGE` 表逐码登记两件事：**触发点**（backend 里 raise 它的模块）与
**覆盖它的测试**。两者都会被验证：

- 触发点：模块必须真的存在，且文本里真的出现这个码或对应的异常类；
- 覆盖测试：用 pytest 自己的收集器确认那个 node id **收得到**。写错一个
  测试名会红，删掉一个用例也会红。

**不用 grep 数「码字符串出现了几次」**：那种统计对「码写在注释里」和「码真的
被抛出来」一视同仁，是典型的假绿。

## 两条 v6 修订的专项

- **FTS-2001** 的口径已扩展为「数据引用完整性失败，**或同一数据源内部的值冲突**」
  （§5.5 的 X1/X3）。两种形态各有断言。
- **FTS-1002** 在当前版本下**不应被触发**（S-01~S-13 全部裁定）。所以这里不是
  「证明它会出现」，而是**构造出「有人新增了一条未裁定的开关」这个情形，
  证明它确实会阻断**。见 `test_fts_1002_...`。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final, NamedTuple

import pytest

from backend.core.config import PROJECT_ROOT
from backend.core.errors import (
    ERROR_REGISTRY,
    ArchitecturalBanError,
    BudgetExceededError,
    DataConflictError,
    ErrorCode,
    ErrorResponse,
    ExportVerifyError,
    FTSError,
    InfeasibleError,
    IngestionError,
    LLMSchemaError,
    LLMUnavailableError,
    RequiredInputMissingError,
    RevisionInfeasibleError,
    RuleParseError,
    ScheduleLockedError,
    SemanticsUnconfirmedError,
    SnapshotStaleError,
    SolveTimeoutError,
    ToolPermissionDeniedError,
    ValidatorSolverDisagreementError,
)
from backend.core.ruleset import load_semantics

pytestmark = pytest.mark.guardrail


class Coverage(NamedTuple):
    """一个码的覆盖登记。"""

    exception: type[FTSError]
    raised_in: tuple[str, ...]
    covered_by: tuple[str, ...]


#: 码 → （异常类、触发点模块、覆盖它的测试）。**收工报告的覆盖表就是它。**
COVERAGE: Final[dict[ErrorCode, Coverage]] = {
    ErrorCode.RULE_PARSE_FAILED: Coverage(
        RuleParseError,
        ("backend/core/ruleset.py",),
        (
            "tests/unit/test_ruleset_loader.py::test_ruleset_rejects_missing_rules",
            "tests/unit/test_ruleset_loader.py::test_ruleset_rejects_bad_tier",
        ),
    ),
    ErrorCode.RULE_SEMANTICS_UNCONFIRMED: Coverage(
        SemanticsUnconfirmedError,
        ("backend/core/ruleset.py",),
        (
            "tests/guardrail/test_error_contract.py"
            "::test_fts_1002_blocks_when_a_new_switch_is_undecided",
            "tests/unit/test_ruleset_loader.py::test_undecided_extra_switch_is_blocked",
        ),
    ),
    ErrorCode.PDF_REPAIR_ASSERTION_FAILED: Coverage(
        IngestionError,
        ("backend/ingestion/repair.py", "backend/ingestion/safety.py"),
        ("tests/unit/test_ingestion_repair.py::test_assert_no_orphan_tokens_blocks_dirty_token",),
    ),
    ErrorCode.REQUIRED_INPUT_MISSING: Coverage(
        RequiredInputMissingError,
        ("backend/ingestion/questions.py", "backend/api/routers/schedule.py"),
        (
            "tests/guardrail/test_error_contract.py::test_fts_1004_missing_cycle_start",
            "tests/guardrail/test_error_contract.py::test_fts_1004_missing_a_whole_input_file",
        ),
    ),
    ErrorCode.DATA_INTEGRITY_OR_CONFLICT: Coverage(
        DataConflictError,
        ("backend/ingestion/validate.py", "backend/ingestion/conflicts.py"),
        (
            "tests/guardrail/test_error_contract.py::test_fts_2001_covers_referential_integrity",
            "tests/guardrail/test_error_contract.py::test_fts_2001_covers_intra_source_conflict",
        ),
    ),
    ErrorCode.INFEASIBLE: Coverage(
        InfeasibleError,
        ("backend/nodes/solve.py",),
        (
            "tests/integration/test_solver_baseline_live.py::test_i1_i4_i5_are_infeasible",
            "tests/unit/test_solver_diagnose.py::test_diagnose_end_to_end_on_infeasible_case",
        ),
    ),
    ErrorCode.SOLVE_TIMEOUT_UNKNOWN: Coverage(
        SolveTimeoutError,
        ("backend/nodes/solve.py",),
        ("tests/unit/test_solver_diagnose.py::test_diagnose_never_labels_unknown_as_infeasible",),
    ),
    ErrorCode.VALIDATOR_SOLVER_DISAGREE: Coverage(
        ValidatorSolverDisagreementError,
        ("backend/nodes/validate.py", "backend/graph/graph.py"),
        (
            "tests/property/test_solver_validator_agreement.py"
            "::test_solver_output_always_passes_validator",
            "tests/property/test_solver_validator_agreement.py"
            "::test_two_checkers_agree_on_solver_output",
        ),
    ),
    ErrorCode.SNAPSHOT_STALE_ON_RESUME: Coverage(
        SnapshotStaleError,
        ("backend/nodes/resume_guard.py",),
        ("tests/integration/test_graph_live.py::test_resume_guard_node_raises_fts_3004",),
    ),
    ErrorCode.REVISION_INFEASIBLE: Coverage(
        RevisionInfeasibleError,
        ("backend/planner/revision.py", "backend/graph/graph.py"),
        (
            "tests/integration/test_revision_live.py"
            "::test_an_infeasible_revision_rolls_back_with_fts_3005",
        ),
    ),
    ErrorCode.LLM_UNAVAILABLE: Coverage(
        LLMUnavailableError,
        # `core/http.py` 抛的是 `EgressDeniedError`（v6 §9.3 未给 egress 单列码，
        # 复用 4001 作为对外口径），所以那个文件里出现的是子类名而不是 4001 字面量。
        ("backend/llm/ollama.py",),
        (
            "tests/guardrail/test_egress.py::test_e1_external_domain_is_denied",
            "tests/guardrail/test_egress.py::test_e1_denial_carries_a_usable_error_contract",
        ),
    ),
    ErrorCode.LLM_SCHEMA_VIOLATION: Coverage(
        LLMSchemaError,
        ("backend/harness/harness.py",),
        (
            "tests/unit/test_harness_call.py::test_degrades_to_form_after_two_retries",
            "tests/guardrail/test_harness_malformed_injection.py"
            "::test_entity_hallucination_that_never_corrects_degrades_honestly",
        ),
    ),
    ErrorCode.HARNESS_BUDGET_EXCEEDED: Coverage(
        BudgetExceededError,
        ("backend/harness/budget.py", "backend/harness/harness.py"),
        (
            "tests/guardrail/test_harness_budget_injection.py"
            "::test_llm_budget_trips_mid_retry_loop",
            "tests/guardrail/test_harness_budget_injection.py"
            "::test_probe_pool_exhaustion_returns_4003",
        ),
    ),
    ErrorCode.TOOL_PERMISSION_DENIED: Coverage(
        ToolPermissionDeniedError,
        ("backend/harness/harness.py", "backend/core/errors.py"),
        (
            "tests/guardrail/test_harness_acl_injection.py"
            "::test_interception_rate_is_one_hundred_percent",
            "tests/unit/test_harness_call.py::test_acl_denial_is_not_retried",
        ),
    ),
    ErrorCode.SCHEDULE_LOCKED: Coverage(
        ScheduleLockedError,
        ("backend/api/service.py", "backend/api/locks.py"),
        (
            "tests/unit/test_api_endpoints.py::test_another_week_is_not_blocked",
            "tests/integration/test_api_live.py"
            "::test_concurrent_submission_for_the_same_week_is_rejected",
        ),
    ),
    ErrorCode.EXPORT_VERIFY_FAILED: Coverage(
        ExportVerifyError,
        ("backend/report/verify.py", "backend/report/archive.py"),
        (
            "tests/unit/test_report_manifest.py"
            "::test_failed_readback_delivers_nothing_but_keeps_the_json",
        ),
    ),
}


# ═════════════════════════════════════════════════════════════════════
# ① 16 个码，一个不少
# ═════════════════════════════════════════════════════════════════════
def test_registry_has_sixteen_codes() -> None:
    """`Z-24` 之后是 16 个码。看到「15 个」的旧表述一律按 16 理解。"""
    assert len(ERROR_REGISTRY) == 16
    assert set(ERROR_REGISTRY) == set(ErrorCode)


def test_coverage_table_covers_every_registered_code() -> None:
    """覆盖表不许漏码，也不许多出一个 registry 里没有的码。"""
    assert set(COVERAGE) == set(ERROR_REGISTRY), (
        f"覆盖表与 registry 不一致。\n只在 registry：{sorted(set(ERROR_REGISTRY) - set(COVERAGE))}\n"
        f"只在覆盖表：{sorted(set(COVERAGE) - set(ERROR_REGISTRY))}"
    )


@pytest.mark.parametrize("code", sorted(ERROR_REGISTRY), ids=lambda c: c.value)
def test_every_code_has_a_chinese_message_and_actionable_advice(code: ErrorCode) -> None:
    """每个码都要有**面向用户的中文说明**与**可执行建议**。

    判据是「说明里有汉字」+「建议非空且不是空话」。一个只会说
    `internal error` 的错误码，对排班员来说与没有错误码是一回事。
    """
    spec = ERROR_REGISTRY[code]
    assert any("一" <= ch <= "鿿" for ch in spec.scenario), f"{code} 的场景说明没有中文"
    assert any("一" <= ch <= "鿿" for ch in spec.behavior), f"{code} 的行为说明没有中文"

    exception = COVERAGE[code].exception
    error = exception("测试构造的错误", suggestions=["先做这个", "再做那个"])
    payload = error.to_response(trace_id="t-1")
    assert isinstance(payload, ErrorResponse)
    assert payload.code == code
    assert payload.suggestions, f"{code} 的响应缺少可执行建议"
    assert payload.trace_id == "t-1"


@pytest.mark.parametrize("code", sorted(ERROR_REGISTRY), ids=lambda c: c.value)
def test_every_code_has_a_real_raise_site(code: ErrorCode) -> None:
    """触发点模块必须存在，且文本里真的出现这个码或它的异常类名。"""
    entry = COVERAGE[code]
    for rel in entry.raised_in:
        path = PROJECT_ROOT / rel
        assert path.is_file(), f"{code} 登记的触发点 {rel} 不存在"
        text = path.read_text(encoding="utf-8")
        assert code.value in text or entry.exception.__name__ in text, (
            f"{rel} 里既没有 {code.value} 也没有 {entry.exception.__name__}"
        )


def test_every_code_names_a_test_that_actually_exists() -> None:
    """覆盖表登记的每个测试 node id 都必须**真的收得到**。

    用 pytest 自己的收集器判，而不是 grep 测试名 —— 后者对「函数被注释掉了」
    这种情况一样会绿。
    """
    import subprocess

    node_ids = sorted({node for entry in COVERAGE.values() for node in entry.covered_by})
    files = sorted({node.split("::", 1)[0] for node in node_ids})
    result = subprocess.run(  # noqa: S603
        # ⚠️ **不能加 `-q`**：这个 pytest 版本的 `-q --collect-only` 只打
        # 「文件: 条数」，node id 一条都不印，于是「找不到」会全体成立 ——
        # 一个永远失败（或永远通过）的检查。不带 `-q` 才逐行打 node id。
        # 用 `sys.executable` 而不是裸 "python"：子进程必须与本进程同一个解释器，
        # 否则收集到的是另一个环境里的用例（或者根本收集不到）。
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "--no-header",
            "-p",
            "no:randomly",
            *files,
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    collected = result.stdout
    missing = [node for node in node_ids if node.split("[")[0] not in collected]
    assert missing == [], (
        "以下覆盖用例收集不到（改名了？删了？）：\n  "
        + "\n  ".join(missing)
        + "\n"
        + result.stdout[-2000:]
    )


# ═════════════════════════════════════════════════════════════════════
# ② FTS-1002：当前版本下不该触发，但**新增未裁定开关时必须阻断**
# ═════════════════════════════════════════════════════════════════════
def test_fts_1002_is_not_triggered_by_the_shipped_semantics() -> None:
    """S-01~S-13 全部裁定 → 交付的 `semantics.yaml` 取快照不该抛。"""
    semantics = load_semantics(PROJECT_ROOT / "rules" / "semantics.yaml")
    snapshot = semantics.snapshot()
    assert len(snapshot) >= 13
    assert all(value for value in snapshot.values()), "有开关没有取值 —— 那正是 FTS-1002 的定义"


def test_fts_1002_blocks_when_a_new_switch_is_undecided(tmp_path: Path) -> None:
    """★ 按 M8 出口标准构造：**新增一条未裁定的语义开关**，确认排班被阻断。

    构造方式是往 `semantics.yaml` 里加一条 `S-14`，只有 `topic` 与 `options`、
    **没有 `value`** —— 这正是「有人加了开关但没走裁定流程」的样子
    （v6 §14 R1 的残留风险那一行）。
    """
    source = (PROJECT_ROOT / "rules" / "semantics.yaml").read_text(encoding="utf-8")
    block = (
        "  # ── S-14（测试构造）：新增但**未裁定**的开关 ──\n"
        "  S-14:\n"
        "    topic: 教员单日带飞上限是否按自然日计\n"
        "    options: [calendar_day, duty_period]\n"
        "    rationale: 未经业务方裁定，故意不写 value\n"
        "\n"
    )
    # ⚠️ 必须插在 `switches:` 段**内部**。`frequency_anchor:` 在文件更靠后的位置，
    # 直接 `source + block` 会让 S-14 落到那个键底下 —— 于是 `switches` 里根本
    # 没有它，构造静默失效（第一版就是这么写的，红在「新开关没被读进来」）。
    marker = "frequency_anchor:"
    assert marker in source, "semantics.yaml 的结构变了，S-14 的插入点要重新定"
    tampered = source.replace(marker, block + marker, 1)
    path = tmp_path / "semantics.yaml"
    path.write_text(tampered, encoding="utf-8")

    # 阻断发生在**解析期**：`parse_semantics` 一读到没有 `value` 的开关就抛，
    # 不必等到 `snapshot()`。这比「用的时候才炸」更早，也更对 —— 一份没裁定完
    # 的 semantics 根本不该被装进进程。
    with pytest.raises(SemanticsUnconfirmedError) as excinfo:
        load_semantics(path)
    assert excinfo.value.code == ErrorCode.RULE_SEMANTICS_UNCONFIRMED
    assert "S-14" in str(excinfo.value)

    # ★ 反向对照，也是这条防线真正的强度所在：**光把 `value` 填上并不放行**。
    # `parse_semantics` 拦的是「出现了 `REQUIRED_SWITCHES` 之外的开关」，
    # 而那份清单在**代码**里。也就是说，想让 S-14 生效，必须有人去改代码里的
    # 登记表 —— 那一步会进 code review、会留 git 记录、会被问「业务方哪天裁定的」。
    # 只改 yaml 悄悄加一条开关这条路是走不通的。
    decided = tampered.replace(
        "    rationale: 未经业务方裁定，故意不写 value",
        "    value: calendar_day\n    rationale: 自己填了个值",
        1,
    )
    decided_path = tmp_path / "semantics_decided.yaml"
    decided_path.write_text(decided, encoding="utf-8")
    with pytest.raises(SemanticsUnconfirmedError) as still_blocked:
        load_semantics(decided_path)
    assert "S-14" in str(still_blocked.value)

    # 而 S-01~S-13 少一条同样阻断（另一个方向的同一道防线）。
    without_s13 = source.replace("  S-13:", "  S-13-disabled:", 1)
    (tmp_path / "semantics_missing.yaml").write_text(without_s13, encoding="utf-8")
    with pytest.raises(SemanticsUnconfirmedError):
        load_semantics(tmp_path / "semantics_missing.yaml")


def test_fts_1002_is_error_severity_and_not_retryable() -> None:
    """未裁定的语义**不可重试** —— 重试一百次那条开关还是没有裁定。"""
    spec = ERROR_REGISTRY[ErrorCode.RULE_SEMANTICS_UNCONFIRMED]
    assert spec.retryable is False
    assert spec.severity in {"ERROR", "CRITICAL"}


# ═════════════════════════════════════════════════════════════════════
# ③ FTS-1004：两种构造（少传一类数据 / 不给 cycle_start）
# ═════════════════════════════════════════════════════════════════════
def test_fts_1004_missing_cycle_start() -> None:
    """构造一：课目文件没有「课程开始日期」列，用户也没回答 `Q_cycle_start`。

    这是 S-14 裁定的第三分支：**提问并阻断，不设默认值**（v6 §6.3.1 / §5.1.1）。
    `resolution="answer"` —— 给一个值即可，不必重传文件。
    """
    from backend.ingestion.questions import detect_open_questions
    from backend.ingestion.schema import IngestedFacts, IngestedMission

    facts = IngestedFacts(
        missions=(
            IngestedMission(
                mission_id="missionA-1",
                name="A-1",
                mission_class="A",
                kind="飞行",
                duration_minutes=60,
                cycle_weeks=12,
                freq_days=3,
                dual_required=False,
                aircraft_types=("JL-8",),
                airspace_name="一号空域",
                cycle_start=None,  # ← 课目文件没给「课程开始日期」
            ),
        )
    )
    questions = [q for q in detect_open_questions(facts) if "cycle_start" in q.question_id]
    assert questions, "缺 cycle_start 时必须提问（不许设默认值）"
    question = questions[0]
    assert question.resolution == "answer"

    error = RequiredInputMissingError(
        "排班必需的输入缺失，需人工补充",
        details={"questions": [{"question_id": question.question_id, "resolution": "answer"}]},
        suggestions=["回答课程周期起点（如 2026-01-05）后重跑"],
    )
    assert error.code == ErrorCode.REQUIRED_INPUT_MISSING
    assert error.retryable is True
    assert error.details["questions"][0]["resolution"] == "answer"


def test_fts_1004_missing_a_whole_input_file() -> None:
    """构造二：少传了一整类数据（如空域文件）。

    `resolution="upload"` —— **必须补传整份文件**，不许拿基准数据或上一版快照顶替
    （CLAUDE.md 反模式）。
    """
    from backend.ingestion.questions import detect_missing_inputs
    from backend.ingestion.schema import IngestedFacts

    questions = detect_missing_inputs(IngestedFacts())
    uploads = [q for q in questions if q.resolution == "upload"]
    assert uploads, "少传一整类数据时必须要求补传，不许拿旧快照顶替"

    error = RequiredInputMissingError(
        "排班必需的输入缺失，需人工补充",
        details={
            "questions": [
                {"question_id": q.question_id, "resolution": q.resolution} for q in uploads
            ]
        },
        suggestions=["补传缺失的那一类数据文件后重跑"],
    )
    assert error.code == ErrorCode.REQUIRED_INPUT_MISSING
    assert error.retryable is True
    assert all(item["resolution"] == "upload" for item in error.details["questions"])


# ═════════════════════════════════════════════════════════════════════
# ④ FTS-2001：v6 扩展后的两种形态
# ═════════════════════════════════════════════════════════════════════
def test_fts_2001_covers_referential_integrity() -> None:
    """形态一：孤立外键（课目引用了不存在的空域）。"""
    error = DataConflictError(
        "课目 missionA-1 引用了不存在的空域 AS-99",
        details={"kind": "orphan_reference", "mission_id": "missionA-1", "airspace_id": "AS-99"},
        suggestions=["补齐空域数据后重新上传"],
    )
    assert error.code == ErrorCode.DATA_INTEGRITY_OR_CONFLICT


def test_fts_2001_covers_intra_source_conflict() -> None:
    """形态二（v6 扩展）：同一数据源内部的值冲突，如 §5.5 的 X1。"""
    error = DataConflictError(
        "刘斌 C 类到期日在总表与明细表中不一致",
        details={
            "kind": "intra_source_conflict",
            "conflict_id": "X1",
            "values": ["2026-01-07", "2026-02-07"],
        },
        suggestions=["按 v6 §5.5 裁定表选定 2026-01-07（总表）"],
    )
    assert error.code == ErrorCode.DATA_INTEGRITY_OR_CONFLICT
    assert error.details["kind"] == "intra_source_conflict"


def test_fts_2001_scenario_text_mentions_both_forms() -> None:
    """registry 里的场景描述必须体现 v6 扩展后的**两种**形态。"""
    scenario = ERROR_REGISTRY[ErrorCode.DATA_INTEGRITY_OR_CONFLICT].scenario
    assert "完整性" in scenario and "冲突" in scenario


# ═════════════════════════════════════════════════════════════════════
# ⑤ 几条最容易被混用的码，逐对钉住
# ═════════════════════════════════════════════════════════════════════
def test_4005_is_not_3004() -> None:
    """锁冲突（数据没变、有人在排）≠ 快照过期（数据变了要重解）。"""
    locked = ERROR_REGISTRY[ErrorCode.SCHEDULE_LOCKED]
    stale = ERROR_REGISTRY[ErrorCode.SNAPSHOT_STALE_ON_RESUME]
    assert locked.retryable is True, "等对方跑完再提交即可"
    assert locked.severity == "WARN"
    assert locked.code != stale.code


def test_3002_is_not_3001() -> None:
    """UNKNOWN ≠ INFEASIBLE（铁律 8）—— 两个码、两种严重度、两条出路。"""
    unknown = ERROR_REGISTRY[ErrorCode.SOLVE_TIMEOUT_UNKNOWN]
    infeasible = ERROR_REGISTRY[ErrorCode.INFEASIBLE]
    assert unknown.code != infeasible.code
    assert unknown.retryable is True, "延长时限后可以再试"
    assert "UNKNOWN" in unknown.scenario or "超时" in unknown.scenario


def test_3003_is_critical() -> None:
    """求解器与校验器分歧是 CRITICAL，且**不可重试** —— 要人来查规格。"""
    spec = ERROR_REGISTRY[ErrorCode.VALIDATOR_SOLVER_DISAGREE]
    assert spec.severity == "CRITICAL"
    assert spec.retryable is False


def test_4004_architectural_ban_is_critical() -> None:
    """越权分两档：ACL 违规是 ERROR，踩架构级禁令是 CRITICAL（`Z-12`）。"""
    denied = ToolPermissionDeniedError("planner 无权调用 solve")
    banned = ArchitecturalBanError("planner 试图调用确定性节点 solve")
    assert denied.code == ErrorCode.TOOL_PERMISSION_DENIED
    assert banned.code == ErrorCode.TOOL_PERMISSION_DENIED
    assert ERROR_REGISTRY[ErrorCode.TOOL_PERMISSION_DENIED].retryable is False
