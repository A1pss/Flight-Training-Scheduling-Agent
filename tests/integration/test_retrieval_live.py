"""检索管线的真库实测（v6 §6.5 / §12.4）。

本文件是 M5 出口标准的证据所在，四组：

1. **v6 §12.4 的四条易错事实探针 M1~M4** —— 这四条是本项目真实踩过的坑；
2. **刘斌 C 类资质的时效样本**（§6.4）—— 同一问题两个时点，都是「能」但理由不同；
3. **「何超 vs 高超」专项 + 去 SQL 精确路消融**（§12.4 消融第一条的预演）；
4. **路 A 置顶 / 时间过滤 / 三路开关 / 步数熔断** 各自的边界。

⚠️ **本文件不报任何检索指标**（Recall@5 / MRR@10）。正式测量要 W11 的 320 条
探针集，这里跑的是**功能正确性验证**（CLAUDE.md 铁律 6）。

⚠️ 嵌入与精排一律用确定性替身（`tests/fixtures/retrieval_fixtures.py` 有理由）。
真模型的对照实测见收工报告 §5。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.orm import Session

from backend.agents.knowledge import KNOWLEDGE_MAX_STEPS, ask
from backend.core.db import session_scope
from backend.retrieval.documents import RetrievedDoc, structured_doc
from backend.retrieval.pipeline import RetrievalConfig, retrieve
from backend.retrieval.rrf import fuse
from backend.routing.entities import EntityDirectory, directory_from_session
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot
from tests.fixtures.retrieval_fixtures import NeverStoppingHarness, RetrievalRig, build_rig

pytestmark = pytest.mark.integration

#: 基准周（SPEC_DECISIONS §C.3）
MONDAY = date(2026, 1, 5)


@pytest.fixture(scope="module")
def snapshot() -> str:
    with session_scope() as session:
        snapshot_id = ensure_baseline_snapshot(session)
        session.commit()
    return snapshot_id


@pytest.fixture
def session(snapshot: str) -> Session:
    with session_scope() as s:
        yield s


@pytest.fixture
def directory(session: Session, snapshot: str) -> EntityDirectory:
    return directory_from_session(session, snapshot)


@pytest.fixture
def rig(session: Session, snapshot: str) -> RetrievalRig:
    return build_rig(session, snapshot)


def answer_of(
    question: str,
    session: Session,
    snapshot: str,
    directory: EntityDirectory,
    rig: RetrievalRig,
    *,
    as_of: date | None = None,
    config: RetrievalConfig | None = None,
) -> str:
    return ask(
        question,
        session=session,
        snapshot_id=snapshot,
        directory=directory,
        today=MONDAY,
        as_of=as_of,
        harness=None,
        config=config,
        **rig.kwargs(),
    ).text


# ─────────────────────────────────────────────────────────────────────
# ① v6 §12.4 的四条易错事实探针
# ─────────────────────────────────────────────────────────────────────
def test_probe_m1_liu_bin_instrument_rating_expires_on_january_7th(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """M1：**2026-01-07**（总表），不是明细表的 02-07。错答会让复训窗口整体偏移一个月。"""
    text = answer_of("刘斌的仪表等级何时到期？", session, snapshot, directory, rig)
    assert "2026-01-07" in text
    assert "2026-02-07" not in text


def test_probe_m2_ac73_is_a_jl8(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """M2：**JL-8**，不是 JL-9。错答会让周转时间与跑道可用性全错。"""
    text = answer_of("AC73 是什么机型？", session, snapshot, directory, rig)
    assert "JL-8" in text
    assert "AC73 的机型是 JL-9" not in text


def test_probe_m3_he_chao_cannot_take_mission_b1_missing_a2(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """M3：**不能**，A 类先修未达标（缺 missionA-2）。错答会排出违反约束13 的架次。"""
    text = answer_of("何超能不能排 missionB-1？", session, snapshot, directory, rig)
    assert "不能" in text
    assert "missionA-2" in text
    assert "S-01" in text, "理由要落在具体条款上"


def test_probe_m4_students_fly_mission_a1_solo(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """M4：**不需要**教员（带飞列为否）。错答会让教员容量估算偏高 4 倍。"""
    text = answer_of("学员飞 missionA-1 需要教员吗？", session, snapshot, directory, rig)
    assert "不需要" in text
    assert "带飞" in text


# ─────────────────────────────────────────────────────────────────────
# ② 时效样本：刘斌的 C 类资质（v6 §6.4 的活样本）
# ─────────────────────────────────────────────────────────────────────
def test_liu_bin_can_fly_instrument_missions_on_both_dates_for_different_reasons(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """同一问题、两个时点，**都是「能」但理由不同**。

    - 2026-01-06：资质尚未到期（到期日当日仍可执行），正常执行；
    - 2026-01-09：已于 01-07 到期 → S-11 转**强制复训**，窗口 [01-08, 01-14]。
    """
    question = "刘斌能不能飞仪表课目？"
    before = answer_of(question, session, snapshot, directory, rig, as_of=date(2026, 1, 6))
    after = answer_of(question, session, snapshot, directory, rig, as_of=date(2026, 1, 9))

    assert "能" in before and "不能" not in before
    assert "正常有效" in before
    assert "S-11" not in before, "01-06 还没到期，不该提复训"

    assert "能" in after
    assert "S-11" in after and "强制复训" in after
    assert "2026-01-07" in after and "2026-01-14" in after
    assert before != after, "两个时点的理由必须不同 —— 这正是 §6.4 要验的东西"


# ─────────────────────────────────────────────────────────────────────
# ③ 「何超 vs 高超」专项 + 去 SQL 精确路消融（v6 §12.4 消融第一条）
# ─────────────────────────────────────────────────────────────────────
def test_path_a_tells_the_two_near_homophones_apart(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """路 A 走 `WHERE person_id = ...`，两个名字不存在混淆的可能。"""
    he = answer_of("何超的资质情况", session, snapshot, directory, rig)
    gao = answer_of("高超的资质情况", session, snapshot, directory, rig)

    assert "何超（P08）" in he and "学员" in he
    assert "P02" not in he, "何超的答案里不该出现高超的编号"

    assert "高超（P02）" in gao and "教员" in gao
    assert "P08" not in gao


def test_ablating_path_a_loses_the_verdict_entirely(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """消融「去 SQL 精确路」：**召回得到，却答不出**。

    这是本项目里这条消融的真实失效形态 —— 不是「检索不到文档」，而是
    「跨表连接 + S-01 展开得出的结论，任何一句摘要里都没有」。
    """
    with_a = ask(
        "何超能不能排 missionB-1？",
        session=session,
        snapshot_id=snapshot,
        directory=directory,
        today=MONDAY,
        harness=None,
        config=RetrievalConfig.from_settings(),
        **rig.kwargs(),
    )
    without_a = ask(
        "何超能不能排 missionB-1？",
        session=session,
        snapshot_id=snapshot,
        directory=directory,
        today=MONDAY,
        harness=None,
        config=RetrievalConfig.from_settings(enable_structured=False),
        **rig.kwargs(),
    )

    assert len(with_a.retrieval.answers) >= 1
    assert "不能" in with_a.text and "missionA-2" in with_a.text

    assert without_a.retrieval.answers == (), "关掉路 A 就没有任何结构化结论"
    assert without_a.retrieval.per_route["structured"] == 0
    # 「不能 + 缺 missionA-2」是跨表连接 + S-01 展开得出的结论，任何一句摘要里都没有。
    # 关掉路 A 之后回答退化成召回内容的直陈 —— 它可能把 missionA-2 当作某架飞机
    # 「可执行课目」列的一项列出来，但**给不出判定**。
    assert "不能" not in without_a.text
    assert "S-01" not in without_a.text
    assert "检索到以下相关内容" in without_a.text


def test_ablating_path_a_lets_the_confusable_entity_into_the_top5(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """§6.5.1 预言的那件事：关掉精确通道，高超就挤进了何超的上下文。"""
    without_a = ask(
        "何超的资质情况",
        session=session,
        snapshot_id=snapshot,
        directory=directory,
        today=MONDAY,
        harness=None,
        config=RetrievalConfig.from_settings(enable_structured=False),
        **rig.kwargs(),
    )
    top5 = [d.doc_id for d in without_a.retrieval.contexts[:5]]
    assert any("P02" in doc_id for doc_id in top5), (
        "近音近形实体混入 Top-5 —— 这正是精确通道不可替代的证据"
    )
    assert without_a.retrieval.answers == ()


# ─────────────────────────────────────────────────────────────────────
# ④ 三路可独立开关（为 W13 的消融做准备）
# ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({}, ("structured", "bm25", "vector")),
        ({"enable_structured": False}, ("bm25", "vector")),
        ({"enable_bm25": False}, ("structured", "vector")),
        ({"enable_vector": False}, ("structured", "bm25")),
        ({"enable_bm25": False, "enable_vector": False}, ("structured",)),
    ],
)
def test_each_route_can_be_switched_off_independently(
    session: Session,
    snapshot: str,
    directory: EntityDirectory,
    rig: RetrievalRig,
    flags: dict[str, bool],
    expected: tuple[str, ...],
) -> None:
    """关掉的那一路**一次都不跑**，不是跑完再丢弃 —— 否则消融测出的延迟是假的。"""
    config = RetrievalConfig.from_settings(**flags)
    result = retrieve(
        "何超的资质情况",
        session=session,
        snapshot_id=snapshot,
        directory=directory,
        today=MONDAY,
        harness=None,
        config=config,
        **rig.kwargs(),
    )
    assert config.enabled_routes == expected
    for route in ("structured", "bm25", "vector"):
        if route not in expected:
            assert result.per_route[route] == 0
    assert all("已关闭" in n for n in result.notes if "消融配置" in n)


def test_disabling_query_rewrite_is_also_a_switch(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """v6 §12.4 消融第二条：去查询改写。"""
    result = retrieve(
        "何超的资质情况",
        session=session,
        snapshot_id=snapshot,
        directory=directory,
        today=MONDAY,
        harness=None,
        config=RetrievalConfig.from_settings(enable_rewrite=False),
        **rig.kwargs(),
    )
    assert result.query.resolved_entities == [], "不改写就没有实体消解"
    assert result.per_route["structured"] == 0, "路 A 没有实体可查"
    assert any("查询改写已关闭" in n for n in result.notes)


# ─────────────────────────────────────────────────────────────────────
# ⑤ 路 A 置顶：向量召回的旧摘要 vs SQL 精确结果
# ─────────────────────────────────────────────────────────────────────
def test_a_stale_vector_summary_never_outranks_the_sql_truth(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """构造 §6.5.4 点名的那个冲突场景：旧摘要说 02-07，PG 说 01-07。"""
    stale = [
        RetrievedDoc(
            doc_id="ent:person:P04@old",
            text="刘斌（P04）的 C 类仪表等级到期日 2026-02-07",  # ← 已被裁定为笔误
            source_kind="vector",
            score=0.99,
        )
    ]
    truth = [
        structured_doc(
            "pg:person_qualifications:P04:C",
            "刘斌（P04）的 C 类资质等级为单飞，复训到期日 2026-01-07",
            table="person_qualifications",
            pk={"person_id": "P04", "mission_class": "C"},
        )
    ]
    entries = fuse([truth, stale], top_k=10)
    assert entries[0].doc.doc_id.startswith("pg:"), "SQL 精确结果必须置顶"
    assert entries[0].pinned

    # 端到端复核：真库上问同一个问题，答案取的是 01-07
    text = answer_of("刘斌的仪表等级何时到期？", session, snapshot, directory, rig)
    assert "2026-01-07" in text and "2026-02-07" not in text


# ─────────────────────────────────────────────────────────────────────
# ⑥ KnowledgeAgent 的步数上限（v6 §7.2.2）
# ─────────────────────────────────────────────────────────────────────
def test_step_limit_is_six_and_it_actually_fuses(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """模型不肯停时，第 6 步熔断并用已查到的内容作答 —— **不是抛异常**。"""
    harness = NeverStoppingHarness()
    outcome = ask(
        "何超的资质情况",
        session=session,
        snapshot_id=snapshot,
        directory=directory,
        today=MONDAY,
        harness=harness,  # type: ignore[arg-type]
        **rig.kwargs(),
    )
    assert KNOWLEDGE_MAX_STEPS == 6
    assert outcome.steps == 6
    assert outcome.steps_exhausted is True
    assert harness.react_rounds == 6, "刚好 6 轮，不多不少"
    assert outcome.text, "熔断之后照样有答案"
    assert any("步数上限" in n for n in outcome.notes)


def test_without_a_harness_the_agent_still_answers_deterministically(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """FTS-4001：没有 LLM 也能答，自治那一层没了而已。"""
    outcome = ask(
        "AC73 是什么机型？",
        session=session,
        snapshot_id=snapshot,
        directory=directory,
        today=MONDAY,
        harness=None,
        **rig.kwargs(),
    )
    assert outcome.autonomous is False
    assert outcome.steps == 0
    assert outcome.llm_calls == 0
    assert "JL-8" in outcome.text


def test_the_agent_is_reproducible(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """同输入同输出（铁律 9）。"""
    kwargs = dict(
        session=session,
        snapshot_id=snapshot,
        directory=directory,
        today=MONDAY,
        harness=None,
        **rig.kwargs(),
    )
    first = ask("何超能不能排 missionB-1？", **kwargs)  # type: ignore[arg-type]
    second = ask("何超能不能排 missionB-1？", **kwargs)  # type: ignore[arg-type]
    assert first.text == second.text
    assert [d.doc_id for d in first.retrieval.contexts] == [
        d.doc_id for d in second.retrieval.contexts
    ]


def test_ambiguous_person_asks_back_instead_of_answering(
    session: Session, snapshot: str, directory: EntityDirectory, rig: RetrievalRig
) -> None:
    """名录里没有「郝超」，两个候选并列 —— 反问，不挑一个。"""
    from backend.routing.entities import resolve_person

    resolution = resolve_person("郝超", directory)
    assert resolution.reason == "ambiguous"
    assert {c.entity_id for c in resolution.candidates} == {"P02", "P08"}
