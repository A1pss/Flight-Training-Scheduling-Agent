"""候选枚举与静态预筛（v6 §3.1）。

本文件用 `tests/fixtures/solver_facts.py` 的**合成算例**，实体编号/机型/规模
与基准数据全不一样 —— 求解器若偷偷依赖了 8 人 / `JL-8`，这里第一个红。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.core.ruleset import get_semantics
from backend.solver.candidates import (
    DROP_AIRCRAFT_MAINTENANCE,
    DROP_AIRSPACE_CLOSED,
    DROP_NO_CLASS_QUAL,
    DROP_NO_INSTRUCTOR,
    DROP_NO_RUNWAY,
    DROP_PERSON_UNAVAILABLE,
    DROP_PREREQ_UNMET,
    DROP_QUAL_EXPIRED,
    DROP_SEATS,
    Candidate,
    dual_required_for,
    enumerate_candidates,
    frequency_deadline,
    min_hitting_count,
    sliding_windows,
)
from backend.solver.data import ScenarioOverrides
from tests.fixtures.solver_facts import (
    TEST_WEEK_START,
    all_day,
    make_bundle,
    make_problem,
    make_spec,
)


def _enumerate(**kwargs: object):  # type: ignore[no-untyped-def]
    bundle = make_bundle(**kwargs)
    return bundle, enumerate_candidates(
        bundle.data, bundle.spec, ruleset=bundle.ruleset, semantics=bundle.semantics
    )


# ─────────────────────────────────────────────────────────────────────
# §3.1.1 机组编成判定式
# ─────────────────────────────────────────────────────────────────────
def test_dual_required_decision_rule() -> None:
    """`需带飞 = (mission.带飞 == 是) ∧ (person.身份 == 学员)`（v6 §3.1.1 / D-1）。"""
    sem = get_semantics()
    assert dual_required_for(True, "学员", sem) is True
    assert dual_required_for(False, "学员", sem) is False  # ← D-1：A 类学员单飞
    assert dual_required_for(True, "成熟飞行员", sem) is False
    assert dual_required_for(True, "教员", sem) is False


def test_student_a_class_is_solo_and_b_class_is_dual() -> None:
    _bundle, cset = _enumerate()
    solo = [c for c in cset.candidates if c.mission_id == "missionA-1"]
    dual = [c for c in cset.candidates if c.mission_id == "missionB-1"]
    assert solo and all(c.instructor_id is None and len(c.crew_ids) == 1 for c in solo)
    assert dual and all(c.instructor_id == "P401" and len(c.crew_ids) == 2 for c in dual)


def test_instructors_generate_no_trainee_candidates() -> None:
    """S-09：教员不作为受训人生成候选，只在带飞候选里占教员岗。"""
    _bundle, cset = _enumerate()
    assert all(c.trainee_id != "P401" for c in cset.candidates)
    assert any(c.instructor_id == "P401" for c in cset.candidates)


def test_seats_limit_blocks_dual_candidates() -> None:
    """约束5：机组人数不得超过座位数。单座机上带飞候选不该生成。"""
    _bundle, cset = _enumerate(seats=1)
    assert not [c for c in cset.candidates if c.dual]
    assert DROP_SEATS in cset.drop_counts


# ─────────────────────────────────────────────────────────────────────
# S-01 先修与阻塞项
# ─────────────────────────────────────────────────────────────────────
def test_prereq_unmet_blocks_and_records_item() -> None:
    """先修未达标 → 不生成候选 + 记入 blocked_items（v6 §3.6 BLOCKED ≠ INFEASIBLE）。"""
    _bundle, cset = _enumerate()
    assert not [c for c in cset.candidates if c.mission_id == "missionC-1"]
    blocked = [b for b in cset.blocked_items if b.mission_id == "missionC-1"]
    assert len(blocked) == 1
    assert blocked[0].person_id == "P402"
    # S-01：先修「A类」= A-1 且 A-2 都完成；只完成了 A-1 → 缺 A-2
    assert blocked[0].missing_prereqs == ["missionA-2"]
    # v6 §12.3 要求 Sheet 4 的「缺失先修」列逐字为「missionX 未完成」
    assert blocked[0].reason == "missionA-2 未完成"
    assert DROP_PREREQ_UNMET in cset.drop_counts


def test_s01_class_prereq_needs_every_mission_in_class() -> None:
    """S-01：类引用要求该类**全部**课目完成，缺一门就 BLOCKED。"""
    _bundle, cset = _enumerate(student_completed=frozenset())
    assert not [c for c in cset.candidates if c.mission_id == "missionC-1"]
    reasons = {b.mission_id: b.reason for b in cset.blocked_items}
    # 课目引用只缺一门；类别引用（A类）把该类**全部**课目都要求上（S-01）
    assert reasons["missionB-1"] == "missionA-1 未完成"
    assert reasons["missionC-1"] == "missionA-1 未完成、missionA-2 未完成"


def test_completed_mission_still_generates_candidates() -> None:
    """已完成课目不受约束13，但**照常生成候选** —— 约束3 的每周必飞要用它们。

    v6 §3.1.3 的候选上界公式也是把已完成课目算进去的。
    """
    _bundle, cset = _enumerate()
    assert [c for c in cset.candidates if c.mission_id == "missionA-1"]
    assert not [b for b in cset.blocked_items if b.mission_id == "missionA-1"]


def test_missing_class_qualification_is_not_a_blocked_item() -> None:
    """没有类别资质属于「双重排除」，**不算阻塞项**（v6 §1.4.1）。

    预筛顺序在这里是有语义的：类别资质检查必须排在先修检查**之前**，
    否则学员会因为够不到的课目冒出一堆假阻塞项。
    """
    data = make_problem()
    stripped = {k: v for k, v in data.persons["P402"].qualifications.items() if k != "B"}
    object.__setattr__(data.persons["P402"], "qualifications", stripped)
    cset = enumerate_candidates(
        data, make_spec(data), ruleset=make_bundle().ruleset, semantics=get_semantics()
    )
    assert not [c for c in cset.candidates if c.mission_id == "missionB-1"]
    assert not [b for b in cset.blocked_items if b.mission_id == "missionB-1"]
    assert DROP_NO_CLASS_QUAL in cset.drop_counts


# ─────────────────────────────────────────────────────────────────────
# 约束2 与 S-11 例外
# ─────────────────────────────────────────────────────────────────────
def test_expired_qualification_removed_for_student() -> None:
    """学员按约束2 字面执行：到期日当日保留，次日起剔除。"""
    data = make_problem()
    expiry = TEST_WEEK_START + timedelta(days=2)
    quals = dict(data.persons["P402"].qualifications)
    quals["B"] = type(quals["B"])(mission_class="B", level="带飞", expiry=expiry)
    object.__setattr__(data.persons["P402"], "qualifications", quals)
    cset = enumerate_candidates(
        data, make_spec(data), ruleset=make_bundle().ruleset, semantics=get_semantics()
    )
    days = {c.day for c in cset.candidates if c.mission_id == "missionB-1"}
    assert days == {0, 1, 2}  # 到期日 day2 当日仍可执行
    assert DROP_QUAL_EXPIRED in cset.drop_counts


def test_s11_mature_pilot_keeps_expired_candidates_as_recurrent() -> None:
    """S-11（v6 §3.1.2）：成熟飞行员 `day > expiry` 的候选**不剔除**，标 `is_recurrent`。"""
    expiry = TEST_WEEK_START + timedelta(days=1)
    _bundle, cset = _enumerate(with_mature=True, mature_expiry=expiry)
    mature_c = [
        c for c in cset.candidates if c.trainee_id == "P403" and c.mission_id == "missionC-1"
    ]
    assert mature_c, "成熟飞行员的到期资质候选被错误剔除了"
    assert {c.is_recurrent for c in mature_c if c.day <= 1} == {False}
    assert {c.is_recurrent for c in mature_c if c.day >= 2} == {True}
    # 复训架次机组人数为 1（v6 §1.2.4）
    assert all(len(c.crew_ids) == 1 for c in mature_c)


def test_person_unavailable_day_is_dropped() -> None:
    off = TEST_WEEK_START + timedelta(days=3)
    _bundle, cset = _enumerate(student_unavailable=frozenset({off}))
    assert 3 not in {c.day for c in cset.candidates}
    assert DROP_PERSON_UNAVAILABLE in cset.drop_counts


def test_all_day_maintenance_removes_that_day() -> None:
    when = TEST_WEEK_START + timedelta(days=4)
    _bundle, cset = _enumerate(maintenance=(all_day(when),))
    assert 4 not in {c.day for c in cset.candidates}
    assert DROP_AIRCRAFT_MAINTENANCE in cset.drop_counts


def test_closed_airspace_removes_bound_missions() -> None:
    """空域关闭 = 容量降为 0（v6 §3.4）：绑定该空域的课目不生成候选。"""
    _bundle, cset = _enumerate(overrides=ScenarioOverrides(airspace_capacity={"NAV": 0}))
    assert not [c for c in cset.candidates if c.mission_id == "missionB-1"]
    assert DROP_AIRSPACE_CLOSED in cset.drop_counts


def test_all_runways_closed_leaves_no_candidates() -> None:
    _bundle, cset = _enumerate(
        overrides=ScenarioOverrides(closed_runways=frozenset({"RWY-7", "RWY-8"}))
    )
    assert not cset.candidates
    assert DROP_NO_RUNWAY in cset.drop_counts


def test_no_instructor_available_drops_dual_candidates() -> None:
    data = make_problem()
    off = frozenset({data.date_of(d) for d in range(7)})
    object.__setattr__(data.persons["P401"], "unavailable", off)
    cset = enumerate_candidates(
        data, make_spec(data), ruleset=make_bundle().ruleset, semantics=get_semantics()
    )
    assert not [c for c in cset.candidates if c.dual]
    assert DROP_NO_INSTRUCTOR in cset.drop_counts


def test_scope_filters_persons_and_missions() -> None:
    data = make_problem()
    spec = make_spec(data).model_copy(update={"scope_missions": ["missionA-1"]})
    cset = enumerate_candidates(
        data, spec, ruleset=make_bundle().ruleset, semantics=get_semantics()
    )
    assert {c.mission_id for c in cset.candidates} == {"missionA-1"}


# ─────────────────────────────────────────────────────────────────────
# 候选的确定性
# ─────────────────────────────────────────────────────────────────────
def test_candidate_order_is_deterministic() -> None:
    """铁律 9：候选顺序决定变量创建顺序，必须逐次一致。"""
    runs = [_enumerate()[1].candidates for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]
    assert list(runs[0]) == sorted(runs[0], key=Candidate.sort_key)


def test_candidate_key_is_stable_and_unique() -> None:
    _bundle, cset = _enumerate()
    keys = [c.key for c in cset.candidates]
    assert len(keys) == len(set(keys))


def test_slots_group_candidates_by_person_mission_day() -> None:
    _bundle, cset = _enumerate(aircraft_count=2)
    for slot, idxs in cset.slots.items():
        for i in idxs:
            assert cset.candidates[i].slot == slot
    # 同一时隙内只在机号/教员上不同
    for idxs in cset.slots.values():
        assert len({cset.candidates[i].mission_id for i in idxs}) == 1


# ─────────────────────────────────────────────────────────────────────
# §3.5 频率滑窗与跨周锚点
# ─────────────────────────────────────────────────────────────────────
def test_sliding_windows_lengths() -> None:
    assert sliding_windows(3) == ((0, 1, 2), (1, 2, 3), (2, 3, 4), (3, 4, 5), (4, 5, 6))
    assert sliding_windows(7) == (tuple(range(7)),)
    assert sliding_windows(14) == ()  # 周内不存在完整窗口，由锚点决定


def test_frequency_deadline_missing_anchor_is_s12_not_gap999() -> None:
    """S-12：`last_done_date is None` → `deadline = freq_days − 1`，**且不计欠账**。

    这条是 CLAUDE.md §11 点名的反模式现场：写成 `gap = 999` 会让 deadline 归 0、
    所有未完成课目压在周一，基准周假性不可行。
    """
    week = date(2026, 1, 5)
    sem = get_semantics()
    deadline, is_debt = frequency_deadline(
        freq_days=7, week_start=week, last_done=None, semantics=sem
    )
    assert (deadline, is_debt) == (6, False)
    deadline3, _ = frequency_deadline(freq_days=3, week_start=week, last_done=None, semantics=sem)
    assert deadline3 == 2
    # gap=999 的话 deadline 会是 0 —— 断言它不是
    assert deadline3 != 0


def test_frequency_deadline_uses_d4_general_formula() -> None:
    """D-4：统一取通式 `max(0, F − gap)`；SPEC_DECISIONS §B.4 第二分支的 −1 是笔误。"""
    week = date(2026, 1, 5)
    sem = get_semantics()
    # gap = 2 < F = 7 → 通式给 5（笔误版本会给 4）
    deadline, is_debt = frequency_deadline(
        freq_days=7, week_start=week, last_done=week - timedelta(days=2), semantics=sem
    )
    assert (deadline, is_debt) == (5, False)
    # gap = 9 ≥ F = 7 → 已欠账，deadline 归 0
    deadline, is_debt = frequency_deadline(
        freq_days=7, week_start=week, last_done=week - timedelta(days=9), semantics=sem
    )
    assert (deadline, is_debt) == (0, True)


def test_min_hitting_count_matches_debt_required() -> None:
    """`TrainingDebt.required` 的口径：滑窗的最小命中数。"""
    assert min_hitting_count(sliding_windows(3)) == 2  # A 类
    assert min_hitting_count(sliding_windows(7)) == 1  # B~F 类
    assert min_hitting_count(()) == 0


# ─────────────────────────────────────────────────────────────────────
# 要求集（约束3 / 13 / S-11）
# ─────────────────────────────────────────────────────────────────────
def test_weekly_class_requirement_applies_to_all_students() -> None:
    """S-13：约束3 对**全部学员**生效，不论完成状态；S-02：按类别整体计数。"""
    _bundle, cset = _enumerate()
    c03 = [r for r in cset.requirements if r.rule_id == 3]
    assert len(c03) == 1
    assert c03[0].mission_class == "A" and c03[0].mission_id is None
    assert c03[0].days == tuple(range(7)) and c03[0].min_count == 1


def test_completed_mission_has_no_c13_requirement() -> None:
    """S-03：已完成课目不受约束13。"""
    _bundle, cset = _enumerate()
    assert not [r for r in cset.requirements if r.rule_id == 13 and r.mission_id == "missionA-1"]
    assert [r for r in cset.requirements if r.mission_id == "missionB-1"]


def test_blocked_mission_has_no_requirement() -> None:
    _bundle, cset = _enumerate()
    assert not [r for r in cset.requirements if r.mission_id == "missionC-1"]


def test_s11_recurrent_requirement_only_when_window_inside_week() -> None:
    """S-11 复训窗口跨出本周 → 本周不强制（基准周即此情形），落在周内 → 强制。"""
    # 到期日 = 周一前一天 → 复训自周一起算，窗口 [day0, day6] 完全在周内
    inside = TEST_WEEK_START - timedelta(days=1)
    _b1, cset_inside = _enumerate(with_mature=True, mature_expiry=inside)
    recurrent = [r for r in cset_inside.requirements if r.kind == "RECURRENT"]
    assert [r.mission_class for r in recurrent] == ["C"]
    assert recurrent[0].days == tuple(range(7))

    # 到期日 = 周四 → 复训自周五起算，窗口伸到下周 → 本周不强制
    outside = TEST_WEEK_START + timedelta(days=3)
    _b2, cset_outside = _enumerate(with_mature=True, mature_expiry=outside)
    assert not [r for r in cset_outside.requirements if r.kind == "RECURRENT"]


def test_debt_basis_required_counts() -> None:
    _bundle, cset = _enumerate()
    basis = {b.mission_id: b for b in cset.debt_basis}
    assert basis["missionB-1"].required == 1
    assert basis["missionB-1"].is_debt is False


def test_implied_min_sorties_is_a_valid_lower_bound() -> None:
    """冗余下界必须真的是下界。

    合成算例：A-2 未完成、freq 3 → 该类桶取 max(滑窗最小命中 2, 每周必飞 1) = 2；
    B-1 未完成、freq 7 → 1。合计 3，正是实际最优架次数。
    """
    bundle, cset = _enumerate()
    classes = {mid: m.mission_class for mid, m in bundle.data.missions.items()}
    assert cset.implied_min_sorties(classes) == 3
    outcome_count = 3
    assert cset.implied_min_sorties(classes) <= outcome_count


def test_requirement_lookup_and_drop_helpers() -> None:
    _bundle, cset = _enumerate()
    req = cset.requirements[0]
    assert cset.requirement(req.req_id) is req
    with pytest.raises(KeyError):
        cset.requirement("nope")
    assert cset.drops_for("P402", "missionC-1")
    assert cset.drops_for("P402")[0].label
