"""`memory_eval` 与 `report_harness` 的单测。

`memory_eval` 这边最要紧的是 **`Z-30` 的双 doc id 归一**：不归一会把路 A 的
命中全判成未召回，而 Recall@5 是验收主指标。
`report_harness` 这边钉住的是 **硬地板 x 按「调用」计而不是按「失败模式出现
次数」计** —— 两者在口径 B 上差 4 个百分点。
"""

from __future__ import annotations

import pytest

from backend.experiments.memory_eval import (
    PROC_ID_TEMPLATE,
    ProbeOutcome,
    _first_hit_rank,
    false_recall_rate,
    mrr_at_10,
    preference_doc_ids,
    recall_at_5,
)
from backend.experiments.report_harness import (
    predicted_retry_coefficient,
    request_level_failure,
    solve_r,
)


class _Row:
    def __init__(self, namespace: str, key: str) -> None:
        self.namespace = namespace
        self.key = key


# ── 召回 id 归一（Z-30）────────────────────────────────────────────────
def test_pg_and_ent_doc_ids_are_the_same_entity() -> None:
    """路 A 发 `pg:persons:P04`、语料发 `ent:person:P04` —— 同一个实体。

    直接按字符串比会把语义类最强的那一路命中全判成未召回。
    """
    assert _first_hit_rank(["pg:persons:P04"], ["ent:person:P04"], limit=5) == 1


def test_qualification_rows_normalise_to_the_person() -> None:
    assert _first_hit_rank(["pg:person_qualifications:P04:A"], ["ent:person:P04"], limit=5) == 1


def test_rank_is_one_indexed_and_zero_means_miss() -> None:
    assert _first_hit_rank(["x", "y", "ent:person:P04"], ["ent:person:P04"], limit=5) == 3
    assert _first_hit_rank(["x"], ["ent:person:P04"], limit=5) == 0


def test_hit_outside_top5_is_a_miss_at_5_but_counts_for_mrr10() -> None:
    docs = [f"d{i}" for i in range(6)] + ["ent:person:P04"]
    assert _first_hit_rank(docs, ["ent:person:P04"], limit=5) == 0
    assert _first_hit_rank(docs, ["ent:person:P04"], limit=10) == 7


# ── absent 探针是负例 ─────────────────────────────────────────────────
def test_absent_probes_stay_out_of_the_recall_denominator() -> None:
    """`absent` 是负例，正确行为是一条都不召回。混进分母会让 Recall 无端变低。"""
    outs = [
        ProbeOutcome(
            item_id="a",
            memory_type="semantic",
            probe_kind="fact",
            variant="main",
            expected_doc_ids=["x"],
            first_hit_rank_at5=1,
        ),
        ProbeOutcome(
            item_id="b",
            memory_type="semantic",
            probe_kind="absent",
            variant="main",
            expected_doc_ids=[],
        ),
    ]
    assert recall_at_5(outs) == (1, 1)


def test_false_recall_counts_only_absent_probes() -> None:
    outs = [
        ProbeOutcome(
            item_id="b",
            memory_type="semantic",
            probe_kind="absent",
            variant="main",
            expected_doc_ids=[],
            retrieved_doc_ids=["something"],
        ),
        ProbeOutcome(
            item_id="c",
            memory_type="semantic",
            probe_kind="absent",
            variant="main",
            expected_doc_ids=[],
        ),
    ]
    assert false_recall_rate(outs) == (1, 2)


def test_errored_probe_is_excluded_not_counted_as_miss() -> None:
    """跑挂的探针不进分母 —— 混进去会把运维事故记成召回能力下降（M7 §1.3 同款口径）。"""
    outs = [
        ProbeOutcome(
            item_id="a",
            memory_type="semantic",
            probe_kind="fact",
            variant="main",
            expected_doc_ids=["x"],
            first_hit_rank_at5=1,
        ),
        ProbeOutcome(
            item_id="e",
            memory_type="semantic",
            probe_kind="fact",
            variant="main",
            expected_doc_ids=["x"],
            error="boom",
        ),
    ]
    assert recall_at_5(outs) == (1, 1)


def test_mrr_uses_reciprocal_rank() -> None:
    outs = [
        ProbeOutcome(
            item_id="a",
            memory_type="semantic",
            probe_kind="fact",
            variant="main",
            expected_doc_ids=["x"],
            first_hit_rank_at10=2,
        ),
        ProbeOutcome(
            item_id="b",
            memory_type="semantic",
            probe_kind="fact",
            variant="main",
            expected_doc_ids=["x"],
            first_hit_rank_at10=0,
        ),
    ]
    assert mrr_at_10(outs) == pytest.approx(0.25)


def test_mrr_refuses_empty_set() -> None:
    with pytest.raises(ValueError, match="空集不产出"):
        mrr_at_10([])


def test_procedural_doc_ids_follow_the_dataset_convention() -> None:
    """M9-A §7 第 1 条点名要补的适配：`proc:<namespace>/<key>`。"""
    assert preference_doc_ids([_Row("relaxation", "order")]) == ["proc:relaxation/order"]
    assert PROC_ID_TEMPLATE.format(namespace="a", key="b") == "proc:a/b"


# ── §12.5.1 的三条推导 ────────────────────────────────────────────────
def test_backsolved_r_reproduces_m7_context_measurement() -> None:
    """M7 口径 B 实测 p=0.6683 / final=0.9250 → r≈0.5245，
    与 §12.5.1 那张表「本表取值 0.55」几乎一致。"""
    r = solve_r(0.6683, 0.9250)
    assert r is not None
    assert r == pytest.approx(0.5245, abs=1e-3)


def test_r_is_undefined_when_first_pass_is_perfect() -> None:
    """p=1 时没有失败可纠正，`r` 无定义 —— 返回 None 而不是 1.0。"""
    assert solve_r(1.0, 1.0) is None
    assert predicted_retry_coefficient(1.0, None) == pytest.approx(1.0)


def test_request_level_compounding_matches_the_spec_example() -> None:
    """§12.5.1：调用级 3% 失败 → 请求级 ≈8.7%。
    只报 97% 会让人以为每百请求 3 个出问题，实际是 9 个。"""
    assert request_level_failure(0.03) == pytest.approx(0.0873, abs=1e-4)


def test_retry_coefficient_formula_matches_m7_production() -> None:
    """p=0.995、r=1.0 → 重试系数 1.005（M7 实测逐位吻合）。"""
    assert predicted_retry_coefficient(0.995, 1.0) == pytest.approx(1.005, abs=1e-6)
