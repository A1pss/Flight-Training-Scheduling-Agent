"""前端纯逻辑的单测：Sheet 4 七区块、三表拼接、时间线/调用图、规则溯源。

**这些函数一个都不碰 `st.*`**，所以不需要浏览器就能验内容对不对。
浏览器那一层由 `tests/e2e/` 用 Playwright 验（那里验的是「显示出来了没有」，
不是「算得对不对」）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from backend.retrieval.corpus import rule_docs
from backend.schemas.api import JobStage, JobStatus, RunResultView, SolverPanelView
from backend.schemas.common import TraceEvent
from backend.schemas.plan import BlockedItem, CrewMember, SchedulePlan, Sortie, TrainingDebt
from backend.schemas.solver import SolverStats
from backend.schemas.validation import CheckResult, ValidationReport, Violation
from frontend.components import process as proc
from frontend.components import rules_ref, sheet4, tables

INSTRUCTOR = CrewMember(person_id="P01", name="孙军", role="教员")
STUDENT = CrewMember(person_id="P07", name="陈伟", role="学员")
SOLO = CrewMember(person_id="P08", name="何超", role="单飞")
RECURRENT = CrewMember(person_id="P04", name="刘斌", role="复训")


def _sortie(
    sortie_id: str = "S000001",
    *,
    crew: list[CrewMember] | None = None,
    runway: str = "RWY-1",
    is_recurrent: bool = False,
    takeoff: time = time(8, 0),
) -> Sortie:
    return Sortie(
        sortie_id=sortie_id,
        date=date(2026, 1, 5),
        weekday="周一",
        takeoff=takeoff,
        landing=time(takeoff.hour, takeoff.minute + 35),
        mission_id="missionC-1",
        mission_name="仪表飞行",
        airspace_id="IFR",
        aircraft_id="AC10",
        runway_id=runway,
        is_recurrent=is_recurrent,
        crew=crew or [INSTRUCTOR, STUDENT],
    )


def _plan(**overrides: object) -> SchedulePlan:
    base = {
        "plan_id": "pl_test",
        "iso_week": "2026W02",
        "week_start": date(2026, 1, 5),
        "week_end": date(2026, 1, 11),
        "snapshot_id": "snap_x",
        "ruleset_version": "1.3.0",
        "semantics_version": "1.1.0",
        "semantics_switches": {"S-01": "all_missions_completed", "S-02": "class_level"},
        "runway_model": "dual_runway",
        "relaxation_tier": 0,
        "sorties": [_sortie()],
        "debts": [],
        "blocked_items": [],
        "content_sha256": "a" * 64,
    }
    base.update(overrides)
    return SchedulePlan.model_validate(base)


def _report(results: list[CheckResult] | None = None) -> ValidationReport:
    return ValidationReport(
        plan_id="pl_test",
        ruleset_version="1.3.0",
        semantics_version="1.1.0",
        results=results
        or [
            CheckResult(
                rule_id=f"C{i:02d}",
                rule_title=f"规则{i}",
                passed=True,
                checked_items=i * 3,
                duration_ms=1.0,
            )
            for i in range(1, 15)
        ],
    )


def _run(**overrides: object) -> RunResultView:
    base: dict[str, object] = {
        "trace_id": "t1",
        "status": JobStatus.AWAITING_HUMAN,
        "stage": JobStage.REPORTING,
        "snapshot_id": "snap_x",
        "ruleset_version": "1.3.0",
        "semantics_version": "1.1.0",
        "plan": _plan(),
        "validation": _report(),
        "solver": SolverPanelView(
            stats=SolverStats(
                status="OPTIMAL",
                num_candidates=2276,
                num_variables=12568,
                num_constraints=37235,
                objective_value=1.0,
                gap=0.0,
                wall_time_ms=21000.0,
            ),
            runway_allocation={"RWY-1": 7, "RWY-2": 7},
        ),
    }
    base.update(overrides)
    return RunResultView.model_validate(base)


# ─────────────────────────────────────────────────────────────────────
# Sheet 4 七区块（v6 §10.4）
# ─────────────────────────────────────────────────────────────────────
def test_seven_blocks_always_seven() -> None:
    blocks = sheet4.all_blocks(_run())
    assert len(blocks) == 7
    assert [title for title, _ in blocks] == list(sheet4.BLOCK_TITLES)


def test_block1_carries_the_semantics_switches_line() -> None:
    """`Z-7`：语义开关参与 content_sha256，必须出现在区块 1。"""
    rows = {row["字段"]: row["取值"] for row in sheet4.block_1_meta(_run())}
    assert rows["语义开关"] == "S-01=all_missions_completed；S-02=class_level"
    assert rows["跑道模型"] == "dual_runway"
    assert "OPTIMAL" in rows["求解状态 / 耗时 / 目标值 / gap / worker / seed"]


def test_block2_shows_checked_items_per_rule() -> None:
    rows = sheet4.block_2_validation(_run())
    assert len(rows) == 14
    assert rows[0]["检查项数"] == 3
    assert rows[13]["规则编号"] == "约束14"


def test_block3_has_the_relax_tier_column() -> None:
    """`Z-10`：「松弛档」是第 10 列，没有它闸门 3 反解不出 `plan.debts`。"""
    plan = _plan(
        debts=[
            TrainingDebt(
                person_id="P07",
                mission_id="missionC-2",
                required=1,
                scheduled=0,
                debt=1,
                relaxed_by="TIER1",
            )
        ]
    )
    rows = sheet4.block_3_progress(_run(plan=plan))
    assert rows[0]["松弛档"] == "TIER1"
    assert rows[0]["欠账"] == 1


def test_block4_lists_every_blocked_combination() -> None:
    """披露率 100%（v6 §0.3）：被排除的组合不能悄悄消失。"""
    plan = _plan(
        blocked_items=[
            BlockedItem(
                person_id="P08",
                mission_id="missionB-1",
                reason="先修 A 类未达标",
                missing_prereqs=["missionA-2"],
            )
        ]
    )
    rows = sheet4.block_4_blocked(_run(plan=plan))
    assert rows == [
        {
            "人员": "P08",
            "课目": "missionB-1",
            "阻塞原因": "先修 A 类未达标",
            "缺失先修": "missionA-2",
        }
    ]


def test_block5_counts_aircraft_person_airspace_runway() -> None:
    rows = sheet4.block_5_resources(_run())
    kinds = {row["类别"] for row in rows}
    assert kinds == {"飞机", "人员", "空域", "跑道"}
    aircraft = next(r for r in rows if r["对象"] == "AC10")
    assert aircraft["架次"] == 1 and aircraft["飞行时长(分)"] == 35


def test_block6_always_carries_the_s11_declaration_row() -> None:
    """v6 §10.4 区块6：只要 S-11 为 on，这一行必须出现（风险 R17）。"""
    results = [
        CheckResult(
            rule_id="C13",
            rule_title="任务完成度",
            passed=True,
            checked_items=91,
            duration_ms=1.0,
            notes=["S-11：成熟飞行员到期资质转复训，系业务方授权改写"],
        )
    ]
    rows = {
        row["项"]: row["内容"]
        for row in sheet4.block_6_relaxation(_run(validation=_report(results)))
    }
    assert "授权改写" in "".join(rows)
    assert "S-11" in rows["授权改写声明"]
    assert rows["使用的松弛"] == "本次未使用任何松弛"


def test_block7_carries_runway_and_recurrent_flag() -> None:
    plan = _plan(sorties=[_sortie(crew=[RECURRENT], runway="RWY-2", is_recurrent=True)])
    rows = sheet4.block_7_runway(_run(plan=plan))
    assert rows[0]["跑道"] == "RWY-2"
    assert rows[0]["复训标记"] == "复训"


def test_tier_two_label_follows_d6() -> None:
    """D-6：T2 是「约束3 整体降级为软目标」，**不是**旧的「A 类降至每人 1 次」。"""
    assert "约束3" in sheet4.TIER_LABELS[2]
    assert "每人 1 次" not in sheet4.TIER_LABELS[2]
    assert "训练主任" in sheet4.TIER_LABELS[3]


def test_blocked_banner_counts_by_person() -> None:
    plan = _plan(
        blocked_items=[
            BlockedItem(person_id="P08", mission_id=f"missionB-{i}", reason="x") for i in (1, 2)
        ]
        + [BlockedItem(person_id="P06", mission_id="missionC-2", reason="y")]
    )
    banner = sheet4.blocked_banner(plan)
    assert banner.startswith("⚠ 3 项因先修未满足未安排")
    assert "P08 2 项" in banner and "P06 1 项" in banner


def test_blocked_banner_is_empty_when_nothing_is_blocked() -> None:
    assert sheet4.blocked_banner(_plan()) == ""


def test_format_gates_shows_three_layers() -> None:
    text = sheet4.format_gates(_run())
    assert "Schema 层" in text and "业务完整性层" in text and "产物回读层" in text


def test_format_gates_marks_a_failed_rule() -> None:
    failing = _report(
        [
            CheckResult(
                rule_id="C09",
                rule_title="起降密度限制",
                passed=False,
                checked_items=35,
                duration_ms=1.0,
                violations=[Violation(rule_id="C09", detail="20 分钟窗口内 3 次起飞")],
            )
        ]
    )
    assert sheet4.format_gates(_run(validation=failing)).startswith("Schema 层 ❌")


# ─────────────────────────────────────────────────────────────────────
# 三表（v6 §10.1~§10.3）
# ─────────────────────────────────────────────────────────────────────
def test_sheet1_crew_uses_full_width_comma_and_role_suffix() -> None:
    """`Z-10`：机组分隔符是**全角逗号**，回读侧只认这一个。"""
    rows = tables.sheet1_rows(_plan())
    assert rows[0]["机组"] == "孙军教，陈伟学"
    assert rows[0]["课目（空域）"] == "仪表飞行 (missionC-1)（IFR）"


def test_sheet1_solo_and_recurrent_suffixes() -> None:
    plan = _plan(
        sorties=[
            _sortie("S000001", crew=[SOLO]),
            _sortie("S000002", crew=[RECURRENT], is_recurrent=True, takeoff=time(9, 0)),
        ]
    )
    crews = [row["机组"] for row in tables.sheet1_rows(plan)]
    assert crews == ["何超单", "刘斌训"]


def test_sheet2_is_grouped_per_person() -> None:
    rows = tables.sheet2_rows(_plan())
    assert {row["飞行员"] for row in rows} == {"孙军(P01)", "陈伟(P07)"}
    assert rows[0]["时间"] == "08:00-08:35"


def test_sheet3_crew_is_parenthesised() -> None:
    rows = tables.sheet3_rows(_plan())
    assert rows[0]["机组"] == "（孙军/陈伟）"


def test_sheets_do_not_carry_a_runway_column() -> None:
    """跑道只在 Sheet 4 区块 7（v6 §10.4）。三表加了列就偏离版式基准。"""
    for builder in (tables.sheet1_rows, tables.sheet2_rows, tables.sheet3_rows):
        for row in builder(_plan()):
            assert "跑道" not in row


def test_gantt_uses_minute_of_day() -> None:
    rows = tables.gantt_rows(_plan())
    assert rows[0]["起飞分钟"] == 480 and rows[0]["着陆分钟"] == 515


def test_week_summary_counts_by_crew_size_not_mission_class() -> None:
    """D-1 之后「A 类学员单飞」是常态，按类别数会数错。"""
    plan = _plan(
        sorties=[
            _sortie("S000001"),
            _sortie("S000002", crew=[SOLO], takeoff=time(9, 0)),
            _sortie("S000003", crew=[RECURRENT], is_recurrent=True, takeoff=time(10, 0)),
        ]
    )
    assert tables.week_summary(plan) == {
        "架次": 3,
        "带飞": 1,
        "单飞": 1,
        "复训": 1,
        "阻塞项": 0,
    }


def test_empty_plan_yields_empty_tables() -> None:
    assert tables.sheet1_rows(None) == []
    assert tables.gantt_rows(None) == []
    assert tables.week_summary(None)["架次"] == 0


# ─────────────────────────────────────────────────────────────────────
# 运作过程（v6 §8.2）
# ─────────────────────────────────────────────────────────────────────
def _events(*agents: str) -> list[TraceEvent]:
    return [
        TraceEvent(seq=i, ts=datetime.now(UTC), agent=agent, kind="decision", payload={"i": i})
        for i, agent in enumerate(agents)
    ]


@pytest.mark.parametrize(
    ("agent", "expected"),
    [
        ("knowledge", "agent"),
        ("diagnosis", "agent"),
        ("route", "llm"),
        ("planner", "llm"),
        ("explain", "llm"),
        ("solve", "deterministic"),
        ("validate", "deterministic"),
        ("human_gate", "deterministic"),
        ("commit_plan", "deterministic"),
        ("something_else", "other"),
    ],
)
def test_node_kind_classification(agent: str, expected: str) -> None:
    """三类节点必须可分 —— 这是「模型没机会跳过 validate」在界面上的样子。"""
    assert proc.node_kind(agent) == expected


def test_three_kinds_have_distinct_icons_and_colors() -> None:
    kinds = ("agent", "llm", "deterministic")
    assert len({proc.NODE_ICONS[k] for k in kinds}) == 3
    assert len({proc.NODE_COLORS[k] for k in kinds}) == 3


def test_deterministic_set_is_exactly_the_six_nodes() -> None:
    """铁律 4 的那六个。多一个少一个都说明分类表漂了。"""
    assert {
        "compile_spec",
        "solve",
        "validate",
        "resume_guard",
        "human_gate",
        "commit_plan",
    } == proc.DETERMINISTIC_NODES


def test_timeline_is_one_item_per_event_in_seq_order() -> None:
    items = proc.timeline_items(_events("route", "planner", "solve"))
    assert [item["seq"] for item in items] == [0, 1, 2]
    assert items[2]["icon"] == proc.NODE_ICONS["deterministic"]


def test_replay_complete_matches_the_run_view() -> None:
    events = _events("route", "solve")
    assert proc.replay_complete(events) is True
    assert proc.replay_complete([events[1]]) is False  # seq 从 1 开始 = 缺了第 0 步
    assert proc.replay_complete([]) is False


def test_call_graph_marks_loops_with_a_count() -> None:
    """回环次数标在边上 —— 一眼看出是否发生了重解。"""
    events = _events("planner", "solve", "validate", "solve", "validate", "explain")
    dot = proc.call_graph_dot(events)
    assert '"solve" -> "validate"' in dot
    assert "×2" in dot
    assert dot.startswith("digraph fts {") and dot.rstrip().endswith("}")


def test_call_graph_colors_the_three_kinds_differently() -> None:
    dot = proc.call_graph_dot(_events("knowledge", "planner", "solve"))
    for kind in ("agent", "llm", "deterministic"):
        assert proc.NODE_COLORS[kind][0] in dot


def test_call_graph_handles_no_events() -> None:
    assert "本次运行没有事件" in proc.call_graph_dot([])


def test_call_path_dedupes_adjacent_repeats() -> None:
    assert proc.call_path(_events("solve", "solve", "validate")) == ["solve", "validate"]


def test_solver_panel_has_the_runway_allocation_row() -> None:
    rows = {row["项"]: row["值"] for row in proc.solver_panel(_run())}
    assert rows["候选数"] == 2276
    assert rows["求解状态"] == "OPTIMAL"
    assert "RWY-1 7 架次" in rows["跑道分配统计"]


def test_solver_panel_without_stats_says_so() -> None:
    rows = proc.solver_panel(_run(solver=SolverPanelView()))
    assert "没有求解统计" in rows[0]["值"]


# ─────────────────────────────────────────────────────────────────────
# 规则溯源
# ─────────────────────────────────────────────────────────────────────
def test_rule_references_cover_all_fourteen() -> None:
    refs = rules_ref.rule_references()
    assert len(refs) == 14
    assert set(refs) == {f"C{i:02d}" for i in range(1, 15)}


def test_c06_display_name_is_the_v6_rename() -> None:
    assert rules_ref.rule_references()["C06"].title == "资源有效性与容量"


def test_c09_note_states_both_scopes() -> None:
    """D-2：20 分钟窗口按跑道，7 分钟间隔全场。最容易写错的一条。"""
    note = rules_ref.rule_references()["C09"].note
    assert "20 分钟窗口按跑道" in note and "7 分钟间隔全场" in note


def test_chroma_doc_ids_match_the_corpus_builder() -> None:
    """溯源标识必须与检索路 C 的文档 id 一致，否则界面上引的是查不到的东西。"""
    corpus_ids = {doc.doc_id for doc in rule_docs()}
    ui_ids = {ref.chroma_doc_id for ref in rules_ref.rule_references().values()}
    assert ui_ids == corpus_ids
