"""`rules/` 两份 YAML 的结构与裁定值单测。

这两份文件是**规格的机器可读形态**——任何一条裁定被改错，排班结果就跟着错，
而且不会有任何报错。所以这里逐条钉死 v6 §1.1 与 §3.2 的裁定值。
"""

from __future__ import annotations

import math
from typing import Any

import pytest
import yaml

from backend.core.config import Settings

CFG = Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture(scope="module")
def semantics() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(CFG.SEMANTICS_PATH.read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="module")
def ruleset() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(CFG.RULESET_PATH.read_text(encoding="utf-8"))
    return data


# ─── semantics.yaml：S-01 ~ S-13 全部就位 ────────────────────────────


def test_all_thirteen_switches_present(semantics: dict[str, Any]) -> None:
    expected = {f"S-{i:02d}" for i in range(1, 14)}
    assert set(semantics["switches"]) == expected
    assert semantics["semantics_version"]


@pytest.mark.parametrize(
    ("sid", "value"),
    [
        ("S-01", "all_missions_completed"),
        ("S-02", "class_level"),
        ("S-03", "incomplete_only"),
        ("S-04", "half_open"),
        ("S-05", "dual_runway"),
        ("S-06", "landing_to_takeoff"),
        ("S-07", "same_day_only"),
        ("S-08", "students_only"),
        ("S-09", "instructors_exempt_mature_recurrent"),
        ("S-10", "hard_constraint"),
        ("S-11", "convert_to_recurrent"),
        ("S-12", "from_week_monday"),
        ("S-13", "all_students"),
    ],
)
def test_switch_default_values(semantics: dict[str, Any], sid: str, value: str) -> None:
    """默认值取 v6 §1.1 表的「裁定」列，一条都不许飘。"""
    assert semantics["switches"][sid]["value"] == value


def test_every_switch_lists_its_options(semantics: dict[str, Any]) -> None:
    """每条做成可切换开关（v6 §1.1），且当前取值必须在合法选项内。"""
    for sid, sw in semantics["switches"].items():
        assert sw["options"], f"{sid} 缺少 options"
        assert sw["value"] in sw["options"], f"{sid} 的 value 不在 options 内"


def test_s05_runway_mapping(semantics: dict[str, Any]) -> None:
    """S-05：RWY-1 服务 JL-8/JL-9，RWY-2 仅服务 JL-8（v6 §1.1.1）。"""
    rw = semantics["switches"]["S-05"]["runways"]
    assert rw["RWY-1"]["aircraft_types"] == ["JL-8", "JL-9"]
    assert rw["RWY-2"]["aircraft_types"] == ["JL-8"]


def test_s05_density_scope_follows_d2(semantics: dict[str, Any]) -> None:
    """★ D-2：20 分钟按跑道分组，**7 分钟全场统一**。

    把 7 分钟实现成「按跑道」是 CLAUDE.md §11 明列的反模式。
    """
    scope = semantics["switches"]["S-05"]["density_scope"]
    assert scope["window_20min"] == "per_runway"
    assert scope["separation_7min"] == "airport_wide"


def test_s11_recurrent_config(semantics: dict[str, Any]) -> None:
    """S-11：仅成熟飞行员适用，7 天滑窗，自到期次日起（v6 §1.1.2）。"""
    s11 = semantics["switches"]["S-11"]
    assert s11["enabled"] is True
    assert s11["applies_to_identities"] == ["成熟飞行员"]
    assert s11["window_days"] == 7
    assert s11["start_offset_days"] == 1
    assert s11["cross_week_anchor"] == "last_done_date"


def test_s12_does_not_count_debt(semantics: dict[str, Any]) -> None:
    """S-12：锚点缺失不计欠账——把它当欠账（gap=999）会让基准周假性不可行。"""
    assert semantics["switches"]["S-12"]["count_as_debt"] is False


def test_anchor_formula_uses_d4_general_form(semantics: dict[str, Any]) -> None:
    """D-4：统一取通式 max(0, freq_days − gap)，SPEC_DECISIONS 的 −1 是笔误。"""
    anchor = semantics["frequency_anchor"]
    assert anchor["formula"] == "first_exec_day <= max(0, freq_days - gap)"
    assert anchor["missing_anchor_deadline"] == "freq_days - 1"
    assert "- 1)" not in anchor["formula"]


# ─── ruleset_v1.3.yaml：14 条规则 ───────────────────────────────────


def test_exactly_fourteen_rules(ruleset: dict[str, Any]) -> None:
    """对外仍称「14 条规则」——空域容量并入约束6，不新增第 15 条。"""
    assert ruleset["rule_count"] == 14
    assert len(ruleset["rules"]) == 14
    assert [r["id"] for r in ruleset["rules"]] == list(range(1, 15))


def test_check_ids_align_with_validator(ruleset: dict[str, Any]) -> None:
    assert [r["check_id"] for r in ruleset["rules"]] == [f"C{i:02d}" for i in range(1, 15)]


def test_every_rule_has_both_implementations(ruleset: dict[str, Any]) -> None:
    """§3.2 是 solver 与 validator 两套独立实现的共同依据，两列都不能空。"""
    for r in ruleset["rules"]:
        assert r["solver_encoding"], f"规则 {r['id']} 缺 solver_encoding"
        assert r["validator_check"], f"规则 {r['id']} 缺 validator_check"


def test_rule6_renamed_and_carries_airspace_capacity(ruleset: dict[str, Any]) -> None:
    """★ v6：约束6 更名「资源有效性与容量」并含空域同时段容量（S-10）。"""
    r6 = next(r for r in ruleset["rules"] if r["id"] == 6)
    assert r6["title"] == "资源有效性与容量"
    assert r6["params"]["airspace_capacity"] == {
        "SAA": 2,
        "SAB": 2,
        "IFR": 1,
        "RT1": 1,
        "RT2": 1,
        "RNG": 1,
    }
    assert r6["tier"] == "R0"  # 空域容量归 R0，不可松弛


def test_rule9_density_scopes(ruleset: dict[str, Any]) -> None:
    """★ D-2 在规则集里的落点。"""
    r9 = next(r for r in ruleset["rules"] if r["id"] == 9)
    p = r9["params"]
    assert p["window_min"] == 20 and p["window_max_takeoffs"] == 2
    assert p["window_scope"] == "per_runway"
    assert p["separation_min"] == 7
    assert p["separation_scope"] == "airport_wide"
    assert p["window_boundary"] == "half_open"  # S-04


def test_rule7_turnaround_basis(ruleset: dict[str, Any]) -> None:
    """S-06：着陆 → 起飞；JL-8=30 / JL-9=40。"""
    r7 = next(r for r in ruleset["rules"] if r["id"] == 7)
    assert r7["params"]["basis"] == "landing_to_takeoff"
    assert r7["params"]["turnaround_min"] == {"JL-8": 30, "JL-9": 40}


def test_rule3_applies_to_all_students(ruleset: dict[str, Any]) -> None:
    """S-02 + S-13：A 类整体 ≥1 次/周，对全部学员生效。"""
    r3 = next(r for r in ruleset["rules"] if r["id"] == 3)
    assert r3["params"]["weekly_a_class_min"] == 1
    assert r3["params"]["a_class_scope"] == ["missionA-1", "missionA-2"]
    assert r3["params"]["applies_to"] == "全部学员"


def test_rule13_freq_days_match_v6(ruleset: dict[str, Any]) -> None:
    """v6 §1.3.3 的 freq_days：A 类 3、B~F 类 7、G/H 类 14。"""
    r13 = next(r for r in ruleset["rules"] if r["id"] == 13)
    freq = r13["params"]["freq_days"]
    assert len(freq) == 12
    assert freq["missionA-1"] == freq["missionA-2"] == 3
    assert freq["missionG-1"] == freq["missionH-1"] == 14
    for m in (
        "missionB-1",
        "missionB-2",
        "missionC-1",
        "missionC-2",
        "missionD-1",
        "missionE-1",
        "missionE-2",
        "missionF-1",
    ):
        assert freq[m] == 7


def test_rule14_req_max_is_ceil_seven_over_freq(ruleset: dict[str, Any]) -> None:
    """★ 约束14 `req_max = ceil(7 / freq_days)` —— A 类 3，其余 1（v6 §3.2）。"""
    r13 = next(r for r in ruleset["rules"] if r["id"] == 13)
    r14 = next(r for r in ruleset["rules"] if r["id"] == 14)
    freq = r13["params"]["freq_days"]
    req_max = r14["params"]["req_max"]
    assert set(freq) == set(req_max)
    for mission, f in freq.items():
        assert req_max[mission] == math.ceil(7 / f), mission
    assert req_max["missionA-1"] == 3
    assert req_max["missionB-1"] == 1


def test_rule14_tier_is_explicitly_pending(ruleset: dict[str, Any]) -> None:
    """v6 §3.10 未给约束14 分级 —— 实现为不可松弛，并显式标注待裁定。

    松弛阶梯 Tier0~3 从未松弛过它，所以这个取值不改变任何排班结果。
    """
    r14 = next(r for r in ruleset["rules"] if r["id"] == 14)
    assert r14["tier"] is None
    assert r14["relaxable"] is False
    assert r14["tier_pending_decision"] is True


# ─── 松弛分级（§3.10）───────────────────────────────────────────────


def test_r0_rules_are_exactly_v6_list(ruleset: dict[str, Any]) -> None:
    r0 = sorted(r["id"] for r in ruleset["rules"] if r.get("tier") == "R0")
    assert r0 == [1, 2, 4, 5, 6, 7, 8, 9]


def test_r1_and_r2_rules(ruleset: dict[str, Any]) -> None:
    assert sorted(r["id"] for r in ruleset["rules"] if r.get("tier") == "R1") == [10, 11, 12]
    assert sorted(r["id"] for r in ruleset["rules"] if r.get("tier") == "R2") == [3, 13]


def test_r0_marked_non_relaxable(ruleset: dict[str, Any]) -> None:
    """无论松弛到哪一级，R0 恒满足——这是「100% 合规」在松弛场景下的依据。"""
    assert ruleset["tiers"]["R0"]["relaxable"] is False


def test_relaxation_ladder_four_tiers(ruleset: dict[str, Any]) -> None:
    ladder = ruleset["relaxation_ladder"]
    assert [t["tier"] for t in ladder] == [0, 1, 2, 3]
    assert ladder[0]["relaxes"] == []
    assert ladder[1]["relaxes"] == [13]
    assert ladder[2]["relaxes"] == [13, 3]  # ★ D-6 重定义
    assert set(ladder[3]["relaxes"]) == {13, 3, 10, 11, 12}


def test_ladder_never_relaxes_r0(ruleset: dict[str, Any]) -> None:
    r0 = {r["id"] for r in ruleset["rules"] if r.get("tier") == "R0"}
    for tier in ruleset["relaxation_ladder"]:
        assert not (set(tier["relaxes"]) & r0), f"Tier {tier['tier']} 试图松弛 R0"


def test_ruleset_version(ruleset: dict[str, Any]) -> None:
    assert ruleset["ruleset_version"] == "1.3.0"
    assert ruleset["source"] == "data/origin/rules.pdf"
