"""排班契约单测（v6 附录 B）。

重点覆盖出口标准要求的 `_crew_composition` 四种情形：
带飞 2 人合法 / 带飞 2 人角色错 / 单飞 1 人 / is_recurrent 但角色不是「复训」。
"""

from __future__ import annotations

from datetime import date, time

import pytest
from pydantic import ValidationError

from backend.schemas import (
    BlockedItem,
    CrewMember,
    SchedulePlan,
    Sortie,
    TrainingDebt,
)

SHA_ZERO = "0" * 64


def _sortie(**overrides: object) -> Sortie:
    base: dict[str, object] = {
        "sortie_id": "S000001",
        "date": date(2026, 1, 5),
        "weekday": "周一",
        "takeoff": time(8, 0),
        "landing": time(8, 35),
        "mission_id": "missionC-1",
        "mission_name": "仪表飞行",
        "airspace_id": "IFR",
        "aircraft_id": "AC10",
        "runway_id": "RWY-1",
        "crew": [
            CrewMember(person_id="P01", name="孙军", role="教员"),
            CrewMember(person_id="P08", name="何超", role="学员"),
        ],
    }
    base.update(overrides)
    return Sortie(**base)  # type: ignore[arg-type]


# ─── _crew_composition 的四种情形（出口标准）─────────────────────────


def test_crew_dual_ok() -> None:
    """① 带飞 2 人合法：1 教员 + 1 学员。"""
    s = _sortie()
    assert len(s.crew) == 2
    assert sorted(c.role for c in s.crew) == sorted(["教员", "学员"])


@pytest.mark.parametrize(
    "roles",
    [
        ("教员", "教员"),
        ("学员", "学员"),
        ("教员", "单飞"),
        ("复训", "学员"),
    ],
)
def test_crew_dual_wrong_roles(roles: tuple[str, str]) -> None:
    """② 带飞 2 人角色错：必须是 1 教员 + 1 学员，其余组合一律拒绝。"""
    crew = [
        CrewMember(person_id="P01", name="甲", role=roles[0]),  # type: ignore[arg-type]
        CrewMember(person_id="P02", name="乙", role=roles[1]),  # type: ignore[arg-type]
    ]
    with pytest.raises(ValidationError, match="带飞架次机组必须为 1 教员 \\+ 1 学员"):
        _sortie(crew=crew)


@pytest.mark.parametrize("role", ["单飞", "复训"])
def test_crew_solo_ok(role: str) -> None:
    """③ 单飞 1 人：角色必须是「单飞」或「复训」。"""
    s = _sortie(
        crew=[CrewMember(person_id="P08", name="何超", role=role)],  # type: ignore[arg-type]
        is_recurrent=(role == "复训"),
    )
    assert len(s.crew) == 1


@pytest.mark.parametrize("role", ["教员", "学员"])
def test_crew_solo_wrong_role(role: str) -> None:
    """③b 单人架次角色不能是「教员」或「学员」。"""
    with pytest.raises(ValidationError, match="单人架次角色必须为 单飞 或 复训"):
        _sortie(crew=[CrewMember(person_id="P08", name="何超", role=role)])  # type: ignore[arg-type]


def test_is_recurrent_requires_recurrent_role() -> None:
    """④ is_recurrent=True 但角色不是「复训」→ 拒绝（S-11）。"""
    with pytest.raises(ValidationError, match="is_recurrent 架次的角色必须为 复训"):
        _sortie(
            crew=[CrewMember(person_id="P04", name="刘斌", role="单飞")],
            is_recurrent=True,
        )


# ─── 时间一致性 ──────────────────────────────────────────────────────


def test_landing_must_be_after_takeoff() -> None:
    with pytest.raises(ValidationError, match="着陆时刻必须晚于起飞时刻"):
        _sortie(takeoff=time(9, 0), landing=time(9, 0))


@pytest.mark.parametrize(
    ("takeoff", "landing"),
    [(time(5, 30), time(6, 5)), (time(17, 40), time(18, 30))],
)
def test_sortie_must_fit_training_window(takeoff: time, landing: time) -> None:
    """训练窗 06:00-18:00（v6 §1.3.2）。"""
    with pytest.raises(ValidationError, match="训练窗 06:00-18:00"):
        _sortie(takeoff=takeoff, landing=landing)


# ─── v6 新增字段一个都不许漏 ─────────────────────────────────────────


def test_v6_new_fields_present() -> None:
    fields = set(Sortie.model_fields)
    assert {"runway_id", "is_recurrent"} <= fields
    assert set(SchedulePlan.model_fields) >= {"semantics_switches", "runway_model"}


def test_crew_role_enum_includes_recurrent() -> None:
    """★ v6 新增「复训」。"""
    CrewMember(person_id="P04", name="刘斌", role="复训")


def test_extra_forbid_everywhere() -> None:
    with pytest.raises(ValidationError):
        _sortie(unexpected_field="x")


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("sortie_id", "S1"),  # 系统自己发的号，位数仍然钉死（§10.6）
        ("mission_id", "mission1-1"),  # 类别位必须是字母
        ("mission_id", "missionA1"),  # 缺连字符
        ("aircraft_id", "A10"),  # 前缀不对
        ("aircraft_id", "ACx"),  # 序号不是数字
        ("runway_id", "RWY-A"),  # 序号不是数字
        ("runway_id", "RW-1"),  # 前缀不对
        ("person_id", "X01"),  # 前缀不对
        ("airspace_id", ""),  # 空域编号不许为空
    ],
)
def test_field_patterns_reject_malformed(field: str, bad: str) -> None:
    """格式确实**畸形**的编号要拒。"""
    kwargs: dict[str, object] = {field: bad}
    if field == "person_id":
        kwargs = {"crew": [{"person_id": bad, "name": "某人", "role": "单飞"}]}
    with pytest.raises(ValidationError):
        _sortie(**kwargs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # 编号只固定前缀约定、不限位数（v6 §5.1.1，业务方 2026-08-11 裁定）
        ("mission_id", "missionZ-1"),  # 类别放宽到 A~Z
        ("mission_id", "missionA-12"),  # 序号不限位数
        ("aircraft_id", "AC1"),  # 一位机号
        ("aircraft_id", "AC1024"),  # 四位机号
        ("runway_id", "RWY-3"),  # 换机场就换跑道编号
        ("airspace_id", "LAC"),  # 空域编号完全由上传数据决定，不是枚举
        ("airspace_id", "Large-Area-C"),
    ],
)
def test_field_patterns_accept_non_baseline_ids(field: str, value: str) -> None:
    """**换一批数据也要能装出 `Sortie`。**

    这一组曾经全是「期望抛 ValidationError」—— 那是把 §1.3 的基准取值当成了系统上限。
    附录 B 于 2026-08-11 按 §5.1.1 放宽：编号只固定前缀约定，空域编号不是枚举。
    否则用户上传 `P100` / `LAC` / `RWY-3` 时，**摄取通过、求解通过，组装方案时才炸**。
    """
    assert _sortie(**{field: value})


def test_three_digit_person_id_is_accepted() -> None:
    """9 个人以上的训练单位（`P100`）必须能排班。"""
    sortie = _sortie(crew=[{"person_id": "P100", "name": "第一百人", "role": "单飞"}])
    assert sortie.crew[0].person_id == "P100"


def test_role_and_weekday_stay_enumerated() -> None:
    """`role` / `weekday` 是**规格**，不是基准取值 —— 保持枚举（§5.1.1 同一口径）。"""
    with pytest.raises(ValidationError):
        _sortie(crew=[{"person_id": "P01", "name": "某人", "role": "见习教员"}])
    with pytest.raises(ValidationError):
        _sortie(weekday="星期一")


# ─── SchedulePlan ────────────────────────────────────────────────────


def _plan(**overrides: object) -> SchedulePlan:
    base: dict[str, object] = {
        "plan_id": "PLAN-2026W02-001",
        "iso_week": "2026W02",
        "week_start": date(2026, 1, 5),
        "week_end": date(2026, 1, 11),
        "snapshot_id": "snap-001",
        "ruleset_version": "1.3.0",
        "semantics_version": "1.0.0",
        "semantics_switches": {"S-05": "dual_runway"},
        "runway_model": "dual_runway",
        "relaxation_tier": 0,
        "sorties": [_sortie()],
        "content_sha256": SHA_ZERO,
    }
    base.update(overrides)
    return SchedulePlan(**base)  # type: ignore[arg-type]


def test_plan_ok() -> None:
    p = _plan()
    assert p.iso_week == "2026W02"
    assert p.runway_model == "dual_runway"


def test_plan_week_must_span_seven_days() -> None:
    with pytest.raises(ValidationError, match="必须为 7 天"):
        _plan(week_end=date(2026, 1, 10))


def test_plan_rejects_sortie_outside_week() -> None:
    with pytest.raises(ValidationError, match="落在排班周"):
        _plan(sorties=[_sortie(date=date(2026, 1, 12), weekday="周一")])


def test_plan_rejects_duplicate_sortie_ids() -> None:
    with pytest.raises(ValidationError, match="sortie_id 必须唯一"):
        _plan(sorties=[_sortie(), _sortie()])


def test_plan_relaxation_tier_range() -> None:
    with pytest.raises(ValidationError):
        _plan(relaxation_tier=4)


def test_plan_content_sha_pattern() -> None:
    with pytest.raises(ValidationError):
        _plan(content_sha256="notasha")


# ─── BlockedItem / TrainingDebt ─────────────────────────────────────


def test_blocked_item() -> None:
    """基准周何超的 B-1 被 A 类先修卡住（v6 §1.4.2）。"""
    b = BlockedItem(
        person_id="P08",
        mission_id="missionB-1",
        reason="先修 A 类未达标",
        missing_prereqs=["missionA-2"],
    )
    assert b.missing_prereqs == ["missionA-2"]


def test_training_debt_arithmetic() -> None:
    d = TrainingDebt(
        person_id="P06",
        mission_id="missionF-1",
        required=1,
        scheduled=0,
        debt=1,
        relaxed_by="TIER1",
    )
    assert d.debt == 1


def test_training_debt_rejects_inconsistent_debt() -> None:
    """欠账必须等于 max(0, required − scheduled)，不许手填一个好看的数。"""
    with pytest.raises(ValidationError, match="debt 应为"):
        TrainingDebt(
            person_id="P06",
            mission_id="missionF-1",
            required=2,
            scheduled=0,
            debt=1,
            relaxed_by="TIER1",
        )
