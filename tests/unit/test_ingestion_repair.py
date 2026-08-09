"""修复层单测（v6 §1.5 / §5.2）。

**TOKEN_PATTERNS 五条正则逐条各有正例与反例** —— 反例尤其重要：一条写得太宽的
正则会把 `commission7`、`emission5` 这类无关文本改坏，而那种破坏在最终产物里
极难追溯。
"""

from __future__ import annotations

import pytest

from backend.core.errors import ErrorCode, IngestionError
from backend.ingestion.repair import (
    TOKEN_PATTERNS,
    aggregate_rows,
    apply_token_patterns,
    assert_no_orphan_tokens,
    dehyphenate_linebreaks,
    extract_mission_tokens,
    is_null_token,
    join_wrapped_words,
    normalize_separators,
    normalize_widths,
    repair_cell,
    repair_linebreaks,
    repair_text,
    split_list,
    strip_cjk_linebreaks,
)


def test_there_are_exactly_five_token_patterns() -> None:
    """v6 §5.2 明确是五条；少一条说明有人删了 missionC1 那条（X2 会复发）。"""
    assert len(TOKEN_PATTERNS) == 5


# ── 五条正则：逐条正例 ────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected", "which"),
    [
        ("mis\nsionB-1", "missionB-1", "第1条 mis+sion 切点"),
        ("mis sionB-1", "missionB-1", "第1条 空格变体"),
        ("mission\nC-1", "missionC-1", "第2条 mission+C-1 切点"),
        ("mi\nssionF-1", "missionF-1", "第3条 mi+ssion 切点"),
        ("ssionF-1", "missionF-1", "第4条 裸 ssion 残片"),
        ("missionC1", "missionC-1", "第5条 X2 缺连字符变体"),
    ],
)
def test_token_patterns_positive(raw: str, expected: str, which: str) -> None:
    assert apply_token_patterns(raw) == expected, which


# ── 五条正则：反例（不该被改的文本） ──────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "missionA-1",  # 已经合法，幂等
        "commission7",  # 含 mission 但不是课目编号（第5条不得误伤）
        "emission5",  # 同上
        "submission",  # 无数字
        "missionI-1",  # I 不在 A-H 范围内
        "missionA-12",  # 两位数字，不是合法编号形态
        "transmission1",  # `mission1` 前面粘着别的词，\b 应拦住
    ],
)
def test_token_patterns_negative(text: str) -> None:
    assert apply_token_patterns(text) == text


def test_pattern5_does_not_touch_two_digit_suffix() -> None:
    """`missionA-12` 里的 `A-1` 已带连字符，第 5 条不该再插一个。"""
    assert apply_token_patterns("missionA-12") == "missionA-12"


# ── v6 §5.2 原函数 ───────────────────────────────────────────────────
def test_repair_linebreaks_matches_v6_signature() -> None:
    """v6 给出的 `repair_linebreaks` 行为保持原样：中文换行 + 五条正则。"""
    assert repair_linebreaks("定检\n维护") == "定检维护"
    assert repair_linebreaks("mis\nsionB-1") == "missionB-1"


def test_strip_cjk_linebreaks_only_between_cjk() -> None:
    assert strip_cjk_linebreaks("定检\n维护") == "定检维护"
    # 数字与中文之间不是「中文之间」，不删
    assert strip_cjk_linebreaks("2026\n年") == "2026\n年"


# ── 顺序：去连字符必须先于第 5 条正则（X2 的因果链） ──────────────────
def test_dehyphenate_produces_the_x2_variant_then_pattern5_fixes_it() -> None:
    """`missionC-\\n1` →(去连字符)→ `missionC1` →(第5条)→ `missionC-1`。

    这条链是 §5.5 X2 在真实 `aircraft.pdf` 上的完整因果，顺序反了第 5 条就成
    死代码。
    """
    raw = "missionC-\n1"
    dehyphenated = dehyphenate_linebreaks(raw)
    assert dehyphenated == "missionC1"
    assert apply_token_patterns(dehyphenated) == "missionC-1"
    assert repair_cell(raw) == "missionC-1"


def test_dehyphenate_does_not_break_dates() -> None:
    """`2026-01-\\n09` 是数字-连字符-数字，不该被当作断词拼接。"""
    assert dehyphenate_linebreaks("2026-01-\n09") == "2026-01-\n09"
    assert repair_cell("2026-01-\n09") == "2026-01-09"


def test_join_wrapped_words_covers_what_patterns_miss() -> None:
    """第 1~4 条覆盖不到的切点：`missi|onB-1`、`missio|nH-1`、`2026|-01-07`。"""
    assert join_wrapped_words("missi\nonB-1") == "missionB-1"
    assert join_wrapped_words("missio\nnH-1") == "missionH-1"
    assert join_wrapped_words("2026\n-01-07") == "2026-01-07"


# ── 全半角归一化 ──────────────────────────────────────────────────────
def test_normalize_widths_folds_fullwidth_to_ascii() -> None:
    assert normalize_widths("ＡＣ１０") == "AC10"
    assert normalize_widths("（硬约束）") == "(硬约束)"
    # 顿号与书名号没有半角等价物，原样保留
    assert normalize_widths("A、B") == "A、B"
    assert normalize_widths("【硬约束】") == "【硬约束】"
    # ≥ 不可分解，必须活下来（频率解析依赖它）
    assert normalize_widths("每7天≥1次") == "每7天≥1次"


def test_normalize_separators_unifies_list_delimiters() -> None:
    assert normalize_separators("a,b;c、d") == "a、b、c、d"


# ── 单元格与列表 ─────────────────────────────────────────────────────
def test_repair_cell_collapses_residual_newlines() -> None:
    """`20周,每14天≥1\\n次` 必须拼成一句，否则频率正则匹配不到。"""
    assert repair_cell("20周，每14天≥1\n次") == "20周,每14天≥1次"


def test_repair_text_is_idempotent() -> None:
    once = repair_text("missionA-1、mis\nsionB-1")
    assert repair_text(once) == once


@pytest.mark.parametrize("token", ["—", "-", "", "无", "N/A"])
def test_is_null_token(token: str) -> None:
    assert is_null_token(token)


def test_split_list_slash_only_when_allowed() -> None:
    assert split_list("JL-8/JL-9", allow_slash=True) == ["JL-8", "JL-9"]
    # 不允许拆斜杠时，`IFR Route` 这类含斜杠的名字不能被切碎
    assert split_list("JL-8/JL-9") == ["JL-8/JL-9"]


def test_split_list_dedupes_and_keeps_order() -> None:
    assert split_list("b、a、b") == ["b", "a"]


def test_extract_mission_tokens_from_real_broken_cell() -> None:
    """personnel.pdf 里教员那一格的真实形态（12 门全被硬换行打散）。"""
    cell = (
        "missionA-1、missionA-2、mis\nsionB-1、missionB-2、mission\n"
        "C-1、missionC-2、missionD-1\n、missionE-1、missionE-2、mi\n"
        "ssionF-1、missionG-1、missio\nnH-1"
    )
    assert extract_mission_tokens(cell) == [
        "missionA-1",
        "missionA-2",
        "missionB-1",
        "missionB-2",
        "missionC-1",
        "missionC-2",
        "missionD-1",
        "missionE-1",
        "missionE-2",
        "missionF-1",
        "missionG-1",
        "missionH-1",
    ]


# ── 跨行单元格聚合 ───────────────────────────────────────────────────
def test_aggregate_rows_merges_continuation_lines() -> None:
    rows = [
        ["P01", "孙军", "missionA-1"],
        ["", "", "missionA-2"],
        ["P02", "高超", "missionB-1"],
    ]
    assert aggregate_rows(rows) == [
        ["P01", "孙军", "missionA-1\nmissionA-2"],
        ["P02", "高超", "missionB-1"],
    ]


def test_aggregate_rows_pads_short_continuation() -> None:
    rows = [["P01", "孙军", "a"], ["", "", "b", "尾列"]]
    assert aggregate_rows(rows) == [["P01", "孙军", "a\nb", "尾列"]]


def test_aggregate_rows_keeps_leading_orphan_row() -> None:
    """首行主键就为空时不能丢，否则会静默少一条记录。"""
    assert aggregate_rows([["", "x"]]) == [["", "x"]]


# ── 后置断言 ─────────────────────────────────────────────────────────
def test_assert_no_orphan_tokens_passes_on_clean_records() -> None:
    assert_no_orphan_tokens([{"missions": ["missionA-1", "missionH-1"]}])


def test_assert_no_orphan_tokens_blocks_dirty_token() -> None:
    """铁律 7：宁可阻断，也不让 `sionB-1` 进库。"""
    with pytest.raises(IngestionError) as exc:
        assert_no_orphan_tokens([{"source": "person:P01", "missions": ["sionB-1"]}])
    assert exc.value.code is ErrorCode.PDF_REPAIR_ASSERTION_FAILED
    assert exc.value.details["orphan_tokens"] == ["sionB-1"]


def test_assert_no_orphan_tokens_ignores_records_without_missions() -> None:
    assert_no_orphan_tokens([{"source": "x"}, {"missions": "不是列表"}])
