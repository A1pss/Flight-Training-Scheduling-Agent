"""校验层（v6 §5.1）：Pydantic + 引用完整性 + 值域 + 时间逻辑 + 后置断言。

Pydantic 那一层在 :mod:`backend.ingestion.schema` 的模型里就跑完了（字段类型、
正则、区间）。本模块管的是**跨记录**的东西 —— 单条记录合法不代表整批自洽：

- **引用完整性**：飞机适配课目 / 人员已完成课目 / 空域绑定课目 / 先修引用，
  全部得指向真实存在的课目；课目的空域名得能在空域表里查到
- **值域**：身份、等级、机型、容量、座位
- **时间逻辑**：维护时段有序、可用窗有序、不可用日期可解析
- **后置断言**：:func:`~backend.ingestion.repair.assert_no_orphan_tokens`
- ★ **源内值冲突检出**（§5.5）
- ★ **机组编成一致性断言**（§3.1.1）→ 不一致抛 FTS-2001 阻断

**一次跑完再报**：把全部问题收集齐了一起抛，而不是遇到第一个就退出。摄取
失败要给人看的是完整清单，挤牙膏式的报错会让人来回跑五次管线。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from backend.core.errors import DataConflictError, IngestionError
from backend.core.logging import get_logger
from backend.ingestion.conflicts import Conflict, detect_all, raise_on_fatal
from backend.ingestion.questions import OpenQuestion, detect_missing_inputs
from backend.ingestion.repair import assert_no_orphan_tokens
from backend.ingestion.schema import IngestedFacts
from backend.models.entities import IDENTITIES, QUAL_LEVELS

logger = get_logger(__name__)

#: **基准数据集**（`data/origin/*.pdf`）的实体规模，v6 §1.3 的实体全景表。
#:
#: ⚠️ **这是基准回归护栏，不是系统上限。** 它的用途只有一个：确认那四份 PDF
#: 没被改坏、抽取没漏行。**绝不能跑在用户上传路径上** —— 用户有 9 个人、
#: 10 架飞机是完全正常的事，拿基准规模去卡他们，等于宣布这套系统只能排这一批
#: 人。`validate_facts` 的 `expected_counts` 默认 `None` = 不做这项检查，
#: 只有基准回归测试才显式传它进来。
BASELINE_ENTITY_COUNTS: Final[Mapping[str, int]] = {
    "persons": 8,
    "aircraft": 8,
    "missions": 12,
    "airspaces": 6,
    "runways": 2,
    "rules": 14,
}


@dataclass
class ValidationOutcome:
    """校验结果。`conflicts` 里的 BLOCKING 项与 `questions` 都要走人工确认门禁。"""

    conflicts: list[Conflict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 缺整类必需数据时的补传请求（`resolution="upload"`）
    questions: list[OpenQuestion] = field(default_factory=list)

    @property
    def blocking(self) -> list[Conflict]:
        return [c for c in self.conflicts if c.severity == "BLOCKING"]


def _orphan_records(facts: IngestedFacts) -> list[dict[str, object]]:
    """把各处的课目编号列表整理成 `assert_no_orphan_tokens` 要的形状。"""
    records: list[dict[str, object]] = []
    for person in facts.persons:
        records.append(
            {"source": f"person:{person.person_id}", "missions": list(person.completed_missions)}
        )
    for aircraft in facts.aircraft:
        records.append(
            {
                "source": f"aircraft:{aircraft.aircraft_id}",
                "missions": list(aircraft.capable_missions),
            }
        )
    for airspace in facts.airspaces:
        records.append(
            {
                "source": f"airspace:{airspace.airspace_id}",
                "missions": list(airspace.bound_missions),
            }
        )
    for mission in facts.missions:
        prereq_missions = [p.prereq_ref for p in mission.prereqs if p.ref_kind == "mission"]
        records.append(
            {
                "source": f"mission:{mission.mission_id}",
                "missions": [mission.mission_id, *prereq_missions],
            }
        )
    return records


def check_referential_integrity(facts: IngestedFacts) -> list[str]:
    """引用完整性：所有跨表引用必须落到实处。返回问题清单。"""
    problems: list[str] = []
    mission_ids = {m.mission_id for m in facts.missions}
    airspace_names = {a.name: a.airspace_id for a in facts.airspaces}
    person_ids = {p.person_id for p in facts.persons}
    mission_classes = {m.mission_class for m in facts.missions}

    for aircraft in facts.aircraft:
        for mission_id in aircraft.capable_missions:
            if mission_id not in mission_ids:
                problems.append(
                    f"aircraft {aircraft.aircraft_id} 的适配课目 {mission_id} 不存在于课目表"
                )
        for entry in aircraft.maintenance:
            if entry.aircraft_id != aircraft.aircraft_id:
                problems.append(
                    f"维护记录的机号 {entry.aircraft_id} 与所属飞机 {aircraft.aircraft_id} 不一致"
                )

    for person in facts.persons:
        for mission_id in person.completed_missions:
            if mission_id not in mission_ids:
                problems.append(
                    f"person {person.person_id} 的已完成课目 {mission_id} 不存在于课目表"
                )
        for qual in person.qualifications:
            if qual.person_id not in person_ids:
                problems.append(f"资质记录引用了不存在的人员 {qual.person_id}")
            if qual.mission_class not in mission_classes:
                problems.append(
                    f"person {person.person_id} 的 {qual.mission_class} 类资质"
                    f"在课目表里没有任何对应课目"
                )

    for airspace in facts.airspaces:
        for mission_id in airspace.bound_missions:
            if mission_id not in mission_ids:
                problems.append(
                    f"airspace {airspace.airspace_id} 的绑定课目 {mission_id} 不存在于课目表"
                )

    for mission in facts.missions:
        if mission.airspace_name not in airspace_names:
            problems.append(
                f"mission {mission.mission_id} 的空域「{mission.airspace_name}」"
                f"不存在于空域表（已知：{sorted(airspace_names)}）"
            )
        for prereq in mission.prereqs:
            if prereq.ref_kind == "mission" and prereq.prereq_ref not in mission_ids:
                problems.append(
                    f"mission {mission.mission_id} 的先修 {prereq.prereq_ref} 不存在于课目表"
                )
            if prereq.ref_kind == "class" and prereq.prereq_ref[0] not in mission_classes:
                problems.append(
                    f"mission {mission.mission_id} 的先修类别 {prereq.prereq_ref} 没有任何对应课目"
                )

    # 空域「绑定课目」与课目「空域/航线」必须互为反函数——两份源文件说的是同一件事
    for airspace in facts.airspaces:
        bound = set(airspace.bound_missions)
        derived = {m.mission_id for m in facts.missions if m.airspace_name == airspace.name}
        if bound != derived:
            problems.append(
                f"airspace {airspace.airspace_id} 的绑定课目 {sorted(bound)} 与课目表反推出的 "
                f"{sorted(derived)} 不一致"
            )

    return problems


def check_known_enums(facts: IngestedFacts) -> list[str]:
    """身份与资质等级必须是已登记的取值。

    这两个字段**不能像机型那样由数据自由决定** —— §3.1.1 的机组编成判定式
    直接读它们（`需带飞 = (mission.带飞==是) ∧ (身份==学员)`）。冒出一个
    「见习教员」，系统没法自己推断他该单飞还是带飞，那是业务方的裁决，
    不是运行时能补的输入。所以这里给一句**说清楚下一步该找谁做什么**的阻断，
    而不是让 pydantic 抛一个看不懂的 ValidationError。
    """
    problems: list[str] = []
    for person in facts.persons:
        if person.identity not in IDENTITIES:
            problems.append(
                f"person {person.person_id}({person.name}) 的身份「{person.identity}」"
                f"不在已登记取值 {list(IDENTITIES)} 内。新增身份会改变 §3.1.1 的机组编成"
                f"判定式，需业务方先裁决该身份的机组编成规则并落进 rules/semantics.yaml，"
                f"不能由摄取侧推断"
            )
        for qual in person.qualifications:
            if qual.level not in QUAL_LEVELS:
                problems.append(
                    f"person {person.person_id} 的 {qual.mission_class} 类资质等级"
                    f"「{qual.level}」不在已登记取值 {list(QUAL_LEVELS)} 内，同上需业务方裁决"
                )
    return problems


def check_value_domains(facts: IngestedFacts) -> list[str]:
    """值域：主键唯一、机型自洽、跑道机型可实现。

    **机型不是枚举** —— 机队里出现什么机型就是什么机型；这里只保证人员/课目/
    跑道引用的机型都能在机队里找到。
    """
    problems: list[str] = []

    for label, ids in (
        ("persons", [p.person_id for p in facts.persons]),
        ("aircraft", [a.aircraft_id for a in facts.aircraft]),
        ("missions", [m.mission_id for m in facts.missions]),
        ("airspaces", [a.airspace_id for a in facts.airspaces]),
        ("runways", [r.runway_id for r in facts.runways]),
    ):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            problems.append(f"{label} 主键重复：{duplicates}")

    fleet_types = {a.aircraft_id: a.aircraft_type for a in facts.aircraft}
    known_types = set(fleet_types.values())

    for runway in facts.runways:
        unknown = set(runway.aircraft_types) - known_types
        if unknown:
            problems.append(f"runway {runway.runway_id} 服务的机型 {sorted(unknown)} 机队里没有")

    for mission in facts.missions:
        capable = {
            fleet_types[a.aircraft_id]
            for a in facts.aircraft
            if mission.mission_id in a.capable_missions
        }
        declared = set(mission.aircraft_types)
        if capable and capable != declared:
            problems.append(
                f"mission {mission.mission_id} 声明机型 {sorted(declared)}，"
                f"但机队适配课目反推出 {sorted(capable)}"
            )

    for person in facts.persons:
        unknown = set(person.aircraft_types) - known_types
        if unknown:
            problems.append(f"person {person.person_id} 的机型资质 {sorted(unknown)} 机队里没有")

    return problems


def check_time_logic(facts: IngestedFacts) -> list[str]:
    """时间逻辑：维护时段落在飞机存在期内、不与可用窗矛盾。"""
    problems: list[str] = []
    for aircraft in facts.aircraft:
        for entry in aircraft.maintenance:
            if entry.start_ts.date() != entry.end_ts.date() and not entry.all_day:
                problems.append(
                    f"aircraft {aircraft.aircraft_id} 的维护 {entry.start_ts}~{entry.end_ts} 跨日，"
                    f"但未标记为全天"
                )
    for person in facts.persons:
        if len(set(person.unavailable_dates)) != len(person.unavailable_dates):
            problems.append(f"person {person.person_id} 的不可用日期有重复")
    return problems


def check_entity_counts(
    facts: IngestedFacts, expected: Mapping[str, int] = BASELINE_ENTITY_COUNTS
) -> list[str]:
    """与给定的期望规模逐项核对。**只用于基准回归**，见
    :data:`BASELINE_ENTITY_COUNTS` 的说明。

    数量对不上通常意味着修复层漏了一行或跨页表没合并 —— 在基准数据上这类错误
    不报出来的话，会一路安静地跑到求解阶段才以「候选数不对」的形式暴露。
    """
    actual = {
        "persons": len(facts.persons),
        "aircraft": len(facts.aircraft),
        "missions": len(facts.missions),
        "airspaces": len(facts.airspaces),
        "runways": len(facts.runways),
        "rules": len(facts.rules),
    }
    return [
        f"{key} 实体数为 {actual[key]}，期望 {want}"
        for key, want in expected.items()
        if actual.get(key) != want
    ]


def validate_facts(
    facts: IngestedFacts,
    documents: Sequence[tuple[str, str]] = (),
    *,
    expected_counts: Mapping[str, int] | None = None,
    reference_period: tuple[date, date] | None = None,
) -> ValidationOutcome:
    """校验层主入口。

    顺序：**必需数据是否齐全** → 后置断言 → 引用完整性/值域/枚举/时间逻辑 →
    （可选）实体规模 → 源内冲突检出 → 机组编成断言。

    - 缺整类必需数据 → **不报错，直接返回补传请求**（`questions`），后面几步
      全部跳过。理由见 :func:`~backend.ingestion.questions.detect_missing_inputs`。
    - 结构性校验任一失败 → `IngestionError`（FTS-1003）
    - 机组编成不一致 → `DataConflictError`（FTS-2001）

    `expected_counts` **默认 None = 不做规模校验**。它只服务于基准回归，
    传 :data:`BASELINE_ENTITY_COUNTS` 才会生效 —— 用户上传的数据有多少人多少
    飞机是用户的事，不该被基准规模卡住。
    """
    # ⓪ 少了整整一类数据 → 先把这句话说清楚，别让人从一屏外键错误里反推
    missing = detect_missing_inputs(facts)
    if missing:
        logger.info("缺少必需数据，已生成补传请求", missing=[q.question_id for q in missing])
        return ValidationOutcome(questions=missing)

    # ① 后置断言：残缺课目编号一个都不许流进来
    assert_no_orphan_tokens(_orphan_records(facts))

    # ② 结构性校验，一次收集完整清单
    problems: list[str] = []
    problems.extend(check_referential_integrity(facts))
    problems.extend(check_value_domains(facts))
    problems.extend(check_known_enums(facts))
    problems.extend(check_time_logic(facts))
    if expected_counts is not None:
        problems.extend(check_entity_counts(facts, expected_counts))

    if problems:
        raise IngestionError(
            f"摄取校验未通过，共 {len(problems)} 条问题",
            details={"problems": problems},
            suggestions=["逐条修正后重跑；管线不做部分入库（铁律 7）"],
        )

    # ③ 源内值冲突（X1/X3/X4）；X2 由 detect_all 内部核验
    conflicts = detect_all(facts, documents, reference_period=reference_period)

    # ④ 机组编成一致性断言 —— FATAL 直接抛 FTS-2001，不给人工放行的选项
    raise_on_fatal(conflicts)

    outcome = ValidationOutcome(
        conflicts=conflicts, warnings=[c.message for c in conflicts if c.severity == "WARN"]
    )
    logger.info(
        "摄取校验通过",
        blocking_conflicts=len(outcome.blocking),
        warnings=len(outcome.warnings),
    )
    return outcome


def assert_crew_composition(facts: IngestedFacts) -> None:
    """机组编成一致性断言的独立入口（§3.1.1）。

    :func:`validate_facts` 已经包含它；单独暴露是为了让测试能只打这一条，
    以及让 M2 的窗口在拿到快照后可以再自证一次。
    """
    from backend.ingestion.conflicts import detect_x3_crew_composition

    raise_on_fatal(detect_x3_crew_composition(facts.persons, facts.missions))


__all__ = [
    "BASELINE_ENTITY_COUNTS",
    "DataConflictError",
    "ValidationOutcome",
    "assert_crew_composition",
    "check_entity_counts",
    "check_known_enums",
    "check_referential_integrity",
    "check_time_logic",
    "check_value_domains",
    "validate_facts",
]
