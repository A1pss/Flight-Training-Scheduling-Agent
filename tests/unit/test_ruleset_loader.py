"""`backend/core/ruleset.py`：ruleset / semantics 的类型化加载。"""

from __future__ import annotations

from datetime import time

import pytest

from backend.core.errors import RuleParseError, SemanticsUnconfirmedError
from backend.core.ruleset import (
    EXPECTED_RULE_IDS,
    IDENTITY_INSTRUCTOR,
    IDENTITY_MATURE,
    IDENTITY_STUDENT,
    LEVEL_DUAL,
    LEVEL_INSTRUCTOR,
    LEVEL_SOLO,
    REQUIRED_SWITCHES,
    get_ruleset,
    get_semantics,
    load_ruleset,
    load_semantics,
    parse_ruleset,
    parse_semantics,
    req_max_for,
)
from backend.models.entities import IDENTITIES, QUAL_LEVELS


def test_ruleset_has_all_14_rules() -> None:
    rules = load_ruleset()
    assert rules.version == "1.3.0"
    assert rules.rule_count == 14
    assert sorted(rules.rules) == list(EXPECTED_RULE_IDS)


def test_rule_params_typed_correctly() -> None:
    """规格参数一个都不许在代码里另写一份，全部从 YAML 取。"""
    rules = load_ruleset()
    assert rules.window_start == time(6, 0)
    assert rules.window_end == time(18, 0)
    assert rules.expiry_inclusive is True  # 约束2：到期日当日仍可执行
    assert rules.weekly_class_min == 1  # 约束3 + S-02
    assert (rules.min_gap_minutes, rules.rest_after_n, rules.rest_minutes) == (10, 2, 30)
    assert (rules.density_window_minutes, rules.density_window_cap) == (20, 2)
    assert rules.separation_minutes == 7
    assert rules.density_window_scope == "per_runway"  # D-2
    assert rules.separation_scope == "airport_wide"  # D-2：**不是** per_runway
    assert (rules.daily_minutes_default, rules.daily_minutes_student) == (480, 240)
    assert (rules.weekly_sorties_default, rules.weekly_sorties_student) == (12, 10)
    assert (rules.daily_sorties_per_person, rules.daily_sorties_per_aircraft) == (3, 6)
    assert rules.recurrent_window_days == 7  # S-11


def test_identity_dependent_caps() -> None:
    rules = load_ruleset()
    assert rules.daily_minute_cap(IDENTITY_STUDENT) == 240
    assert rules.daily_minute_cap(IDENTITY_INSTRUCTOR) == 480
    assert rules.daily_minute_cap(IDENTITY_MATURE) == 480
    assert rules.weekly_sortie_cap(IDENTITY_STUDENT) == 10
    assert rules.weekly_sortie_cap(IDENTITY_INSTRUCTOR) == 12


def test_r0_is_never_relaxable() -> None:
    """R0 恒不可松弛（v6 §3.10，代码层硬编码禁止）。"""
    rules = load_ruleset()
    r0 = [rid for rid in EXPECTED_RULE_IDS if rules.tier_of(rid) == "R0"]
    assert set(r0) == {1, 2, 4, 5, 6, 7, 8, 9, 14}
    for rid in r0:
        assert rules.is_relaxable(rid) is False
    for rid in (3, 13):
        assert rules.tier_of(rid) == "R2"
        assert rules.is_relaxable(rid) is True
    for rid in (10, 11, 12):
        assert rules.tier_of(rid) == "R1"
        assert rules.is_relaxable(rid) is True


def test_relaxation_ladder_never_touches_r0() -> None:
    rules = load_ruleset()
    assert [step.tier for step in rules.ladder] == [0, 1, 2, 3]
    assert rules.ladder_step(1).relaxes == (13,)
    assert set(rules.ladder_step(2).relaxes) == {3, 13}  # D-6
    assert set(rules.ladder_step(3).relaxes) == {3, 10, 11, 12, 13}
    for step in rules.ladder:
        for rid in step.relaxes:
            assert rules.tier_of(rid) != "R0"


def test_ladder_step_rejects_unknown_tier() -> None:
    with pytest.raises(RuleParseError, match="Tier 9"):
        load_ruleset().ladder_step(9)


def test_req_max_formula() -> None:
    """约束14：`req_max = ceil(7 / freq_days)`（v6 §3.2）。"""
    assert req_max_for(3) == 3  # A 类
    assert req_max_for(7) == 1  # B~F 类
    assert req_max_for(14) == 1  # G/H 类
    with pytest.raises(RuleParseError):
        req_max_for(0)


def test_semantics_all_13_switches_decided() -> None:
    sem = load_semantics()
    assert sorted(sem.switches) == sorted(REQUIRED_SWITCHES)
    assert sem.snapshot()["S-02"] == "class_level"


def test_semantics_typed_accessors_match_v6_decisions() -> None:
    sem = load_semantics()
    assert sem.s01_class_needs_all is True  # S-01 该类全部课目完成
    assert sem.s02_class_level is True  # S-02 A 类整体 ≥1 次
    assert sem.s03_incomplete_only is True  # S-03 仅未完成课目受约束13
    assert sem.s04_half_open is True  # S-04 [t, t+20)
    assert sem.s05_dual_runway is True  # S-05 双跑道
    assert sem.s05_density_scope["window_20min"] == "per_runway"
    assert sem.s05_density_scope["separation_7min"] == "airport_wide"  # D-2
    assert sem.s06_landing_to_takeoff is True  # S-06
    assert sem.s07_same_day_only is True  # S-07
    assert sem.s08_students_only is True  # S-08
    assert sem.s09_instructors_exempt is True and sem.s09_mature_recurrent is True
    assert sem.s10_airspace_hard is True  # S-10
    assert sem.s11_enabled is True and sem.s11_identities == (IDENTITY_MATURE,)
    assert (sem.s11_window_days, sem.s11_start_offset_days) == (7, 1)
    assert sem.s12_from_week_monday is True
    assert sem.s12_count_as_debt is False  # S-12：锚点缺失**不计欠账**
    assert sem.s13_all_students is True  # S-13


def test_identity_and_level_constants_match_orm() -> None:
    """身份/等级是**规格的一部分**（§5.1.1），与 ORM 的已知集合必须一致。"""
    assert {IDENTITY_INSTRUCTOR, IDENTITY_MATURE, IDENTITY_STUDENT} == set(IDENTITIES)
    assert {LEVEL_INSTRUCTOR, LEVEL_SOLO, LEVEL_DUAL} == set(QUAL_LEVELS)


def test_missing_switch_is_blocked() -> None:
    sem = load_semantics()
    raw = {
        "semantics_version": "x",
        "switches": {sid: dict(sem.switches[sid]) for sid in REQUIRED_SWITCHES[:-1]},
    }
    with pytest.raises(SemanticsUnconfirmedError, match="S-13"):
        parse_semantics(raw)


def test_undecided_extra_switch_is_blocked() -> None:
    """新增一条未裁定的开关即 FTS-1002（v6 §9.3），不许静默接受。"""
    sem = load_semantics()
    raw = {
        "semantics_version": "x",
        "switches": {
            **{sid: dict(sem.switches[sid]) for sid in REQUIRED_SWITCHES},
            "S-99": {"value": "whatever"},
        },
    }
    with pytest.raises(SemanticsUnconfirmedError, match="S-99"):
        parse_semantics(raw)


def test_switch_value_outside_options_is_blocked() -> None:
    sem = load_semantics()
    switches = {sid: dict(sem.switches[sid]) for sid in REQUIRED_SWITCHES}
    switches["S-02"]["value"] = "made_up"
    with pytest.raises(SemanticsUnconfirmedError, match="S-02"):
        parse_semantics({"semantics_version": "x", "switches": switches})


def test_ruleset_rejects_relaxable_r0() -> None:
    raw = {
        "ruleset_version": "x",
        "rules": [{"id": i, "tier": "R0", "params": {}} for i in EXPECTED_RULE_IDS],
        "tiers": {"R0": {"relaxable": True}},
        "relaxation_ladder": [{"tier": 0, "relaxes": []}],
    }
    with pytest.raises(RuleParseError, match="R0"):
        parse_ruleset(raw)


def test_ruleset_rejects_ladder_that_relaxes_r0() -> None:
    raw = {
        "ruleset_version": "x",
        "rules": [{"id": i, "tier": "R0", "params": {}} for i in EXPECTED_RULE_IDS],
        "tiers": {"R0": {"relaxable": False}},
        "relaxation_ladder": [{"tier": 1, "relaxes": [9]}],
    }
    with pytest.raises(RuleParseError, match="R0"):
        parse_ruleset(raw)


def test_ruleset_rejects_missing_rules() -> None:
    raw = {
        "ruleset_version": "x",
        "rules": [{"id": 1, "tier": "R0", "params": {}}],
        "tiers": {},
        "relaxation_ladder": [],
    }
    with pytest.raises(RuleParseError, match="缺少约束"):
        parse_ruleset(raw)


def test_ruleset_rejects_bad_tier() -> None:
    raw = {
        "ruleset_version": "x",
        "rules": [{"id": 1, "tier": "R9", "params": {}}],
        "tiers": {},
        "relaxation_ladder": [],
    }
    with pytest.raises(RuleParseError, match="R9"):
        parse_ruleset(raw)


def test_airspace_capacity_cross_check_reports_diffs() -> None:
    """PG 是空域容量的真源；YAML 那份只用来交叉核对，不一致时返回差异而不抛。"""
    rules = load_ruleset()
    assert rules.cross_check_airspace_capacity({"SAA": 2, "IFR": 1}) == ()
    diffs = rules.cross_check_airspace_capacity({"SAA": 9})
    assert diffs and "SAA" in diffs[0]


def test_singletons_are_cached() -> None:
    assert get_ruleset() is get_ruleset()
    assert get_semantics() is get_semantics()
