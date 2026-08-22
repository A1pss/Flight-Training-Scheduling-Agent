"""`backend/experiments/trajectory_eval.py` 的单测（§12.6 的判定逻辑）。"""

from __future__ import annotations

from backend.experiments.trajectory_eval import (
    FREE_TEXT_FIELDS,
    aggregate,
    lcs_length,
    params_match,
    path_is_correct,
    path_similarity,
    score_steps,
)


def test_lcs_basic() -> None:
    assert lcs_length(["a", "b", "c"], ["a", "c"]) == 2
    assert lcs_length([], ["a"]) == 0


def test_similarity_penalises_extra_steps() -> None:
    """用 Dice 而不是 `LCS/|expected|`：后者对「多走了三步」毫无惩罚，
    而冗余调用正是本组要抓的失效之一。"""
    assert path_similarity(["a", "b"], ["a", "b"]) == 1.0
    assert path_similarity(["a", "b", "x", "y"], ["a", "b"]) < 1.0


def test_forbidden_path_beats_high_similarity() -> None:
    """禁止路径即便与期望只差一个元素也必须判错。

    数据集的 E 规则：`sql_query` 也能查到人和课目，但「A 类整体达标」要手写
    递归 —— 偶尔答对、迟早写错。相似度高恰恰是这类错误危险的地方。
    """
    ok, reason = path_is_correct(
        ["route", "knowledge", "tool:sql_query", "END"],
        ["route", "knowledge", "tool:prereq_cte", "END"],
        forbidden=[["route", "knowledge", "tool:sql_query", "END"]],
    )
    assert ok is False
    assert "forbidden" in reason


def test_acceptable_path_counts_as_correct() -> None:
    ok, _ = path_is_correct(
        ["route", "knowledge", "tool:prereq_cte", "tool:sql_query", "END"],
        ["route", "knowledge", "tool:prereq_cte", "END"],
        acceptable=[["route", "knowledge", "tool:prereq_cte", "tool:sql_query", "END"]],
    )
    assert ok is True


def test_free_text_params_are_not_compared() -> None:
    """M7 §4.2：期望 `query="张勇 的训练情况"`、模型给分好词的版本，
    工程上更对却被字符串相等判成错。自由文本一律不比。"""
    assert "rationale" in FREE_TEXT_FIELDS
    assert params_match(
        {"person_id": "P08", "rationale": "随便"}, {"person_id": "P08", "rationale": "别的"}
    )


def test_entity_ids_must_match_exactly() -> None:
    assert not params_match({"person_id": "P02"}, {"person_id": "P08"})


def test_missing_expected_field_is_a_mismatch() -> None:
    assert not params_match({}, {"person_id": "P08"})


def test_extra_optional_field_is_tolerated() -> None:
    """多填一个可选字段不是错误；少填或填错**被标注的**字段才是。"""
    assert params_match({"person_id": "P08", "top_k": 5}, {"person_id": "P08"})


def test_list_params_compare_as_sets() -> None:
    assert params_match({"ids": ["b", "a"]}, {"ids": ["a", "b"]})


def test_unmatched_required_step_is_counted() -> None:
    """必需步骤没匹配上要计数 —— 但**这不是缺失调用率**，见下一条。"""
    score = score_steps([{"tool": "prereq_cte", "params": {}, "optional": False}], [])
    assert score.unmatched_required == 1


def test_optional_step_absence_is_not_unmatched() -> None:
    """数据集规则 B：信息已足够时省略可选步骤是可接受的。"""
    score = score_steps([{"tool": "sql_query", "params": {}, "optional": True}], [])
    assert score.unmatched_required == 0


def test_missing_calls_means_no_tool_at_all_not_wrong_tool() -> None:
    """★ `Z-41` 的回归闸：**缺失调用率的判据是「一个工具都没调」**。

    §12.6 的定义是「该调工具却**直接回答（凭记忆编造）**」。
    「换了个工具去查」不是凭记忆编造 —— 那是工具选择错误，已由工具选择
    准确率在管；算进缺失调用率等于**同一个错误记两遍**，还记进了规格称为
    「最重要」的那一个（M9-B 实测两种算法差 27 个点：54.29% vs 26.67%）。
    """
    from backend.experiments.trajectory_eval import TrajectoryOutcome

    wrong_tool = TrajectoryOutcome(item_id="A", flow="query")
    wrong_tool.steps = score_steps(
        [{"tool": "sql_query", "params": {}, "optional": False}],
        [("bm25_search", {}), ("vector_search", {})],
    )
    silent = TrajectoryOutcome(item_id="B", flow="query")
    silent.steps = score_steps([{"tool": "sql_query", "params": {}, "optional": False}], [])

    agg = aggregate([wrong_tool, silent])
    assert agg["missing_calls"] == {"hits": 1, "n": 2}, "只有「一个工具都没调」的那条算缺失"
    assert agg["unmatched_required_steps"]["hits"] == 2, "两条的必需步骤都没匹配上（诊断量）"


def test_repeated_same_tool_with_different_params_is_an_acceptable_path() -> None:
    """★ `Z-41` 的回归闸：同工具不同参数的连续重复调用，折叠后再比。

    实测模型对同一工具用不同参数多查几次是**探索**，不是走错路，
    而逐元素比对会整条判 0 分 —— 等于用路径正确率惩罚探索。
    """
    ok, reason = path_is_correct(
        ["route", "knowledge", "tool:prereq_cte", "tool:prereq_cte", "tool:prereq_cte", "END"],
        ["route", "knowledge", "tool:prereq_cte", "END"],
    )
    assert ok is True
    assert "折叠" in reason


def test_collapsing_never_launders_a_forbidden_path() -> None:
    """折叠只用来宽容重复探索，**不得把禁止路径洗成可接受**。"""
    forbidden = [["route", "knowledge", "tool:sql_query", "tool:sql_query", "END"]]
    ok, reason = path_is_correct(
        ["route", "knowledge", "tool:sql_query", "tool:sql_query", "END"],
        ["route", "knowledge", "tool:prereq_cte", "END"],
        forbidden=forbidden,
    )
    assert ok is False
    assert "forbidden" in reason


def test_collapsing_only_merges_adjacent_repeats() -> None:
    """`A → A → B → A` 折成 `A → B → A` —— 隔着别的步骤的那次不能丢，顺序信息要留住。"""
    from backend.experiments.trajectory_eval import _collapse_repeat_tools

    assert _collapse_repeat_tools(["tool:a", "tool:a", "tool:b", "tool:a"]) == [
        "tool:a",
        "tool:b",
        "tool:a",
    ]


def test_collapsing_does_not_touch_non_tool_nodes() -> None:
    """节点名不折叠 —— 连着两次 `solve` 是真的回环，不是重复探索。"""
    from backend.experiments.trajectory_eval import _collapse_repeat_tools

    assert _collapse_repeat_tools(["solve", "solve"]) == ["solve", "solve"]


def test_alternatives_count_as_the_right_tool() -> None:
    score = score_steps(
        [
            {
                "tool": "bm25_search",
                "params": {},
                "optional": False,
                "alternatives": ["vector_search"],
            }
        ],
        [("vector_search", {})],
    )
    assert score.tool_hits == 1


def test_repeating_a_consumed_call_is_redundant() -> None:
    """「又查了一遍刚查过的东西」是最典型的冗余形态。

    只在**剩余**调用之间比会把它漏掉 —— 第一次已被期望步骤认领了。
    """
    score = score_steps(
        [{"tool": "prereq_cte", "params": {"person_id": "P08"}, "optional": False}],
        [("prereq_cte", {"person_id": "P08"}), ("prereq_cte", {"person_id": "P08"})],
    )
    assert score.redundant == 1


def test_different_entity_is_not_redundant() -> None:
    score = score_steps(
        [{"tool": "prereq_cte", "params": {"person_id": "P08"}, "optional": False}],
        [("prereq_cte", {"person_id": "P08"}), ("prereq_cte", {"person_id": "P02"})],
    )
    assert score.redundant == 0


def test_aggregate_keeps_denominators_separate() -> None:
    """八项指标的分母口径各不相同，聚合时不许合并。"""
    from backend.experiments.trajectory_eval import TrajectoryOutcome

    outs = [
        TrajectoryOutcome(item_id="A", flow="query", path_ok=True),
        TrajectoryOutcome(item_id="B", flow="query", error="boom"),
    ]
    agg = aggregate(outs)
    assert agg["n_scored"] == 1
    assert agg["n_errored"] == 1
    assert agg["path_correct"] == {"hits": 1, "n": 1}, "跑挂的那条不进分母"


def test_revision_metrics_only_count_revision_flows() -> None:
    from backend.experiments.trajectory_eval import TrajectoryOutcome

    outs = [
        TrajectoryOutcome(item_id="A", flow="query"),
        TrajectoryOutcome(item_id="B", flow="revision", revision_translation_ok=True),
    ]
    agg = aggregate(outs)
    assert agg["revision_translation"] == {"hits": 1, "n": 1}
