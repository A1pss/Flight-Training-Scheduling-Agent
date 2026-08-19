"""`judge_calib_50` 的断言（v6 §12.4.1）。

★ 本集与其余八集的根本区别：**标签必须留空**。所以这里最重要的一条测试
不是「标得对不对」，而是「有没有人偷偷替业务方把标签填了」。
"""

from __future__ import annotations

from collections import Counter

import pytest

from backend.datasets.loader import dataset_dir, load_eval_dataset
from backend.datasets.schemas import JudgeCalibItem, canonical_doc_id
from tests.datasets import calib_catalog

pytestmark = pytest.mark.skipif(
    not (dataset_dir("judge_calib_50") / "items.jsonl").exists(),
    reason="judge_calib_50 尚未生成（需要先跑 run_probes.py 冻结 320 条回答）",
)


@pytest.fixture(scope="module")
def items() -> list[JudgeCalibItem]:
    _manifest, rows = load_eval_dataset("judge_calib_50")
    return [row for row in rows if isinstance(row, JudgeCalibItem)]


def test_fifty_items(items: list[JudgeCalibItem]) -> None:
    assert len(items) == 50


def test_labels_are_blank(items: list[JudgeCalibItem]) -> None:
    """★ 最重要的一条：交付时标签必须全空。

    §12.4.1 的「一处例外」——这一集是给 judge 当基准真值的，用 LLM 生成初稿
    会把要验证的偏差直接引进基准里。填了标签就等于自己给自己判卷。
    """
    for item in items:
        for claim in item.claims:
            assert claim.verdict is None, f"{item.item_id}/{claim.claim_id}"
            assert claim.context_used is None, f"{item.item_id}/{claim.claim_id}"


def test_stratification_is_not_all_positive(items: list[JudgeCalibItem]) -> None:
    """★ 「不能全是正例」——全正例算出来的一致率是虚高的，等于没验证。"""
    strata = Counter(i.stratum for i in items)
    assert strata["high_risk"] == 25, strata
    assert strata["regular"] == 25, strata


def test_regular_layer_is_stratified_by_memory_type(items: list[JudgeCalibItem]) -> None:
    regular = Counter(i.memory_type for i in items if i.stratum == "regular")
    assert regular == {"semantic": 10, "episodic": 9, "procedural": 6}, regular


def test_high_risk_items_have_a_reason(items: list[JudgeCalibItem]) -> None:
    """高风险层的每一条都要说得出「为什么高风险」—— 信号或合成负例标记。"""
    for item in items:
        if item.stratum == "high_risk":
            assert item.risk_signals or item.is_synthetic_negative, item.item_id


def test_synthetic_negatives_are_flagged_and_explained(items: list[JudgeCalibItem]) -> None:
    """合成负例必须标出来并写明扰动方式 —— 报一致率时要与真实样本分开。"""
    for item in items:
        if item.is_synthetic_negative:
            assert item.perturbation, item.item_id


def test_every_item_has_at_least_one_claim(items: list[JudgeCalibItem]) -> None:
    for item in items:
        assert item.claims, item.item_id
        assert all(c.text.strip() for c in item.claims), item.item_id


def test_non_assertive_fragments_are_flagged(items: list[JudgeCalibItem]) -> None:
    """★ 非陈述片段要标出来，且不能占满某一条。

    「检索到以下相关内容：」「请问是哪一个？」这类片段没有「有没有被召回支撑」
    可言。把它们混进一致率的分母，会让 judge 与人在一堆无意义的格子上
    「达成一致」，把一致率抬高 —— 那正是 §12.4.1 说的虚高。
    判据是**纯机械**的（疑问收尾 / 冒号收尾），不涉及对内容的判断。
    """
    for item in items:
        assert any(c.is_assertive for c in item.claims), f"{item.item_id} 全是非陈述片段"


def test_probe_ids_are_unique(items: list[JudgeCalibItem]) -> None:
    """★ 50 条要来自 50 个不同的探针 —— 同一条抽两次等于样本量虚标。"""
    ids = [i.probe_id for i in items]
    assert len(set(ids)) == len(ids)


def test_doc_id_normalization_is_applied_in_signals() -> None:
    """★ 分层信号必须先归一 `pg:` 与 `ent:` 两种形态。

    路 A 发 `pg:persons:P04`、语料发 `ent:person:P04`。不归一的话，
    语义类探针**最强的那一路命中**会被判成未召回，高风险层会被假阳性灌满。
    """
    record = {
        "item_id": "MEM-SEM-001",
        "answer": "刘斌的 C 类资质到期日是 2026-01-07。",
        "expected_doc_ids": ["ent:person:P04"],
        "retrieved_doc_ids": ["pg:persons:P04"],
        "supported_ratio": 1.0,
    }
    assert canonical_doc_id("pg:persons:P04") == "ent:person:P04"
    assert "recall_miss" not in calib_catalog.risk_signals(record)
