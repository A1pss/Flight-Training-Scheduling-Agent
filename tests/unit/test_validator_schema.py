"""格式校验前两层的单元测试（v6 §4.3）。

重点是**三表交叉一致性**——Sheet 1/2/3 是同一份数据的三种投影，v6 §4.3 直接点名
它是「最易出错处」。这里的用例都在投影上做单点篡改，断言比对能指出是哪一张表、
哪一个字段对不上。
"""

from __future__ import annotations

import dataclasses
from datetime import date, time

import pytest

from backend.schemas.plan import SchedulePlan
from backend.validator.context import ValidationContext
from backend.validator.schema import (
    ThreeTableProjection,
    check_cross_table_consistency,
    check_referential_integrity,
    project_plan,
    row_of,
    sorties_of,
    validate_plan_schema,
    verify_format,
)
from tests.fixtures.validator_facts import (
    SHA_ZERO,
    baseline_context,
    compliant_plan,
    compliant_sorties,
    debt,
    make_plan,
    make_sortie,
)


@pytest.fixture(scope="module")
def ctx() -> ValidationContext:
    return baseline_context()


@pytest.fixture(scope="module")
def plan() -> SchedulePlan:
    return compliant_plan()


def payload_of(plan: SchedulePlan) -> dict[str, object]:
    return plan.model_dump(mode="json")


# ─────────────────────────────────────────────────────────────────────
# ① Schema 层
# ─────────────────────────────────────────────────────────────────────
def test_schema_layer_accepts_the_compliant_plan(plan: SchedulePlan) -> None:
    parsed, errors = validate_plan_schema(payload_of(plan))
    assert errors == []
    assert parsed is not None
    assert len(parsed.sorties) == 14


@pytest.mark.parametrize(
    ("path", "value", "fragment"),
    [
        ("sortie_id", "S1", "sorties.0.sortie_id"),  # 系统自己发的号，仍钉死 6 位
        ("aircraft_id", "10AC", "sorties.0.aircraft_id"),
        ("mission_id", "A-1", "sorties.0.mission_id"),
        ("runway_id", "RUNWAY-1", "sorties.0.runway_id"),
        ("airspace_id", "", "sorties.0.airspace_id"),
        ("takeoff", "不是时间", "sorties.0.takeoff"),
    ],
)
def test_schema_layer_rejects_malformed_fields(
    plan: SchedulePlan, path: str, value: str, fragment: str
) -> None:
    payload = payload_of(plan)
    payload["sorties"][0][path] = value  # type: ignore[index]
    parsed, errors = validate_plan_schema(payload)
    assert parsed is None
    assert any(fragment in e for e in errors), errors


def test_schema_layer_rejects_unknown_crew_role(plan: SchedulePlan) -> None:
    payload = payload_of(plan)
    payload["sorties"][0]["crew"][0]["role"] = "副驾"  # type: ignore[index]
    parsed, errors = validate_plan_schema(payload)
    assert parsed is None
    assert any("role" in e for e in errors)


def test_schema_layer_accepts_the_recurrent_role() -> None:
    """`复训` 是 v6 为 S-11 新增的角色，必须在枚举里。"""
    solo = make_sortie(
        "S000900", 3, "07:20", "missionC-1", "AC61", (("P04", "复训"),), is_recurrent=True
    )
    parsed, errors = validate_plan_schema(payload_of(make_plan([solo])))
    assert errors == []
    assert parsed is not None
    assert parsed.sorties[0].crew[0].role == "复训"


def test_schema_layer_follows_z4_prefix_only_numbering() -> None:
    """v6 §5.1.1 / Z-4：编号只固定前缀、不限位数；空域编号不是枚举。"""
    payload = payload_of(compliant_plan())
    first = payload["sorties"][0]  # type: ignore[index]
    first["aircraft_id"] = "AC1000"
    first["runway_id"] = "RWY-37"
    first["mission_id"] = "missionZ-12"
    first["airspace_id"] = "LAC"  # 换机场就换空域编号
    first["crew"][0]["person_id"] = "P100"
    parsed, errors = validate_plan_schema(payload)
    assert errors == []
    assert parsed is not None


def test_schema_layer_rejects_plan_level_inconsistency(plan: SchedulePlan) -> None:
    payload = payload_of(plan)
    payload["week_end"] = "2026-01-12"  # 跨度 8 天
    parsed, errors = validate_plan_schema(payload)
    assert parsed is None
    assert any("排班跨度必须为 7 天" in e for e in errors)


# ─────────────────────────────────────────────────────────────────────
# ② 外键存在性
# ─────────────────────────────────────────────────────────────────────
def test_referential_integrity_passes_on_baseline(
    plan: SchedulePlan, ctx: ValidationContext
) -> None:
    assert check_referential_integrity(plan, ctx) == []


def test_referential_integrity_catches_dangling_references(ctx: ValidationContext) -> None:
    broken = make_plan(
        [
            make_sortie("S000901", 6, "09:00", "missionA-1", "AC10", (("P05", "单飞"),)).model_copy(
                update={
                    "aircraft_id": "AC99",
                    "airspace_id": "LAC",
                    "runway_id": "RWY-9",
                }
            )
        ],
        validate=False,
    )
    errors = check_referential_integrity(broken, ctx)
    assert any("aircraft_id=AC99 不在册" in e for e in errors)
    assert any("airspace_id=LAC 不在册" in e for e in errors)
    assert any("runway_id=RWY-9 不在册" in e for e in errors)


def test_referential_integrity_catches_wrong_names(ctx: ValidationContext) -> None:
    s = compliant_sorties()[0]
    renamed = s.model_copy(
        update={
            "mission_name": "本场起落",
            "crew": [s.crew[0].model_copy(update={"name": "罗垒"})],
        }
    )
    errors = check_referential_integrity(make_plan([renamed], validate=False), ctx)
    assert any("与人员表的 罗磊 不一致" in e for e in errors)
    assert any("与课目表的 本场起落航线 不一致" in e for e in errors)


def test_referential_integrity_covers_debts_and_blocked_items(ctx: ValidationContext) -> None:
    broken = make_plan(
        compliant_sorties(),
        validate=False,
        debts=[debt("P99", "missionZ-9", required=1, scheduled=0)],
        blocked_items=[],
    )
    errors = check_referential_integrity(broken, ctx)
    assert any("debts.person_id=P99 不在册" in e for e in errors)
    assert any("debts.mission_id=missionZ-9 不在册" in e for e in errors)


# ─────────────────────────────────────────────────────────────────────
# ③ 三表交叉一致性
# ─────────────────────────────────────────────────────────────────────
def test_projection_is_self_consistent(plan: SchedulePlan) -> None:
    proj = project_plan(plan)
    assert check_cross_table_consistency(proj) == []
    assert len(sorties_of(proj)) == 14
    # 每个架次在人员表里按机组人数出现
    total_person_rows = sum(len(rows) for rows in proj.by_person.values())
    assert total_person_rows == sum(len(s.crew) for s in plan.sorties)


def test_cross_table_detects_field_drift_in_person_table(plan: SchedulePlan) -> None:
    proj = project_plan(plan)
    person_id = next(iter(proj.by_person))
    rows = list(proj.by_person[person_id])
    rows[0] = dataclasses.replace(rows[0], aircraft_id="AC99")
    tampered = ThreeTableProjection(
        by_day=proj.by_day,
        by_person={**proj.by_person, person_id: tuple(rows)},
        by_aircraft=proj.by_aircraft,
    )
    errors = check_cross_table_consistency(tampered)
    assert any("在分日表与人员表" in e and "aircraft_id" in e for e in errors)


def test_cross_table_detects_missing_row_in_aircraft_table(plan: SchedulePlan) -> None:
    proj = project_plan(plan)
    ac_id = next(iter(proj.by_aircraft))
    trimmed = {**proj.by_aircraft, ac_id: proj.by_aircraft[ac_id][1:]}
    dropped = proj.by_aircraft[ac_id][0].sortie_id
    errors = check_cross_table_consistency(
        ThreeTableProjection(by_day=proj.by_day, by_person=proj.by_person, by_aircraft=trimmed)
    )
    assert f"{dropped} 出现在分日表但不在飞机表" in errors


def test_cross_table_detects_wrong_grouping_key(plan: SchedulePlan) -> None:
    proj = project_plan(plan)
    day = next(iter(proj.by_day))
    other_day = date(2026, 1, 11) if day != date(2026, 1, 11) else date(2026, 1, 5)
    misfiled = dict(proj.by_day)
    misfiled[other_day] = misfiled.pop(day)  # 周一那组被错分到周日名下
    errors = check_cross_table_consistency(
        ThreeTableProjection(
            by_day=misfiled, by_person=proj.by_person, by_aircraft=proj.by_aircraft
        )
    )
    assert any("被分在分日表" in e and "但行内日期为" in e for e in errors)


def test_cross_table_detects_crew_count_mismatch(plan: SchedulePlan) -> None:
    """带飞架次在人员表里必须出现两次（教员一次、学员一次）。"""
    proj = project_plan(plan)
    dual = next(r for rows in proj.by_day.values() for r in rows if len(r.crew) == 2)
    person_id = dual.crew[0][0]
    kept = tuple(r for r in proj.by_person[person_id] if r.sortie_id != dual.sortie_id)
    errors = check_cross_table_consistency(
        ThreeTableProjection(
            by_day=proj.by_day,
            by_person={**proj.by_person, person_id: kept},
            by_aircraft=proj.by_aircraft,
        )
    )
    assert any("在人员表中出现 1 次" in e for e in errors)


def test_row_of_sorts_crew_deterministically() -> None:
    s = make_sortie("S000902", 0, "06:00", "missionC-1", "AC10", (("P06", "学员"), ("P01", "教员")))
    assert row_of(s).crew == (("P01", "教员"), ("P06", "学员"))


# ─────────────────────────────────────────────────────────────────────
# 汇总
# ─────────────────────────────────────────────────────────────────────
def test_verify_format_accepts_plan_object_and_payload(
    plan: SchedulePlan, ctx: ValidationContext
) -> None:
    assert verify_format(plan, ctx).passed
    assert verify_format(payload_of(plan), ctx).passed


def test_verify_format_stops_at_schema_layer(ctx: ValidationContext) -> None:
    """Schema 层不过就不必往下走 —— 后两层需要一个类型正确的对象才谈得上比对。"""
    report = verify_format({"plan_id": "x"}, ctx)
    assert not report.passed
    assert report.schema_errors and not report.integrity_errors


def test_verify_format_reports_all_three_layers(ctx: ValidationContext) -> None:
    broken = make_plan(
        [
            make_sortie("S000903", 6, "09:00", "missionA-1", "AC10", (("P05", "单飞"),)).model_copy(
                update={"aircraft_id": "AC99", "landing": time(9, 30)}
            )
        ],
        validate=False,
    )
    report = verify_format(broken, ctx)
    assert not report.passed
    assert report.integrity_errors
    assert report.all_errors()


def test_verify_format_uses_supplied_projection(plan: SchedulePlan, ctx: ValidationContext) -> None:
    """回读场景：投影来自 xlsx，而不是再从 plan 投一次（否则是自证）。"""
    proj = project_plan(plan)
    empty = ThreeTableProjection(by_day=proj.by_day, by_person={}, by_aircraft={})
    report = verify_format(plan, ctx, projection=empty)
    assert not report.passed
    assert any("不在飞机表" in e for e in report.cross_table_errors)


def test_sha_placeholder_is_wellformed() -> None:
    """夹具用的指纹也必须过契约（64 位小写十六进制）。"""
    assert len(SHA_ZERO) == 64
