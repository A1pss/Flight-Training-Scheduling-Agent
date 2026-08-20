"""`judge_calib_50` 的断言（v6 §12.4.1）。

★ 本集与其余八集的根本区别：**标签必须留空**。所以这里最重要的一条测试
不是「标得对不对」，而是「有没有人偷偷替业务方把标签填了」。
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

import pytest

from backend.datasets.loader import dataset_dir, load_eval_dataset
from backend.datasets.schemas import JudgeCalibItem, canonical_doc_id
from tests.datasets import calib_catalog, calib_sheet

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
        for entry in item.context_usage:
            assert entry.used is None, f"{item.item_id}/{entry.doc_id}"


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


def test_every_item_carries_context_text(items: list[JudgeCalibItem]) -> None:
    """★ 召回条目必须带**原文**，不能只有 doc id。

    Faithfulness 判的是「这条断言有没有被**召回内容**支撑」——
    只给 `ent:person:P04` 这样一个 id，标注者与 judge 都只能凭空判。
    """
    for item in items:
        assert item.retrieved_contexts, item.item_id
        for context in item.retrieved_contexts:
            assert context.snippet.strip(), f"{item.item_id}/{context.doc_id}"
            assert context.snippet != context.doc_id, (
                f"{item.item_id}/{context.doc_id} 的原文没还原出来，只回退成了 id"
            )


def test_context_usage_is_gold_intersect_top5(items: list[JudgeCalibItem]) -> None:
    """★ 上下文利用率的判定对象是 **gold ∩ Top-5**，两个限定缺一不可。

    没进 Top-5 的谈不上「用没用上」；不是 gold 的也不该算进这个指标。
    """
    for item in items:
        gold = {canonical_doc_id(d) for d in item.expected_doc_ids}
        top5 = item.retrieved_doc_ids[:5]
        expected = [d for d in top5 if canonical_doc_id(d) in gold]
        assert [c.doc_id for c in item.context_usage] == expected, item.item_id


def test_annotation_sheet_round_trips(tmp_path: Path, items: list[JudgeCalibItem]) -> None:
    """★ 标注表 → 合并回 jsonl 的往返：**填错要抛，不许静默丢**。

    一条被悄悄丢掉的标注会让分母对不上而没人发现 —— 而分母错了，
    一致率与 Kappa 全错。
    """
    directory = dataset_dir("judge_calib_50")
    rows = [
        json.loads(line)
        for line in (directory / "items.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    sheet = tmp_path / "sheet.csv"
    written = calib_sheet.write_annotation_sheet(sheet, rows)
    assert written == sum(
        sum(1 for c in i.claims if c.is_assertive) + len(i.context_usage) for i in items
    )

    # 填两行（一条断言、一条召回条目），其余留空。
    # ★ 必须用 csv 模块改：`contexts` 列里带换行（引号内），按行文本编辑会把 CSV 撕坏 ——
    # 这正是 W11 第一版测试踩的坑。
    with sheet.open("r", encoding="utf-8-sig", newline="") as handle:
        records = list(csv.DictReader(handle))
    claim_done = context_done = False
    for record in records:
        if record["kind"] == "claim" and not claim_done:
            record["verdict"] = "SUPPORTED"
            claim_done = True
        elif record["kind"] == "context" and not context_done:
            record["used"] = "Y"
            context_done = True
    with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(calib_sheet.HEADER))
        writer.writeheader()
        writer.writerows(records)

    target = tmp_path / "items.jsonl"
    target.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n",
        encoding="utf-8",
    )
    claims, contexts = calib_sheet.merge_annotations(sheet, target)
    assert (claims, contexts) == (1, 1)

    merged = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert any(c["verdict"] == "SUPPORTED" for r in merged for c in r["claims"])
    assert any(e["used"] is True for r in merged for e in r["context_usage"])


def test_merge_rejects_bad_values(tmp_path: Path) -> None:
    """枚举外的取值必须抛 —— 「SUPPORT」少一个 ED 也不行。"""
    sheet = tmp_path / "bad.csv"
    sheet.write_text(
        ",".join(calib_sheet.HEADER)
        + "\nclaim,JCAL-001,c1,high_risk,semantic,q,t,a,ctx,,SUPPORT,\n",
        encoding="utf-8-sig",
    )
    items = tmp_path / "items.jsonl"
    items.write_text(
        json.dumps(
            {"item_id": "JCAL-001", "claims": [{"claim_id": "c1"}], "context_usage": []},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="不在"):
        calib_sheet.merge_annotations(sheet, items)


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
