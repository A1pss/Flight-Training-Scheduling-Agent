"""`backend/routing/modifiers.py` 的单测（`Z-45`）。

**为什么这些要交给正则而不是模型**：M9-B 实测模型抽修饰 99 条里只抽到 8 条，
补上字段 description 后 20 条里从 3 条升到 4 条 —— 几乎没动。而线索是
**同一修饰换个前半句就抽不到**（说明没有稳定规则可依），且全集统计
**84 种表述、7 类**、高度模式化。可枚举的东西不该交给概率模型。
"""

from __future__ import annotations

import pytest

from backend.routing.modifiers import scan_modifiers


def kinds(text: str) -> list[str]:
    return [c.kind for c in scan_modifiers(text)]


@pytest.mark.parametrize(
    "text",
    [
        "下周给所有人排班，周三不要安排飞行",
        "把2026-W04的班排出来，周三不要安排飞行",
        "全员2026-W04的排班计划做一版，周三不要安排飞行",
    ],
)
def test_forbid_day_is_stable_across_phrasings(text: str) -> None:
    """★ 核心回归闸：**同一修饰、不同前半句，结果必须一致**。

    模型在这三条上的表现是 ✅❌❌ —— 正是「碰运气」的形态。
    """
    assert kinds(text) == ["FORBID"]
    assert scan_modifiers(text)[0].params == {"weekday": "周三"}


def test_forbid_day_reversed_word_order() -> None:
    assert kinds("排班时不要在周三安排飞行") == ["FORBID"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("每天最多排 3 个架次", 3),
        ("一天最多一个架次", 1),
        ("每日最多两个架次", 2),
    ],
)
def test_density_reads_the_number(text: str, expected: int) -> None:
    got = scan_modifiers(text)
    assert [c.kind for c in got] == ["REDUCE_DENSITY"]
    assert got[0].params == {"max_per_day": expected}


def test_pin_aircraft_collects_every_id() -> None:
    got = scan_modifiers("本周只用 AC10 和 AC27 给陈伟排班")
    assert [c.kind for c in got] == ["PIN_RESOURCE"]
    assert got[0].params == {"aircraft": ["AC10", "AC27"]}


@pytest.mark.parametrize("text", ["别用 AC73", "不要用 AC73", "避免用 AC73", "禁止用 AC73"])
def test_pin_aircraft_never_reverses_a_negative_instruction(text: str) -> None:
    """禁用不是指定使用；宁可留给 Planner，也不能生成方向相反的求解输入。"""
    assert scan_modifiers(text) == []


def test_pin_runway_and_time() -> None:
    assert kinds("JL-8 的架次都走 RWY-2") == ["PIN_RUNWAY"]
    assert kinds("都排在 08:00 之后") == ["PIN_TIME"]


@pytest.mark.parametrize(
    "text",
    [
        "RWY-2 下周三关闭，重新排一版",
        "本周每天 12:00 之后不飞，重排一版",
    ],
)
def test_pin_scanners_never_reverse_a_closure(text: str) -> None:
    """关闭跑道/时段是禁用，不得因出现编号或“之后”而翻成固定使用。"""
    assert not {"PIN_RUNWAY", "PIN_TIME"} & set(kinds(text))


def test_closed_runway_becomes_day_scoped_forbid() -> None:
    got = scan_modifiers("RWY-2 下周三关闭，重新排一版")
    assert [(c.kind, c.targets, c.params) for c in got] == [
        ("FORBID", ["ALL"], {"weekday": "周三", "runway_id": "RWY-2"})
    ]


def test_shift_window_morning() -> None:
    assert kinds("都安排在上午") == ["SHIFT_WINDOW"]


def test_no_modifier_returns_empty_not_a_guess() -> None:
    """**抓不到就返回空，绝不猜** —— 编一条约束比漏抓更糟，它会真的进求解器。"""
    assert scan_modifiers("给所有人排 2026-W02 的班") == []
    assert scan_modifiers("何超能不能排 missionB-1？") == []


def test_same_modifier_is_not_emitted_twice() -> None:
    """两种语序都命中同一条时只留一条。"""
    got = scan_modifiers("周三不要安排飞行，另外不要在周三排")
    assert len(got) == 1


def test_targets_are_all_not_a_guessed_person() -> None:
    """`targets` 一律 ["ALL"] —— 「周三不要安排飞行」不是针对某个人。
    **推错对象比漏抓更糟**，所以扫描器不去推断对象。"""
    got = scan_modifiers("本周用AC49给何超排班")
    assert got[0].targets == ["ALL"]


def test_origin_utterance_is_kept_for_audit() -> None:
    """v6 §7.3.4：原话要留着供撤销与审计、供 UI 回显确认。"""
    got = scan_modifiers("每天最多排 3 个架次")
    assert "最多" in got[0].origin_utterance
