"""校验器测试用的手工事实与手工排班方案。

⚠️ **本文件的每一条数据都是手工构造的**：实体来自 v6 §1.3 的实体全景表（逐格照
`data/origin/*.pdf`），排班方案是本窗口按 v6 §3.2 的 14 条规则**手工排出来的**，
**没有跑过求解器**（CLAUDE.md 铁律 2：写 `validator/` 的窗口不许打开
`backend/solver/`，也不许去跑它拿解）。

三份方案：

- :func:`compliant_plan` —— 14 架次（9 带飞 + 5 单飞）的合规样本，14 条全过
- :func:`image4_plan` —— `data/origin/image 4.png` 里那份**已知违规**的样例排班
  （裁剪说明见该函数文档）
- 各类单点违规样本由 `tests/unit/test_validator_checks.py` 在合规样本上就地改写

实体行用 `backend.models` 的 ORM 对象构造后交给 `context_from_rows` 装配 ——
这样「PG 行 → ValidationContext」这条路径也被测到，且不需要连库。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from backend.models.entities import (
    Aircraft,
    AircraftMaintenance,
    AircraftMissionCapability,
    Airspace,
    Mission,
    MissionAircraftType,
    MissionPrereq,
    Person,
    PersonAircraftType,
    PersonCompletedMission,
    PersonQualification,
    PersonUnavailability,
    Runway,
    RunwayAircraftType,
)
from backend.models.progress import TrainingProgress
from backend.schemas.plan import CrewMember, SchedulePlan, Sortie, TrainingDebt
from backend.validator.context import ContextRows, ValidationContext, context_from_rows

SNAPSHOT = "snap_m2b_fixture"
WEEK_START = date(2026, 1, 5)  # 2026W02 周一（v6 §1.2.3 基准周）
WEEK_END = date(2026, 1, 11)
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
SHA_ZERO = "0" * 64

# ─────────────────────────────────────────────────────────────────────
# 实体（v6 §1.3，逐格照 data/origin/*.pdf）
# ─────────────────────────────────────────────────────────────────────
#: (编号, 姓名, 身份, 机型资质, 已完成课目, 类别资质)
PERSONNEL: tuple[tuple[str, str, str, tuple[str, ...], tuple[str, ...], dict[str, str]], ...] = (
    ("P01", "孙军", "教员", ("JL-8", "JL-9"), (), dict.fromkeys("ABCDEFGH", "教员")),
    ("P02", "高超", "教员", ("JL-8", "JL-9"), (), dict.fromkeys("ABCDEFGH", "教员")),
    ("P03", "吴鹏", "教员", ("JL-8", "JL-9"), (), dict.fromkeys("ABCDEFGH", "教员")),
    ("P04", "刘斌", "成熟飞行员", ("JL-8", "JL-9"), (), dict.fromkeys("ABCDEFGH", "单飞")),
    (
        "P05",
        "罗磊",
        "学员",
        ("JL-8",),
        ("missionA-1", "missionA-2", "missionB-1", "missionB-2", "missionC-1"),
        {"A": "单飞", "B": "带飞", "C": "带飞", "F": "带飞"},
    ),
    (
        "P06",
        "张勇",
        "学员",
        ("JL-8",),
        ("missionA-1", "missionA-2"),
        {"A": "单飞", "B": "带飞", "C": "带飞", "F": "带飞"},
    ),
    (
        "P07",
        "陈伟",
        "学员",
        ("JL-8",),
        ("missionA-1", "missionA-2", "missionB-1"),
        {"A": "单飞", "B": "带飞", "C": "带飞", "F": "带飞"},
    ),
    (
        "P08",
        "何超",
        "学员",
        ("JL-8",),
        ("missionA-1",),
        {"A": "单飞", "B": "带飞", "C": "带飞", "F": "带飞"},
    ),
)
#: 教员与成熟飞行员「全 12 门」已完成
ALL_MISSIONS: tuple[str, ...] = (
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
)
JL8_MISSIONS = (
    "missionA-1",
    "missionA-2",
    "missionB-1",
    "missionB-2",
    "missionC-1",
    "missionC-2",
    "missionF-1",
)
JL9_MISSIONS = (
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
)

#: (机号, 机型, 周转分钟, 适配课目)
FLEET: tuple[tuple[str, str, int, tuple[str, ...]], ...] = (
    ("AC10", "JL-8", 30, JL8_MISSIONS),
    ("AC27", "JL-8", 30, JL8_MISSIONS),
    ("AC34", "JL-8", 30, JL8_MISSIONS),
    ("AC49", "JL-8", 30, JL8_MISSIONS),
    ("AC61", "JL-8", 30, JL8_MISSIONS),
    ("AC73", "JL-8", 30, JL8_MISSIONS),  # ⚠️ AC73 是 JL-8，不是 JL-9（v6 F2）
    ("AC84", "JL-9", 40, JL9_MISSIONS),
    ("AC95", "JL-9", 40, JL9_MISSIONS),
)

#: (课目号, 名称, 时长, cycle_weeks, freq_days, 每周必飞, 带飞, 机型, 空域, 先修)
MISSIONS: tuple[
    tuple[str, str, int, int, int, bool, bool, tuple[str, ...], str, tuple[tuple[str, str], ...]],
    ...,
] = (
    ("missionA-1", "本场起落航线", 30, 12, 3, True, False, ("JL-8",), "SAA", ()),
    ("missionA-2", "本场起落航线", 27, 12, 3, True, False, ("JL-8",), "SAB", ()),
    ("missionB-1", "导航飞行", 52, 16, 7, False, True, ("JL-8", "JL-9"), "RT2", (("A", "class"),)),
    ("missionB-2", "导航飞行", 54, 16, 7, False, True, ("JL-8", "JL-9"), "RT1", (("A", "class"),)),
    ("missionC-1", "仪表飞行", 35, 16, 7, False, True, ("JL-8", "JL-9"), "IFR", (("A", "class"),)),
    (
        "missionC-2",
        "仪表飞行",
        56,
        16,
        7,
        False,
        True,
        ("JL-8", "JL-9"),
        "IFR",
        (("missionC-1", "mission"),),
    ),
    (
        "missionD-1",
        "轰炸与射击",
        26,
        16,
        7,
        False,
        True,
        ("JL-9",),
        "RNG",
        (("B", "class"), ("C", "class")),
    ),
    ("missionE-1", "空战机动", 46, 16, 7, False, True, ("JL-9",), "SAA", (("C", "class"),)),
    (
        "missionE-2",
        "空战机动",
        69,
        16,
        7,
        False,
        True,
        ("JL-9",),
        "SAA",
        (("missionE-1", "mission"),),
    ),
    ("missionF-1", "编队飞行", 40, 16, 7, False, True, ("JL-8", "JL-9"), "SAB", (("A", "class"),)),
    (
        "missionG-1",
        "特技飞行",
        35,
        20,
        14,
        False,
        True,
        ("JL-9",),
        "SAB",
        (("A", "class"), ("F", "class")),
    ),
    ("missionH-1", "低空突防", 50, 20, 14, False, True, ("JL-9",), "RT1", (("B", "class"),)),
)

#: (编号, 名称, 同时段容量)
AIRSPACES: tuple[tuple[str, str, int], ...] = (
    ("SAA", "Small Area A", 2),
    ("SAB", "Small Area B", 2),
    ("IFR", "IFR Route", 1),
    ("RT1", "Route 1", 1),
    ("RT2", "Route 2", 1),
    ("RNG", "Range Area", 1),
)

#: RWY-1 服务 JL-8 与 JL-9；RWY-2 只服务 JL-8（**不是** RWY-2=JL-9）
RUNWAYS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("RWY-1", "跑道 1", ("JL-8", "JL-9")),
    ("RWY-2", "跑道 2", ("JL-8",)),
)

MISSION_NAME = {m[0]: m[1] for m in MISSIONS}
MISSION_DURATION = {m[0]: m[2] for m in MISSIONS}
MISSION_AIRSPACE = {m[0]: m[8] for m in MISSIONS}
PERSON_NAME = {p[0]: p[1] for p in PERSONNEL}
AIRCRAFT_TYPE = {a[0]: a[1] for a in FLEET}

#: 学员可及课目集（v6 §1.4.1：D/E/G/H 类因机型 + 类别双重排除）
STUDENT_REACHABLE = JL8_MISSIONS

#: v6 §1.4.2 的 7 条阻塞项：(person_id, mission_id) → 缺失先修
BLOCKED_EXPECTED: dict[tuple[str, str], str] = {
    ("P06", "missionC-2"): "missionC-1",
    ("P07", "missionC-2"): "missionC-1",
    ("P08", "missionB-1"): "missionA-2",
    ("P08", "missionB-2"): "missionA-2",
    ("P08", "missionC-1"): "missionA-2",
    ("P08", "missionF-1"): "missionA-2",
    ("P08", "missionC-2"): "missionC-1",
}

#: 刘斌 C 类到期日（v6 §1.2.1：取总表的 2026-01-07，明细表 02-07 为笔误）
LIU_BIN_C_EXPIRY = date(2026, 1, 7)
LIU_BIN_RECURRENT_SINCE = date(2026, 1, 8)  # 到期次日（S-11）


# ─────────────────────────────────────────────────────────────────────
# 事实装配
# ─────────────────────────────────────────────────────────────────────
def baseline_rows() -> ContextRows:
    """基准周实体的 ORM 行集合。"""
    persons: list[Person] = []
    ptypes: list[PersonAircraftType] = []
    quals: list[PersonQualification] = []
    unavail: list[PersonUnavailability] = []
    completed: list[PersonCompletedMission] = []
    progress: list[TrainingProgress] = []

    for pid, name, identity, types, done, qual_map in PERSONNEL:
        persons.append(Person(person_id=pid, snapshot_id=SNAPSHOT, name=name, identity=identity))
        ptypes.extend(
            PersonAircraftType(person_id=pid, snapshot_id=SNAPSHOT, aircraft_type=t) for t in types
        )
        done_set = set(done) if identity == "学员" else set(ALL_MISSIONS)
        completed.extend(
            PersonCompletedMission(person_id=pid, snapshot_id=SNAPSHOT, mission_id=m)
            for m in sorted(done_set)
        )
        for mission_class, level in qual_map.items():
            expiry = LIU_BIN_C_EXPIRY if (pid == "P04" and mission_class == "C") else None
            quals.append(
                PersonQualification(
                    person_id=pid,
                    snapshot_id=SNAPSHOT,
                    mission_class=mission_class,
                    level=level,
                    expiry_date=expiry,
                )
            )
        # 训练进度
        scope = STUDENT_REACHABLE if identity == "学员" else ALL_MISSIONS
        for mission_id in scope:
            recurrent = pid == "P04" and mission_id in ("missionC-1", "missionC-2")
            progress.append(
                TrainingProgress(
                    person_id=pid,
                    mission_id=mission_id,
                    cycle_start=WEEK_START,
                    status="COMPLETED" if mission_id in done_set else "NOT_STARTED",
                    completed_count=1 if mission_id in done_set else 0,
                    last_done_date=None,  # 原始 PDF 不含该字段 → S-12
                    cycle_weeks=next(m[3] for m in MISSIONS if m[0] == mission_id),
                    debt_count=0,
                    prereq_met=(pid, mission_id) not in BLOCKED_EXPECTED,
                    blocked_reason=(
                        f"{BLOCKED_EXPECTED[(pid, mission_id)]} 未完成"
                        if (pid, mission_id) in BLOCKED_EXPECTED
                        else None
                    ),
                    is_recurrent=recurrent,
                    recurrent_since=LIU_BIN_RECURRENT_SINCE if recurrent else None,
                    snapshot_id=SNAPSHOT,
                )
            )
    unavail.append(
        PersonUnavailability(
            person_id="P03",
            snapshot_id=SNAPSHOT,
            unavailable_date=date(2026, 1, 5),
            reason="不可用",
        )
    )

    aircraft: list[Aircraft] = []
    capability: list[AircraftMissionCapability] = []
    for ac_id, ac_type, turn, missions in FLEET:
        aircraft.append(
            Aircraft(
                aircraft_id=ac_id,
                snapshot_id=SNAPSHOT,
                aircraft_type=ac_type,
                seats=2,
                daily_window_start=time(6, 0),
                daily_window_end=time(18, 0),
                turnaround_minutes=turn,
            )
        )
        capability.extend(
            AircraftMissionCapability(aircraft_id=ac_id, snapshot_id=SNAPSHOT, mission_id=m)
            for m in missions
        )
    maintenance = [
        AircraftMaintenance(
            aircraft_id="AC73",
            snapshot_id=SNAPSHOT,
            start_ts=datetime(2026, 1, 9, 0, 0),
            end_ts=datetime(2026, 1, 9, 23, 59),
            kind="定检维护",
            all_day=True,
        )
    ]

    missions_rows: list[Mission] = []
    mission_types: list[MissionAircraftType] = []
    prereqs: list[MissionPrereq] = []
    for mid, name, dur, cycle, freq, weekly, dual, types, airspace, prereq in MISSIONS:
        missions_rows.append(
            Mission(
                mission_id=mid,
                snapshot_id=SNAPSHOT,
                name=name,
                mission_class=mid[len("mission")],
                kind="训练",
                duration_minutes=dur,
                cycle_weeks=cycle,
                freq_days=freq,
                weekly_required=weekly,
                dual_required=dual,
                airspace_id=airspace,
                frequency_text=f"每 {freq} 天 ≥1 次",
            )
        )
        mission_types.extend(
            MissionAircraftType(mission_id=mid, snapshot_id=SNAPSHOT, aircraft_type=t)
            for t in types
        )
        prereqs.extend(
            MissionPrereq(mission_id=mid, snapshot_id=SNAPSHOT, prereq_ref=ref, ref_kind=kind)
            for ref, kind in prereq
        )

    return ContextRows(
        persons=persons,
        person_aircraft_types=ptypes,
        person_qualifications=quals,
        person_unavailability=unavail,
        person_completed=completed,
        aircraft=aircraft,
        aircraft_capability=capability,
        maintenance=maintenance,
        missions=missions_rows,
        mission_aircraft_types=mission_types,
        mission_prereq=prereqs,
        airspaces=[
            Airspace(airspace_id=a, snapshot_id=SNAPSHOT, name=n, capacity=c)
            for a, n, c in AIRSPACES
        ],
        runways=[Runway(runway_id=r, snapshot_id=SNAPSHOT, name=n) for r, n, _ in RUNWAYS],
        runway_aircraft_types=[
            RunwayAircraftType(runway_id=r, snapshot_id=SNAPSHOT, aircraft_type=t)
            for r, _, types in RUNWAYS
            for t in types
        ],
        progress=progress,
    )


def baseline_context() -> ValidationContext:
    """基准周（2026W02）的校验上下文。"""
    return context_from_rows(baseline_rows(), week_start=WEEK_START, snapshot_id=SNAPSHOT)


# ─────────────────────────────────────────────────────────────────────
# 架次与方案构造
# ─────────────────────────────────────────────────────────────────────
def crew(*members: tuple[str, str]) -> list[CrewMember]:
    return [
        CrewMember(person_id=pid, name=PERSON_NAME[pid], role=role)  # type: ignore[arg-type]
        for pid, role in members
    ]


def make_sortie(
    sortie_id: str,
    day: int,
    takeoff: str,
    mission_id: str,
    aircraft_id: str,
    members: tuple[tuple[str, str], ...],
    *,
    runway_id: str = "RWY-1",
    airspace_id: str | None = None,
    is_recurrent: bool = False,
    landing: str | None = None,
    duration: int | None = None,
    validate: bool = True,
) -> Sortie:
    """构造一个架次。`validate=False` 时绕过 Pydantic 契约（用于构造违规样本）。

    校验器**不能依赖 Pydantic 层**兜底 —— 闸门1 与闸门2 是两道独立的闸门，
    所以违规样本必须能绕过契约层直接喂给 `checks.py`。
    """
    hh, mm = (int(x) for x in takeoff.split(":"))
    dur = duration if duration is not None else MISSION_DURATION[mission_id]
    if landing is None:
        end = time(hh, mm).hour * 60 + time(hh, mm).minute + dur
        land = time(end // 60, end % 60)
    else:
        lh, lm = (int(x) for x in landing.split(":"))
        land = time(lh, lm)
    kwargs = {
        "sortie_id": sortie_id,
        "date": WEEK_START + timedelta(days=day),
        "weekday": WEEKDAYS[day],
        "takeoff": time(hh, mm),
        "landing": land,
        "mission_id": mission_id,
        "mission_name": MISSION_NAME[mission_id],
        "airspace_id": airspace_id or MISSION_AIRSPACE[mission_id],
        "aircraft_id": aircraft_id,
        "runway_id": runway_id,
        "is_recurrent": is_recurrent,
        "crew": crew(*members),
    }
    return Sortie(**kwargs) if validate else Sortie.model_construct(**kwargs)  # type: ignore[arg-type]


def debt(
    person_id: str, mission_id: str, required: int, scheduled: int, relaxed_by: str = "TIER1"
) -> TrainingDebt:
    """一条如实披露的训练欠账（松弛档位下 SOFT/HARD 的分水岭）。"""
    return TrainingDebt(
        person_id=person_id,
        mission_id=mission_id,
        required=required,
        scheduled=scheduled,
        debt=max(0, required - scheduled),
        relaxed_by=relaxed_by,  # type: ignore[arg-type]
    )


def make_plan(sorties: list[Sortie], *, validate: bool = True, **overrides: object) -> SchedulePlan:
    """把架次装进一个 `SchedulePlan` 外壳。

    `validate=False` 用于装载 `make_sortie(validate=False)` 造出的违规架次 ——
    Pydantic 2 会对嵌套的模型实例**重跑** `mode="after"` 校验器，所以外壳也必须
    走 `model_construct`，否则违规样本在进闸门1 之前就被闸门2 拦掉了。
    """
    payload: dict[str, object] = {
        "plan_id": "pl_m2b_fixture",
        "iso_week": "2026W02",
        "week_start": WEEK_START,
        "week_end": WEEK_END,
        "snapshot_id": SNAPSHOT,
        "ruleset_version": "1.3.0",
        "semantics_version": "1.1.0",
        "semantics_switches": {"S-02": "class_level", "S-05": "dual_runway"},
        "runway_model": "dual_runway",
        "relaxation_tier": 0,
        "sorties": sorties,
        "debts": [],
        "blocked_items": [],
        "content_sha256": SHA_ZERO,
    }
    payload.update(overrides)
    if not validate:
        return SchedulePlan.model_construct(**payload)  # type: ignore[arg-type]
    return SchedulePlan(**payload)  # type: ignore[arg-type]


#: 合规样本的 14 个架次（9 带飞 + 5 单飞），手工排出、逐条对照 14 条规则核过。
#:
#: 设计要点（对应 v6 §1.4.3 的推演）：
#: - 何超 missionA-2 排在第 2、5 天 → 覆盖全部 5 个 3 天滑窗，首次执行 ≤ 第 2 天
#: - 罗磊/张勇/陈伟 各 1 次 A 类单飞（约束3 + S-02 + S-13）
#: - 9 个「未完成且先修满足」的带飞组合各 1 次（约束13，freq_days=7）
#: - 每日只排 2 个架次、起飞间隔 ≥7 分钟、每机每日 ≤1 次 → 周转与密度都留足余量
#: - 全周不用 AC73（避开 01-09 定检），不排吴鹏（避开 01-05 不可用）
COMPLIANT_SORTIES: tuple[tuple[str, int, str, str, str, tuple[tuple[str, str], ...], str], ...] = (
    ("S000001", 0, "06:00", "missionA-1", "AC10", (("P05", "单飞"),), "RWY-1"),
    ("S000002", 0, "06:10", "missionB-1", "AC27", (("P01", "教员"), ("P06", "学员")), "RWY-2"),
    ("S000003", 1, "06:00", "missionB-2", "AC10", (("P01", "教员"), ("P06", "学员")), "RWY-1"),
    ("S000004", 1, "06:20", "missionA-2", "AC27", (("P07", "单飞"),), "RWY-2"),
    ("S000005", 2, "06:00", "missionA-2", "AC10", (("P08", "单飞"),), "RWY-1"),
    ("S000006", 2, "06:15", "missionB-2", "AC27", (("P02", "教员"), ("P07", "学员")), "RWY-2"),
    ("S000007", 3, "06:00", "missionC-1", "AC10", (("P01", "教员"), ("P06", "学员")), "RWY-1"),
    ("S000008", 3, "06:40", "missionC-1", "AC27", (("P02", "教员"), ("P07", "学员")), "RWY-2"),
    ("S000009", 4, "06:00", "missionC-2", "AC10", (("P01", "教员"), ("P05", "学员")), "RWY-1"),
    ("S000010", 4, "07:10", "missionF-1", "AC27", (("P02", "教员"), ("P06", "学员")), "RWY-2"),
    ("S000011", 5, "06:00", "missionA-2", "AC10", (("P08", "单飞"),), "RWY-1"),
    ("S000012", 5, "06:10", "missionF-1", "AC27", (("P02", "教员"), ("P05", "学员")), "RWY-2"),
    ("S000013", 6, "06:00", "missionF-1", "AC10", (("P01", "教员"), ("P07", "学员")), "RWY-1"),
    ("S000014", 6, "06:30", "missionA-1", "AC27", (("P06", "单飞"),), "RWY-2"),
)


def compliant_sorties() -> list[Sortie]:
    return [
        make_sortie(sid, day, takeoff, mission, aircraft, members, runway_id=runway)
        for sid, day, takeoff, mission, aircraft, members, runway in COMPLIANT_SORTIES
    ]


def compliant_plan(**overrides: object) -> SchedulePlan:
    """14 条全过的合规样本（含 v6 §1.4.2 的 7 条阻塞项披露）。"""
    payload: dict[str, object] = {
        "blocked_items": [
            {
                "person_id": pid,
                "mission_id": mid,
                "reason": f"先修 {missing} 未完成",
                "missing_prereqs": [missing],
            }
            for (pid, mid), missing in sorted(BLOCKED_EXPECTED.items())
        ]
    }
    payload.update(overrides)
    return make_plan(compliant_sorties(), **payload)


# ─────────────────────────────────────────────────────────────────────
# image 4 的已知违规样例
# ─────────────────────────────────────────────────────────────────────
#: `data/origin/image 4.png` 周一、周二两组，**只保留课目号能映射到实体表的行**。
#:
#: 裁剪理由（v6 §1.2.2：版式图仅作版式基准，内容一律不采信）：
#: - 周二 06:00 AC34 的 `missionD-2` **不存在**于课目表（12 门里没有 D-2）→ 整行删除。
#:   连带后果：v6 §1.2.2 描述的「周二 AC10/AC49/AC34 三架同时 06:00 起飞」在裁剪后
#:   只剩两架，**但 C09 仍然命中** —— 全场 7 分钟间隔口径下，两架同刻起飞的间隔是
#:   0 分钟。
#: - 周二 06:55 AC34 的 `missionE-2`（Large Area C）与周三及以后各行同理裁掉，
#:   本 fixture 只取周一、周二两天，够覆盖 §1.2.2 点名的四类违规。
#: - 图里的空域名（`Range Route 1`、`Large Area C`）在空域表里不存在；保留下来的行
#:   一律取**课目绑定的空域**（空域本就由课目唯一决定，图上那列是错标）。
#: - 图上没有跑道列（Sheet 1~3 不含跑道，v6 §10.4）。JL-9 只能用 RWY-1；这里把
#:   全部架次都放在 RWY-1，是「图上信息不足时取最保守读法」。
IMAGE4_SORTIES: tuple[tuple[str, int, str, str, str, str, tuple[tuple[str, str], ...]], ...] = (
    # 周一：AC84（JL-9）飞限 JL-8 的 missionA-1；且 06:29 着陆后 06:39 就再起飞
    ("S000101", 0, "06:00", "06:29", "missionA-1", "AC84", (("P01", "教员"), ("P07", "学员"))),
    ("S000102", 0, "06:39", "07:35", "missionC-2", "AC84", (("P01", "教员"),)),
    ("S000103", 0, "06:53", "07:20", "missionA-2", "AC73", (("P04", "教员"), ("P05", "学员"))),
    # 周二：两架 06:00 同刻起飞
    ("S000104", 1, "06:00", "06:27", "missionA-2", "AC10", (("P03", "教员"), ("P08", "学员"))),
    ("S000105", 1, "06:00", "06:26", "missionD-1", "AC49", (("P01", "教员"),)),
    ("S000106", 1, "06:37", "07:33", "missionC-2", "AC84", (("P03", "教员"),)),
    ("S000107", 1, "06:55", "07:22", "missionA-2", "AC73", (("P04", "教员"), ("P06", "学员"))),
)


def image4_plan() -> SchedulePlan:
    """`image 4.png` 的已知违规样例（裁剪后）。**禁止**把它当期望输出使用。"""
    sorties = [
        make_sortie(
            sid,
            day,
            takeoff,
            mission,
            aircraft,
            members,
            landing=landing,
            runway_id="RWY-1",
            validate=False,
        )
        for sid, day, takeoff, landing, mission, aircraft, members in IMAGE4_SORTIES
    ]
    return make_plan(sorties, validate=False, plan_id="pl_image4_known_violations")


__all__ = [
    "AIRSPACES",
    "ALL_MISSIONS",
    "BLOCKED_EXPECTED",
    "COMPLIANT_SORTIES",
    "FLEET",
    "IMAGE4_SORTIES",
    "LIU_BIN_C_EXPIRY",
    "LIU_BIN_RECURRENT_SINCE",
    "MISSIONS",
    "MISSION_DURATION",
    "MISSION_NAME",
    "PERSONNEL",
    "PERSON_NAME",
    "RUNWAYS",
    "SHA_ZERO",
    "SNAPSHOT",
    "STUDENT_REACHABLE",
    "WEEKDAYS",
    "WEEK_END",
    "WEEK_START",
    "baseline_context",
    "baseline_rows",
    "compliant_plan",
    "compliant_sorties",
    "crew",
    "debt",
    "image4_plan",
    "make_plan",
    "make_sortie",
]
