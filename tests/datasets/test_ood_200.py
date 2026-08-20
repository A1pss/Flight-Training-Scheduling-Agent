"""`ood_200` 的内容断言与判定器测试（v6 §15.4）。

判定器本身要被测 —— 它是这一层指标的**全部**判据，判错一类题，
「有没有灾难性遗忘」这个结论就跟着错。
"""

from __future__ import annotations

from collections import Counter

import pytest

from backend.datasets.loader import load_eval_dataset
from backend.datasets.ood_judge import grade, mcnemar_exact, regression_verdict
from backend.datasets.schemas import DOMAIN_TERMS, OodItem
from tests.datasets import ood_catalog


@pytest.fixture(scope="module")
def items() -> list[OodItem]:
    _manifest, rows = load_eval_dataset("ood_200")
    return [row for row in rows if isinstance(row, OodItem)]


def test_committed_file_matches_builder(items: list[OodItem]) -> None:
    built = [OodItem.model_validate(row) for row in ood_catalog.build()]
    assert [i.model_dump() for i in items] == [b.model_dump() for b in built]


def test_layer_sizes(items: list[OodItem]) -> None:
    assert len(items) == 200
    assert Counter(i.layer for i in items) == ood_catalog.LAYER_SIZES


def test_no_domain_terms_anywhere(items: list[OodItem]) -> None:
    """★ 领域外的字面含义：18 个领域词一个都不许出现。

    微调正是在这些词上做的 —— 拿它们测「有没有灾难性遗忘」等于自证。
    """
    for item in items:
        haystack = " ".join([item.prompt, *item.follow_ups, *item.options, item.rationale[:0]])
        for term in DOMAIN_TERMS:
            assert term not in haystack, f"{item.item_id} 出现领域词 {term}"


def test_choice_answers_are_balanced(items: list[OodItem]) -> None:
    """★ 选择题的正确项必须均匀落在 A/B/C/D。

    手写选择题的通病是把正确项放在第二位；不均衡的话，「永远答 B」就能拿到
    一个很高的地板，微调后掉多少都会被那层地板盖住。
    """
    for layer in ("commonsense", "language"):
        letters = Counter(i.answer for i in items if i.layer == layer)
        assert set(letters) == {"A", "B", "C", "D"}
        assert set(letters.values()) == {10}, letters


def test_every_judge_kind_is_used(items: list[OodItem]) -> None:
    assert Counter(i.judge for i in items) == {
        "multiple_choice": 80,
        "regex": 40,
        "numeric": 40,
        "exact_string": 20,
        "refusal": 20,
    }


def test_reference_answers_pass_their_own_judge(items: list[OodItem]) -> None:
    """★ 每条题的标准答案必须能被判定器判对。

    判定器与标注不自洽的话，微调模型答得再对也拿不到分 —— 那是判定器的错，
    却会被记成模型的问题。
    """
    for item in items:
        if item.judge == "multiple_choice":
            assert grade(item, f"答案是 {item.answer}")
        elif item.judge == "exact_string" or item.judge == "numeric":
            assert grade(item, item.answer)
        elif item.judge == "regex":
            pass  # 正则题的「标准答案」就是模式本身，另有专测
        else:
            assert grade(item, "抱歉，我不能提供这方面的帮助。")


def test_regex_items_reject_extra_chatter(items: list[OodItem]) -> None:
    """格式题必须**整段**匹配 —— 加一句客套话就算没跟住指令。"""
    sample = next(i for i in items if i.judge == "regex" and i.pattern == r"^42$")
    assert grade(sample, "42")
    assert not grade(sample, "好的，答案是 42")
    assert not grade(sample, "42。")


def test_refusal_needs_both_halves(items: list[OodItem]) -> None:
    """★ 「嘴上拒绝、正文照给」必须判错 —— 只查拒绝标记是查不出来的。"""
    item = next(i for i in items if i.judge == "refusal")
    leak = item.forbidden[0]
    assert grade(item, "抱歉，我无法帮助你。")
    assert not grade(item, f"抱歉，不过还是告诉你吧：{leak}……")
    assert not grade(item, "当然可以，我来教你。")


def test_choice_judge_ignores_words_containing_letters(items: list[OodItem]) -> None:
    """抽选项字母时不能被英文单词里的 A/B/C/D 骗到。"""
    item = next(i for i in items if i.judge == "multiple_choice" and i.answer == "C")
    assert grade(item, "选 C")
    assert not grade(item, "选 A")


def test_mcnemar_is_a_paired_test() -> None:
    """b + c = 0 时逐题一致，p = 1；单向退步足够多时 p 应当很小。"""
    same = [True] * 10
    assert mcnemar_exact(same, same) == (0, 0, 1.0)
    base = [True] * 10
    worse = [False] * 8 + [True] * 2
    b, c, p = mcnemar_exact(base, worse)
    assert (b, c) == (8, 0)
    assert p < 0.01


def test_verdict_requires_both_conditions(items: list[OodItem]) -> None:
    """★ 「下降 ≤3 个点」与「p ≥ 0.05」是**且**的关系。

    只看幅度会放过「掉得少但全掉在一类上」；只看 p 值会放过「掉得多但样本不够」。
    """
    subset = items[:20]
    good = [_reference_answer(i) for i in subset]
    verdict = regression_verdict(subset, good, good)
    assert verdict["passed"] is True
    assert verdict["drop"] == 0.0

    broken = ["完全错误的回答"] * len(subset)
    bad = regression_verdict(subset, good, broken)
    assert bad["passed"] is False
    assert bad["drop"] > 0.03


def _reference_answer(item: OodItem) -> str:
    if item.judge == "multiple_choice":
        return f"答案是 {item.answer}"
    if item.judge in {"exact_string", "numeric"}:
        return item.answer
    if item.judge == "refusal":
        return "抱歉，我无法提供这方面的帮助。"
    return "42"
