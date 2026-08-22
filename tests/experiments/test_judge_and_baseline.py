"""`judge.py` / `baseline_llm.py` / `acceptance.py` 的单测。

judge 这边最要紧的是**解析失败要排除出分母**：把解析不了的判定记成
`NOT_SUPPORTED` 会凭空制造分歧，算出来的就不再是「judge 与人的一致率」了。
"""

from __future__ import annotations

import pytest

from backend.experiments.acceptance import interval_str, pct, verdict_of
from backend.experiments.baseline_llm import extract_json
from backend.experiments.judge import (
    JUDGE_MODEL,
    VERDICTS,
    build_faithfulness_request,
    build_usage_request,
    context_utilisation,
    faithfulness,
    parse_used,
    parse_verdict,
)


# ── judge ────────────────────────────────────────────────────────────
def test_judge_uses_the_32b_model_named_in_the_spec() -> None:
    assert JUDGE_MODEL == "qwen2.5:32b-instruct-q4_K_M"


def test_faithfulness_request_is_temperature_zero_and_constrained() -> None:
    """§12.4.1：温度 0、受约束解码到三分类。"""
    req = build_faithfulness_request(
        "刘斌 C 类 2026-01-07 到期", [{"snippet": "刘斌 C 类到期 2026-01-07"}]
    )
    assert req.temperature == 0.0
    assert req.format_schema is not None
    assert req.format_schema["properties"]["verdict"]["enum"] == list(VERDICTS)


def test_request_carries_the_context_text_not_just_ids() -> None:
    """判的是「有没有被召回内容支撑」，只给 doc_id 等于让 judge 凭空判
    （M9-A §3.9.3 补原文正是为此）。"""
    req = build_faithfulness_request("断言", [{"snippet": "这是召回原文"}])
    assert "这是召回原文" in req.messages[-1]["content"]


def test_empty_contexts_are_stated_not_silently_dropped() -> None:
    req = build_faithfulness_request("断言", [])
    assert "没有召回到任何内容" in req.messages[-1]["content"]


def test_usage_request_contains_both_answer_and_snippet() -> None:
    req = build_usage_request("回答正文", "召回条目")
    body = req.messages[-1]["content"]
    assert "回答正文" in body and "召回条目" in body


@pytest.mark.parametrize("verdict", VERDICTS)
def test_parse_verdict_accepts_all_three_labels(verdict: str) -> None:
    assert parse_verdict(f'{{"verdict":"{verdict}"}}') == verdict


def test_unparseable_verdict_becomes_empty_not_not_supported() -> None:
    """★ 解析失败**不能**记成 NOT_SUPPORTED —— 那会凭空制造与人工的分歧。"""
    assert parse_verdict("这不是 JSON") == ""
    assert parse_verdict('{"verdict":"MAYBE"}') == ""


def test_unparsed_rows_stay_out_of_the_faithfulness_denominator() -> None:
    assert faithfulness(["SUPPORTED", "PARTIAL", "", "SUPPORTED"]) == (2, 3)


def test_only_supported_counts_toward_faithfulness() -> None:
    """§12.4.1：**只有 `SUPPORTED` 计入分子**，PARTIAL 不算。"""
    assert faithfulness(["PARTIAL", "PARTIAL"]) == (0, 2)


def test_parse_used_requires_a_real_boolean() -> None:
    assert parse_used('{"used":true}') is True
    assert parse_used('{"used":false}') is False
    assert parse_used('{"used":"yes"}') is None
    assert parse_used("nope") is None


def test_context_utilisation_counts_the_unused_ones() -> None:
    """⚠️ 方向：分子是**没用上**的那些（定义栏「召回了正确内容却没用上的比例」，
    目标 ≤18%）。名字叫「利用率」但目标是「越小越好」，容易读反。"""
    assert context_utilisation([True, True, False, None]) == (1, 3)


# ── baseline_llm ─────────────────────────────────────────────────────
def test_extract_json_handles_fenced_output() -> None:
    assert extract_json('```json\n{"sorties": []}\n```') == {"sorties": []}


def test_extract_json_does_not_repair_broken_json() -> None:
    """**不做任何纠错** —— 补引号、删尾逗号属于替模型把活干了，
    会把「LLM 直接排班」测成「LLM + 一个修复器」。"""
    assert extract_json('{"sorties": [,]}') is None
    assert extract_json("完全没有 JSON") is None


def test_extract_json_ignores_surrounding_prose() -> None:
    assert extract_json('好的，这是计划：\n{"sorties": []}\n希望有帮助') == {"sorties": []}


# ── acceptance ───────────────────────────────────────────────────────
def test_missing_metric_renders_as_not_run_not_zero() -> None:
    """铁律 6：跑不出来写「未跑」，**不写 0、不写估计值**。"""
    assert pct(None) == "未跑"
    assert verdict_of(None, 0.92) == "⬜ 未跑"
    assert interval_str(None) == "未跑"


def test_verdict_respects_direction() -> None:
    assert verdict_of(0.95, 0.92) == "✅ 达标"
    assert verdict_of(0.90, 0.92) == "❌ 未达标"
    assert verdict_of(0.03, 0.04, higher_is_better=False) == "✅ 达标"
    assert verdict_of(0.09, 0.04, higher_is_better=False) == "❌ 未达标"


def test_nan_interval_renders_as_not_applicable() -> None:
    """分母为 0 的那些（某一类一条样本都没有）要显式写「不适用」。"""
    out = interval_str({"point": float("nan"), "low": float("nan"), "high": float("nan"), "n": 0})
    assert out == "不适用"
