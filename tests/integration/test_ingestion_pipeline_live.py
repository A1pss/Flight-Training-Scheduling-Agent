"""集成测试：真实 PDF → 入库 → 反查 全链路（v6 §12.1）。

**直连裸装 PG（127.0.0.1:5433），不用 testcontainers** —— 部署形态就是裸装，
测试环境跟它保持一致，否则测的是一个生产里不存在的东西。

每个用例在**独立的 snapshot_id** 下跑，结束后按外键反序清干净，不污染
`--baseline` 产出的基准快照。嵌入一律用 `HashEmbedder`：这里验的是 PG ↔ Chroma
的读写与 metadata 过滤，不是检索质量（那是 M5 的事），没必要为此加载 2.2GB 权重。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.core.config import PROJECT_ROOT
from backend.core.db import session_scope
from backend.ingestion.gate import baseline_answers, baseline_resolutions, review
from backend.ingestion.loader import load_normalized_from_db
from backend.ingestion.pipeline import build_chunks, commit, prepare
from backend.memory.chroma import build_client, upsert_chunks
from backend.memory.embeddings import HashEmbedder
from backend.retrieval.prereq_cte import fetch_prereq_chain

pytestmark = pytest.mark.integration

ORIGIN = PROJECT_ROOT / "data" / "origin"
BASELINE_PDFS = [ORIGIN / n for n in ("personnel.pdf", "aircraft.pdf", "missions.pdf", "rules.pdf")]

#: 落库顺序的逆序，用于清理
_CLEANUP_TABLES = (
    "training_progress",
    "runway_aircraft_types",
    "runways",
    "person_unavailability",
    "person_completed_missions",
    "person_qualifications",
    "person_aircraft_types",
    "persons",
    "aircraft_maintenance",
    "aircraft_mission_capability",
    "aircraft",
    "mission_prereq",
    "mission_aircraft_types",
    "missions",
    "airspaces",
)


def _purge(session: Session, snapshot_id: str) -> None:
    for table in _CLEANUP_TABLES:
        session.execute(text(f"DELETE FROM {table} WHERE snapshot_id = :s"), {"s": snapshot_id})
    session.execute(text("DELETE FROM data_snapshots WHERE snapshot_id = :s"), {"s": snapshot_id})
    session.flush()


@pytest.fixture
def committed() -> Iterator[tuple[Session, str]]:
    """跑一遍完整管线，产出一个临时快照，用完清掉。

    ⚠️ **快照 id 由内容哈希决定（铁律 9），所以这里 commit 出来的 id 和
    `--baseline` 产出的基准快照是同一个。** 收尾时必须先判断它是不是本用例
    新建的：不是的话只恢复 ACTIVE 状态、绝不 purge，否则一跑集成测试就把
    基准快照删了（第一版就踩了这个坑）。
    """
    with session_scope() as session:
        previous_active = session.execute(
            text("SELECT snapshot_id FROM data_snapshots WHERE status = 'ACTIVE'")
        ).scalar_one_or_none()

        # 隔离：本用例不以任何既有 ACTIVE 快照为基线
        prepared = prepare(BASELINE_PDFS, session=None)
        # 四份基准 PDF 没有「课程开始日期」列 → 管线会提问；
        # 这里用 §5.5 同口径的既有裁决（2026-08-09）作答，等价于 `--baseline`。
        decision = review(
            prepared.changeset,
            baseline_resolutions(prepared.changeset, decided_by="pytest"),
            answers=baseline_answers(prepared.changeset),
            approver="pytest",
        )
        assert decision.approved, decision.reasons

        pre_existing = session.execute(
            text("SELECT count(*) FROM data_snapshots WHERE snapshot_id = :s"),
            {"s": prepared.snapshot_id},
        ).scalar_one()

        result = commit(
            prepared,
            decision,
            session,
            ruleset_version="1.3.0",
            embedder=HashEmbedder(),
            write_vectors=False,
        )
        session.flush()
        was_pre_existing = pre_existing > 0 or result.snapshot_id == previous_active
        try:
            yield session, result.snapshot_id
        finally:
            if not was_pre_existing:
                _purge(session, result.snapshot_id)
            if previous_active is not None:
                session.execute(
                    text("UPDATE data_snapshots SET status = 'ACTIVE' WHERE snapshot_id = :s"),
                    {"s": previous_active},
                )
                session.flush()


def test_full_pipeline_lands_every_entity(committed: tuple[Session, str]) -> None:
    """v6 §1.3 的规模：8 人 / 8 机 / 12 课目 / 6 空域 / 2 跑道。"""
    session, snapshot_id = committed
    counts = {
        table: session.execute(
            text(f"SELECT count(*) FROM {table} WHERE snapshot_id = :s"),
            {"s": snapshot_id},
        ).scalar_one()
        for table in ("persons", "aircraft", "missions", "airspaces", "runways")
    }
    assert counts == {"persons": 8, "aircraft": 8, "missions": 12, "airspaces": 6, "runways": 2}


def test_fleet_type_assignment_round_trips(committed: tuple[Session, str]) -> None:
    """⚠️ AC73 是 JL-8；JL-9 只有 AC84/AC95。"""
    session, snapshot_id = committed
    rows = session.execute(
        text(
            "SELECT aircraft_type, string_agg(aircraft_id, ',' ORDER BY aircraft_id) "
            "FROM aircraft WHERE snapshot_id = :s GROUP BY aircraft_type ORDER BY aircraft_type"
        ),
        {"s": snapshot_id},
    ).all()
    assert dict(rows) == {  # type: ignore[arg-type]
        "JL-8": "AC10,AC27,AC34,AC49,AC61,AC73",
        "JL-9": "AC84,AC95",
    }


def test_x1_resolution_is_persisted_as_adjudicated(committed: tuple[Session, str]) -> None:
    """人工确认后落库的是裁定值 2026-01-07，不是明细表的 2026-02-07。"""
    session, snapshot_id = committed
    expiry = session.execute(
        text(
            "SELECT expiry_date FROM person_qualifications "
            "WHERE snapshot_id = :s AND person_id = 'P04' AND mission_class = 'C'"
        ),
        {"s": snapshot_id},
    ).scalar_one()
    assert expiry.isoformat() == "2026-01-07"


def test_runway_aircraft_mapping(committed: tuple[Session, str]) -> None:
    session, snapshot_id = committed
    rows = session.execute(
        text(
            "SELECT runway_id, string_agg(aircraft_type, ',' ORDER BY aircraft_type) "
            "FROM runway_aircraft_types WHERE snapshot_id = :s "
            "GROUP BY runway_id ORDER BY runway_id"
        ),
        {"s": snapshot_id},
    ).all()
    assert dict(rows) == {"RWY-1": "JL-8,JL-9", "RWY-2": "JL-8"}  # type: ignore[arg-type]


def test_airspace_capacities(committed: tuple[Session, str]) -> None:
    session, snapshot_id = committed
    rows = session.execute(
        text("SELECT airspace_id, capacity FROM airspaces WHERE snapshot_id = :s"),
        {"s": snapshot_id},
    ).all()
    assert dict(rows) == {"SAA": 2, "SAB": 2, "IFR": 1, "RT1": 1, "RT2": 1, "RNG": 1}  # type: ignore[arg-type]


def test_ac73_maintenance_landed(committed: tuple[Session, str]) -> None:
    session, snapshot_id = committed
    row = session.execute(
        text(
            "SELECT aircraft_id, start_ts::date, all_day FROM aircraft_maintenance "
            "WHERE snapshot_id = :s"
        ),
        {"s": snapshot_id},
    ).one()
    assert row.aircraft_id == "AC73"
    assert row[1].isoformat() == "2026-01-09"
    assert row.all_day is True


def test_wu_peng_unavailable_date_landed(committed: tuple[Session, str]) -> None:
    session, snapshot_id = committed
    rows = session.execute(
        text(
            "SELECT person_id, unavailable_date FROM person_unavailability WHERE snapshot_id = :s"
        ),
        {"s": snapshot_id},
    ).all()
    assert len(rows) == 1
    assert rows[0].person_id == "P03"
    assert rows[0].unavailable_date.isoformat() == "2026-01-05"


def test_training_progress_matches_the_seven_blocked_items(
    committed: tuple[Session, str],
) -> None:
    """v6 §1.4.2 的 7 条阻塞项，逐条吻合。"""
    session, snapshot_id = committed
    rows = session.execute(
        text(
            "SELECT person_id, mission_id FROM training_progress "
            "WHERE snapshot_id = :s AND NOT prereq_met ORDER BY person_id, mission_id"
        ),
        {"s": snapshot_id},
    ).all()
    assert [(r.person_id, r.mission_id) for r in rows] == [
        ("P06", "missionC-2"),
        ("P07", "missionC-2"),
        ("P08", "missionB-1"),
        ("P08", "missionB-2"),
        ("P08", "missionC-1"),
        ("P08", "missionC-2"),
        ("P08", "missionF-1"),
    ]


def test_last_done_date_is_null_everywhere(committed: tuple[Session, str]) -> None:
    """S-12：原始 PDF 没有这个字段，摄取侧必须留 NULL，不许编日期、不许写 gap=999。"""
    session, snapshot_id = committed
    non_null = session.execute(
        text(
            "SELECT count(*) FROM training_progress "
            "WHERE snapshot_id = :s AND last_done_date IS NOT NULL"
        ),
        {"s": snapshot_id},
    ).scalar_one()
    assert non_null == 0


def test_is_recurrent_left_to_compile_spec(committed: tuple[Session, str]) -> None:
    """S-11 的写入点在 compile_spec_node（v6 §6.3），摄取侧不碰。"""
    session, snapshot_id = committed
    recurrent = session.execute(
        text("SELECT count(*) FROM training_progress WHERE snapshot_id = :s AND is_recurrent"),
        {"s": snapshot_id},
    ).scalar_one()
    assert recurrent == 0


def test_readback_round_trips_to_the_same_content_hash(
    committed: tuple[Session, str],
) -> None:
    """写进去的和读出来的必须一致 —— 否则 Diff 层每次都会报假变更。"""
    from backend.ingestion.diff import content_sha256

    session, snapshot_id = committed
    readback = load_normalized_from_db(session, snapshot_id)
    # 读回来的规范化结构自己和自己稳定
    assert content_sha256(readback) == content_sha256(load_normalized_from_db(session, snapshot_id))
    assert set(readback["person"]) == {f"P0{i}" for i in range(1, 9)}
    assert readback["person"]["P08"]["completed_missions"] == ["missionA-1"]
    assert readback["aircraft"]["AC73"]["aircraft_type"] == "JL-8"
    assert readback["runway"]["RWY-1"]["aircraft_types"] == ["JL-8", "JL-9"]


def test_diff_against_committed_snapshot_is_empty_except_x1(
    committed: tuple[Session, str],
) -> None:
    """再摄取一次：除了 X1 那条（parser 不裁决，冲突每次都会重新浮出来），
    其余实体应当逐字相同。"""
    from backend.ingestion.diff import diff_normalized, normalize_facts
    from backend.ingestion.loader import load_snapshot_normalized

    session, snapshot_id = committed
    baseline = load_snapshot_normalized(session, snapshot_id)
    prepared = prepare(BASELINE_PDFS, session=None)

    changes = diff_normalized(baseline, normalize_facts(prepared.facts))
    assert [(c.entity_type, c.entity_id, c.kind) for c in changes] == [
        ("person", "P04", "MODIFIED")
    ]
    assert changes[0].changed_fields == ("qualifications",)


def test_prereq_chain_cte(committed: tuple[Session, str]) -> None:
    """递归 CTE 在真库上跑通，且在类引用处停住（展开归 compile_spec）。"""
    session, snapshot_id = committed

    c2 = fetch_prereq_chain(session, "missionC-2", snapshot_id)
    assert [(e.depth, e.prereq_ref, e.ref_kind) for e in c2] == [
        (1, "missionC-1", "mission"),
        (2, "A类", "class"),
    ]

    d1 = fetch_prereq_chain(session, "missionD-1", snapshot_id)
    assert [(e.depth, e.prereq_ref) for e in d1] == [(1, "B类"), (1, "C类")]

    g1 = fetch_prereq_chain(session, "missionG-1", snapshot_id)
    assert [(e.depth, e.prereq_ref) for e in g1] == [(1, "A类"), (1, "F类")]

    assert fetch_prereq_chain(session, "missionA-1", snapshot_id) == []


def test_prereq_cte_respects_depth_limit(committed: tuple[Session, str]) -> None:
    session, snapshot_id = committed
    assert fetch_prereq_chain(session, "missionC-2", snapshot_id, max_depth=1) == [
        e for e in fetch_prereq_chain(session, "missionC-2", snapshot_id) if e.depth == 1
    ]


def test_snapshot_is_active_and_audited(committed: tuple[Session, str]) -> None:
    session, snapshot_id = committed
    status, confirmed_by = session.execute(
        text("SELECT status, confirmed_by FROM data_snapshots WHERE snapshot_id = :s"),
        {"s": snapshot_id},
    ).one()
    assert status == "ACTIVE"
    assert confirmed_by == "pytest"

    audited = session.execute(
        text(
            "SELECT count(*) FROM audit_log "
            "WHERE resource_type = 'data_snapshot' AND resource_id = :s"
        ),
        {"s": snapshot_id},
    ).scalar_one()
    assert audited >= 1


def test_chroma_round_trip_with_metadata_filter() -> None:
    """向量写入 → 按 metadata 过滤取回 → field_map 能回指 PG 主键。"""
    from backend.memory.chroma import field_map_of

    snapshot_id = f"snap_test_{uuid.uuid4().hex[:8]}"
    prepared = prepare(BASELINE_PDFS, session=None)
    chunks = build_chunks(prepared.facts, snapshot_id=snapshot_id, ruleset_version="1.3.0")

    written = upsert_chunks(chunks, embedder=HashEmbedder())
    assert written == {"rule_texts": 14, "entity_summaries": 36}

    client = build_client()
    try:
        got = client.get_collection("entity_summaries").get(
            where={"snapshot_id": snapshot_id}, include=["documents", "metadatas"]
        )
        assert len(got["ids"]) == 36
        person = next(
            m for m in got["metadatas"] if m["entity_type"] == "person" and m["entity_id"] == "P08"
        )
        assert field_map_of(person)["pk"] == {"person_id": "P08", "snapshot_id": snapshot_id}

        rules = client.get_collection("rule_texts").get(ids=["rule:9"], include=["metadatas"])
        assert rules["metadatas"][0]["rule_id"] == 9
    finally:
        client.get_collection("entity_summaries").delete(ids=[c.chunk_id for c in chunks])
        client.get_collection("rule_texts").delete(ids=[c.chunk_id for c in chunks])


def test_pipeline_refuses_to_commit_without_approval() -> None:
    """人工确认是硬性门禁 —— 没批准就落库是不可能的。"""
    from backend.core.errors import IngestionError
    from backend.ingestion.gate import GateDecision

    with session_scope() as session:
        prepared = prepare(BASELINE_PDFS, session=None)
        with pytest.raises(IngestionError, match="人工确认未通过"):
            commit(
                prepared,
                GateDecision(outcome="REJECTED", reasons=["测试驳回"]),
                session,
                ruleset_version="1.3.0",
                write_vectors=False,
            )


def test_prepare_is_reproducible() -> None:
    """铁律 9：同样输入 → 同样 snapshot_id（内容哈希，不含时间戳）。"""
    first = prepare(BASELINE_PDFS, session=None)
    second = prepare(BASELINE_PDFS, session=None)
    assert first.snapshot_id == second.snapshot_id
    assert first.content_sha256 == second.content_sha256


def test_baseline_snapshot_exists_from_cli() -> None:
    """`python -m backend.ingestion.cli --baseline` 的产物应当在库里且为 ACTIVE。"""
    with session_scope() as session:
        row = session.execute(
            text("SELECT snapshot_id FROM data_snapshots WHERE status = 'ACTIVE'")
        ).first()
        assert row is not None, "未找到 ACTIVE 快照，请先跑 --baseline"
