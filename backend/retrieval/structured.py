"""路 A · 结构化召回：实体识别 → SQL / 递归 CTE（v6 §6.5.2）。

> 权威、可精确匹配。**结构化召回的结果不参与 RRF 竞争，直接置顶**（§6.5.4）。

## 这一路为什么是达标前提而不是优化项

v6 §12.4 把话说得很直白：

> 撑住总体数的是**语义类 120 条走 SQL 精确通道**：这一路不依赖模型，
> 是把加权值拉过交付线的结构性依靠。……**三路召回中的精确通道不是优化项，
> 是达标前提。**

「何超 / 高超」是本项目的真实风险（§6.5.1）：两个名字在字面和向量空间上都极近，
一个是学员（P08）一个是教员（P02）。混淆会直接导致答错人。这类问题靠调
embedding 模型解决不了，必须靠精确通道兜底。

## 本模块产出两样东西

1. **文档**（`RetrievedDoc`，`authoritative=True`）—— 进上下文，供生成层引用；
2. **结论**（`FactAnswer`）—— 一句**确定性代码算出来的**结论，带引用。

第 2 样是本窗口的关键设计（业务方 2026-08-14 确认）：语义类事实问题的答案
**内容**由这里算出，LLM 只负责组织语言。于是 §12.4 的 M1~M4 四条探针答不答得对
与 14B 当天的发挥无关 —— 与 §12.4 那句「走 SQL 精确通道，不依赖模型」一致。

## 判定用的是既有实现，不另写一份

- 先修达标 → `retrieval.prereq_cte.evaluate_prereq`（S-01 的唯一实现，v6 §6.1）
- 带飞与否 → `mission.dual_required ∧ identity == 学员`（S-08 + D-1）
- 资质到期 → `ruleset.expiry_inclusive`（到期日当日仍可执行）+ S-11（成熟飞行员转复训）

**本模块不是第三个约束实现。** 它回答的是「这个人这门课现在能不能排」这种
单点问题，不做任何跨架次的排布；真正的可行性由 `solver/` 决定、由 `validator/`
独立复核。把它当成排班判据是错的，它只是把 PG 里的事实按规格拼成一句人话。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Final

from sqlalchemy.orm import Session

from backend.core.ruleset import (
    IDENTITY_MATURE,
    IDENTITY_STUDENT,
    Ruleset,
    Semantics,
    get_ruleset,
    get_semantics,
)
from backend.memory import semantic
from backend.retrieval.documents import RetrievedDoc, dedupe
from backend.retrieval.prereq_cte import evaluate_prereq
from backend.schemas.retrieval import Citation

# ─────────────────────────────────────────────────────────────────────
# 问题类型的确定性识别
# ─────────────────────────────────────────────────────────────────────
#
# **这里刻意不用 LLM。** 路 A 的承诺是「权威、可精确匹配」；用模型判问题类型
# 会把一条本该确定的通道变成概率通道，而它恰恰是 §12.4 语义类 ≥98% 的依靠。
# 认不出类型不是错误 —— 那种问题本来就该由路 B/C 的语义召回来答。

_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("qualification_expiry", re.compile(r"到期|复训|有效期|失效|过期")),
    ("qualification_list", re.compile(r"资质|等级|持有|能飞哪些|会飞什么|什么水平")),
    ("eligibility", re.compile(r"能不能|能否|可不可以|可以.*吗|排得上|飞不飞得|准不准")),
    ("crew_requirement", re.compile(r"需要教员|要教员|带飞|单飞|要不要人带|机组")),
    ("aircraft_type", re.compile(r"机型|什么飞机|哪种飞机|型号|是什么机")),
    ("maintenance", re.compile(r"维护|定检|检修|保养")),
    ("unavailability", re.compile(r"不可用|请假|休假|来不了|缺勤")),
    ("completed", re.compile(r"已完成|完成了哪些|飞过哪些|学完")),
    ("progress", re.compile(r"进度|还差|上次.*飞|锚点|欠账")),
    ("airspace_capacity", re.compile(r"空域|容量|同时段")),
)


def classify_question(text: str) -> tuple[str, ...]:
    """一句话可能问了哪几类事实。**可以命中多类**，各自查各自的。

    「刘斌的仪表等级何时到期，他还能不能飞仪表课目」是一句话两个问题，
    v6 §6.5.2 的第 ④ 步「查询分解」说的正是这件事。
    """
    return tuple(kind for kind, pattern in _PATTERNS if pattern.search(text))


# ─────────────────────────────────────────────────────────────────────
# 产物
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class FactAnswer:
    """一条**确定性代码算出来的**结论及其引用。

    `statement` 是要呈现给人的那句话，`citations` 是它的出处。生成层可以改写
    措辞，但改写后的每条断言都要能落回这里的某一条（§6.5.2 第 ④ 步）。
    """

    kind: str
    statement: str
    citations: tuple[Citation, ...] = ()
    #: 结构化判定的机器可读结论（`True` / `False` / `None`=不适用）
    verdict: bool | None = None
    #: 判定依据的规格条目，进解释文本（如 `S-01`、`S-11`、`D-1`）
    basis: tuple[str, ...] = ()


@dataclass(frozen=True)
class StructuredResult:
    """路 A 的完整产物。"""

    docs: tuple[RetrievedDoc, ...] = ()
    answers: tuple[FactAnswer, ...] = ()
    kinds: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def hit(self) -> bool:
        return bool(self.docs or self.answers)


# ─────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────
def structured_recall(
    session: Session,
    snapshot_id: str,
    *,
    query: str,
    person_ids: Sequence[str] = (),
    aircraft_ids: Sequence[str] = (),
    mission_ids: Sequence[str] = (),
    mission_classes: Sequence[str] = (),
    airspace_ids: Sequence[str] = (),
    as_of: date,
    ruleset: Ruleset | None = None,
    semantics: Semantics | None = None,
) -> StructuredResult:
    """按已消解的实体跑精确查询。

    `as_of` 是**必填**的：同一个问题在 2026-01-06 与 2026-01-09 的答案不同
    （刘斌的 C 类资质，v6 §6.4 的活样本）。给它一个默认值等于让「今天是哪天」
    这件事悄悄进入答案 —— 重放时那个答案就再也复现不了（铁律 9）。
    """
    rules = ruleset or get_ruleset()
    sem = semantics or get_semantics()
    kinds = classify_question(query)

    docs: list[RetrievedDoc] = []
    answers: list[FactAnswer] = []
    notes: list[str] = []

    # 把类别展开成课目（只在本快照里存在的那些）
    targets = list(mission_ids)
    for cls in mission_classes:
        targets.extend(m.mission_id for m in semantic.missions_of_class(session, snapshot_id, cls))
    targets = sorted(set(targets))

    persons = [
        p
        for pid in person_ids
        if (p := semantic.person_fact(session, snapshot_id, pid)) is not None
    ]
    aircraft = [
        a
        for aid in aircraft_ids
        if (a := semantic.aircraft_fact(session, snapshot_id, aid)) is not None
    ]
    missions = [
        m for mid in targets if (m := semantic.mission_fact(session, snapshot_id, mid)) is not None
    ]

    # ── 实体画像永远进上下文（哪怕问题类型没认出来）────────────────
    docs.extend(p.doc() for p in persons)
    docs.extend(a.doc() for a in aircraft)
    docs.extend(m.doc() for m in missions)

    # ── M1：资质到期 ────────────────────────────────────────────────
    if "qualification_expiry" in kinds:
        for person in persons:
            for qual in semantic.qualification_facts(
                session,
                snapshot_id,
                person.person_id,
                mission_class=mission_classes[0] if len(mission_classes) == 1 else None,
            ):
                doc = qual.doc()
                docs.append(doc)
                if qual.expiry_date is None:
                    continue
                answers.append(
                    FactAnswer(
                        kind="qualification_expiry",
                        statement=(
                            f"{qual.person_name}（{qual.person_id}）的 {qual.mission_class} 类"
                            f"资质复训到期日是 {qual.expiry_date.isoformat()}。"
                        ),
                        citations=(doc.citation(),),
                        verdict=None,
                        basis=("§5.5 X1",),
                    )
                )

    # ── 资质清单（「何超的资质情况」）──────────────────────────────
    #
    # 这一条正是「何超 / 高超」专项的观测口：路 A 开着时答案里的编号来自
    # `WHERE person_id = 'P08'`，不存在混淆的可能；关掉之后就只剩两条
    # 字面与向量都极近的摘要句在竞争（v6 §6.5.1）。
    if "qualification_list" in kinds:
        for person in persons:
            quals = semantic.qualification_facts(session, snapshot_id, person.person_id)
            docs.extend(q.doc() for q in quals)
            listed = "、".join(
                f"{q.mission_class} 类/{q.level}"
                + (f"（{q.expiry_date.isoformat()} 到期）" if q.expiry_date else "")
                for q in quals
            )
            answers.append(
                FactAnswer(
                    kind="qualification_list",
                    statement=(
                        f"{person.name}（{person.person_id}）的身份是{person.identity}，"
                        f"持有机型 {'、'.join(person.aircraft_types) or '（无）'}；"
                        f"类别资质：{listed or '（无）'}。"
                    ),
                    citations=(person.doc().citation(),),
                )
            )

    # ── M2：机型 ────────────────────────────────────────────────────
    if "aircraft_type" in kinds:
        for plane in aircraft:
            doc = plane.doc()
            answers.append(
                FactAnswer(
                    kind="aircraft_type",
                    statement=f"{plane.aircraft_id} 的机型是 {plane.aircraft_type}。",
                    citations=(doc.citation(),),
                    basis=("§5.5 X2",),
                )
            )

    # ── M3 + 时效样本：能不能排 ──────────────────────────────────────
    if "eligibility" in kinds:
        for person in persons:
            for mission in missions:
                verdict = _eligibility(
                    session,
                    snapshot_id,
                    person=person,
                    mission=mission,
                    as_of=as_of,
                    rules=rules,
                    sem=sem,
                )
                docs.extend(verdict.docs)
                answers.append(verdict.answer)
        if persons and not missions:
            notes.append("问到了「能不能排」但没有指明课目，已按人员画像作答")

    # ── M4：要不要教员 ──────────────────────────────────────────────
    if "crew_requirement" in kinds:
        identity = persons[0].identity if persons else IDENTITY_STUDENT
        for mission in missions:
            answers.append(_crew_requirement(mission, identity=identity, sem=sem))

    # ── 其余事实 ────────────────────────────────────────────────────
    if "completed" in kinds:
        for person in persons:
            done = semantic.completed_missions(session, snapshot_id, person.person_id)
            doc = person.doc()
            answers.append(
                FactAnswer(
                    kind="completed",
                    statement=(
                        f"{person.name}（{person.person_id}）已完成的课目："
                        f"{'、'.join(done) if done else '（无）'}。"
                    ),
                    citations=(doc.citation(),),
                )
            )

    if "progress" in kinds:
        for person in persons:
            for progress in semantic.progress_facts(session, person.person_id):
                if targets and progress.mission_id not in targets:
                    continue
                docs.append(progress.doc())

    if "airspace_capacity" in kinds:
        wanted = frozenset(airspace_ids) or None
        for airspace in semantic.airspace_facts(session, snapshot_id):
            if wanted is not None and airspace.airspace_id not in wanted:
                continue
            doc = airspace.doc()
            docs.append(doc)
            answers.append(
                FactAnswer(
                    kind="airspace_capacity",
                    statement=airspace.sentence() + "。",
                    citations=(doc.citation(),),
                    basis=("S-10",),
                )
            )

    if "maintenance" in kinds:
        for plane in aircraft or []:
            for item in semantic.maintenance_facts(
                session, snapshot_id, aircraft_id=plane.aircraft_id
            ):
                doc = item.doc()
                docs.append(doc)
                answers.append(
                    FactAnswer(
                        kind="maintenance",
                        statement=item.sentence() + "。",
                        citations=(doc.citation(),),
                    )
                )

    if "unavailability" in kinds:
        for person in persons:
            for absence in semantic.unavailability_facts(
                session, snapshot_id, person_id=person.person_id
            ):
                doc = absence.doc()
                docs.append(doc)
                answers.append(
                    FactAnswer(
                        kind="unavailability",
                        statement=absence.sentence() + "。",
                        citations=(doc.citation(),),
                    )
                )

    # ── 兜底：认不出问题类型，但确实提到了某个实体 ──────────────────
    #
    # 「跟我说说何超」不匹配任何模式，但它问的显然是 P08。此时把实体画像
    # 作为结论给出去，好过让答案退化成一堆召回文档的堆叠 —— 而堆叠恰恰是
    # 「何超 / 高超」混淆最容易发生的地方。
    if not answers and (persons or aircraft or missions):
        profiles: list[tuple[str, RetrievedDoc]] = [
            *((p.sentence(), p.doc()) for p in persons),
            *((a.sentence(), a.doc()) for a in aircraft),
            *((m.sentence(), m.doc()) for m in missions),
        ]
        answers.extend(
            FactAnswer(
                kind="entity_profile",
                statement=sentence + "。",
                citations=(doc.citation(),),
            )
            for sentence, doc in profiles
        )

    return StructuredResult(
        docs=tuple(dedupe(docs)),
        answers=tuple(answers),
        kinds=kinds,
        notes=tuple(notes),
    )


# ─────────────────────────────────────────────────────────────────────
# 「能不能排」的判定
# ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Eligibility:
    answer: FactAnswer
    docs: tuple[RetrievedDoc, ...]


def _eligibility(
    session: Session,
    snapshot_id: str,
    *,
    person: semantic.PersonFact,
    mission: semantic.MissionFact,
    as_of: date,
    rules: Ruleset,
    sem: Semantics,
) -> _Eligibility:
    """单点判定：这个人这门课在 `as_of` 这天能不能排。

    **逐条按规格判，顺序即优先级** —— 先看有没有资质，再看机型，再看先修，
    最后看到期。顺序影响的是「给出的理由」，而理由正是本判定的产物之一
    （M3 要的不只是「不能」，还要「缺 missionA-2」）。
    """
    docs: list[RetrievedDoc] = [mission.doc()]
    who = f"{person.name}（{person.person_id}）"
    what = f"{mission.mission_id}（{mission.name}）"

    # ① 类别资质
    qual = person.qualifications.get(mission.mission_class)
    if qual is None:
        return _Eligibility(
            answer=FactAnswer(
                kind="eligibility",
                statement=(f"{who}**不能**排 {what}：他没有 {mission.mission_class} 类资质。"),
                citations=(person.doc().citation(), mission.doc().citation()),
                verdict=False,
                basis=("约束2",),
            ),
            docs=(*docs, person.doc()),
        )
    level, expiry = qual

    # ② 机型
    if mission.aircraft_types and not (set(person.aircraft_types) & set(mission.aircraft_types)):
        return _Eligibility(
            answer=FactAnswer(
                kind="eligibility",
                statement=(
                    f"{who}**不能**排 {what}：该课目需要机型 "
                    f"{'、'.join(mission.aircraft_types)}，而他只持有 "
                    f"{'、'.join(person.aircraft_types) or '（无）'} 的机型资质。"
                ),
                citations=(person.doc().citation(), mission.doc().citation()),
                verdict=False,
                basis=("约束6",),
            ),
            docs=(*docs, person.doc()),
        )

    # ③ 先修（S-01：类引用要求该类**全部**课目完成）
    prereqs = semantic.prereq_map(session, snapshot_id).get(mission.mission_id, [])
    completed = semantic.completed_missions(session, snapshot_id, person.person_id)
    all_ids = [m.mission_id for m in semantic.all_missions(session, snapshot_id)]
    met, missing = evaluate_prereq(prereqs, completed, all_ids)
    if not met:
        refs = "、".join(ref for ref, _ in prereqs) or "（无）"
        return _Eligibility(
            answer=FactAnswer(
                kind="eligibility",
                statement=(
                    f"{who}**不能**排 {what}：先修未达标。该课目的先修是 {refs}，"
                    f"按 S-01「类引用要求该类全部课目完成」展开后还缺 "
                    f"{'、'.join(missing)}。"
                ),
                citations=(person.doc().citation(), mission.doc().citation()),
                verdict=False,
                basis=("S-01", "约束13"),
            ),
            docs=(*docs, person.doc()),
        )

    # ④ 资质到期（约束2 字面 vs S-11 的业务方授权改写）
    if expiry is not None and _expired(as_of, expiry, rules):
        if person.identity in _s11_identities(sem):
            since = expiry + timedelta(days=sem.s11_start_offset_days)
            window_end = since + timedelta(days=sem.s11_window_days - 1)
            return _Eligibility(
                answer=FactAnswer(
                    kind="eligibility",
                    statement=(
                        f"{who}**能**排 {what}，但性质变了：他的 "
                        f"{mission.mission_class} 类资质已于 {expiry.isoformat()} 到期，"
                        f"按 S-11 自 {since.isoformat()} 起转为**强制复训**，"
                        f"滑动窗口 [{since.isoformat()}, {window_end.isoformat()}] "
                        f"内必须至少安排 1 次该类课目。"
                    ),
                    citations=(person.doc().citation(), mission.doc().citation()),
                    verdict=True,
                    basis=("S-11", "S-09"),
                ),
                docs=(*docs, person.doc()),
            )
        return _Eligibility(
            answer=FactAnswer(
                kind="eligibility",
                statement=(
                    f"{who}**不能**排 {what}：其 {mission.mission_class} 类资质已于 "
                    f"{expiry.isoformat()} 到期，按约束2 到期后该资质对应课目不得安排。"
                ),
                citations=(person.doc().citation(), mission.doc().citation()),
                verdict=False,
                basis=("约束2",),
            ),
            docs=(*docs, person.doc()),
        )

    # ⑤ 能飞。已完成的课目**照样能飞**，只是不再被约束13 强制要求（S-03）
    #
    #    ⚠️ 这里曾经写成「已完成 → 不能」，是错的：**S-03 管的是「要不要排」，
    #    不是「能不能飞」。** 一个 C 类资质有效的成熟飞行员当然可以飞 C 类课目，
    #    只是系统不会为了满足频率约束去主动安排他。两件事混起来，
    #    「刘斌 01-06 能不能飞仪表课目」就会答成「不能」——而 v6 §6.4 的活样本
    #    要求那一天的答案是「能（正常执行）」。
    progress = semantic.progress_facts(session, person.person_id, mission_id=mission.mission_id)
    completed_note = ""
    if progress and progress[0].status == "COMPLETED":
        docs.append(progress[0].doc())
        completed_note = (
            "该课目已标记为完成，按 S-03 退出约束13 的频率滑窗，系统不会为满足频率要求主动安排它。"
        )

    dual = _needs_dual(mission, identity=person.identity, sem=sem)
    crew = "需带教员（带飞）" if dual else "单飞"
    return _Eligibility(
        answer=FactAnswer(
            kind="eligibility",
            statement=(
                f"{who}**能**排 {what}：持 {mission.mission_class} 类{level}资质、"
                f"机型匹配、先修达标，资质在 {as_of.isoformat()} 这天正常有效。"
                f"机组编成为{crew}。{completed_note}"
            ),
            citations=(person.doc().citation(), mission.doc().citation()),
            verdict=True,
            basis=("S-01", "S-08", "D-1") + (("S-03",) if completed_note else ()),
        ),
        docs=(*docs, person.doc()),
    )


def _expired(as_of: date, expiry: date, rules: Ruleset) -> bool:
    """到期判定。`expiry_inclusive` 为真时**到期日当日仍可执行**（约束2 原文）。"""
    return as_of > expiry if rules.expiry_inclusive else as_of >= expiry


def _s11_identities(sem: Semantics) -> tuple[str, ...]:
    """S-11 适用的身份。开关关掉时返回空元组 —— 谁都不适用。"""
    if not sem.s11_enabled:
        return ()
    return sem.s11_identities or (IDENTITY_MATURE,)


def _needs_dual(mission: semantic.MissionFact, *, identity: str, sem: Semantics) -> bool:
    """需带飞 = (mission.带飞 == 是) ∧ (身份 == 学员)（S-08 + D-1）。

    A-1/A-2 的带飞列是「否」→ **学员 A 类单飞**，这是 M4 探针的正解，
    也是 CLAUDE.md §11 反模式清单里点名的那条（「让学员 A 类架次带教员」）。
    """
    if not mission.dual_required:
        return False
    if not sem.s08_students_only:
        return True
    return identity == IDENTITY_STUDENT


def _crew_requirement(
    mission: semantic.MissionFact, *, identity: str, sem: Semantics
) -> FactAnswer:
    """M4：这门课要不要教员。"""
    dual = _needs_dual(mission, identity=identity, sem=sem)
    doc = mission.doc()
    subject = "学员" if identity == IDENTITY_STUDENT else identity
    if dual:
        statement = (
            f"{subject}飞 {mission.mission_id}（{mission.name}）**需要**教员带飞："
            f"该课目的「带飞」列为「是」，且执行者是学员（S-08 + D-1）。"
        )
    else:
        reason = (
            "该课目的「带飞」列为「否」"
            if not mission.dual_required
            else f"「带飞」只对学员生效，而{subject}不是学员"
        )
        statement = (
            f"{subject}飞 {mission.mission_id}（{mission.name}）**不需要**教员：{reason}"
            f"（S-08 + D-1）。"
        )
    return FactAnswer(
        kind="crew_requirement",
        statement=statement,
        citations=(doc.citation(),),
        verdict=dual,
        basis=("S-08", "D-1"),
    )


__all__ = [
    "FactAnswer",
    "StructuredResult",
    "classify_question",
    "structured_recall",
]
