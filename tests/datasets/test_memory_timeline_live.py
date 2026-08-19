"""在真库上验证 `memory_320` 的 gold id **确实存在**（v6 §12.4）。

## 这条测试挡的是什么

探针集里的「期望召回的文档 id 集合」如果只是我按格式编出来的字符串，那么整个
Recall@5 会是 0 而看起来像是检索太差 —— 排查方向会完全跑偏。所以 gold 必须
由**真实写入的记录**倒推：时间线跑一遍 `record_episode` / `distill`，
再拿探针里的每个 id 去库里查。

这也顺带验了两件事：情景记忆的内容寻址 id 是**可预先算出**的；两次蒸馏确实
产生了同一 key 的两个版本（时效探针的前提）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.core.db import session_scope
from backend.memory.procedural import NAMESPACE_RELAXATION, get_preference
from backend.models.memory import EpisodicMemory, ProceduralMemory
from backend.retrieval.corpus import build_corpus
from tests.datasets.memory_catalog import at_hour, timeline_records
from tests.datasets.memory_probes import build_full
from tests.datasets.memory_seed import seed_timeline
from tests.fixtures.baseline_snapshot import ensure_baseline_snapshot

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def seeded() -> tuple[str, dict[str, object]]:
    """把时间线写进库，跑完两次蒸馏，最后**回滚** —— 不留痕迹给别的测试。"""
    with session_scope() as session:
        snapshot_id = ensure_baseline_snapshot(session)
        session.execute(delete(ProceduralMemory))
        session.execute(delete(EpisodicMemory).where(EpisodicMemory.session_id.startswith("m9a-")))
        session.flush()
        seed_timeline(session)
        corpus = build_corpus(session, snapshot_id)
        payload: dict[str, object] = {
            "episodic_ids": set(session.scalars(select(EpisodicMemory.memory_id))),
            "procedural_keys": {
                (row.namespace, row.key) for row in session.scalars(select(ProceduralMemory))
            },
            "corpus_ids": set(corpus.ids),
            "tier_at_week8": _tier(session, at_hour(8, 23)),
            "tier_at_week4": _tier(session, at_hour(4, 23)),
            "tier_at_week20": _tier(session, at_hour(20, 23)),
            "procedural_versions": len(
                list(
                    session.scalars(
                        select(ProceduralMemory).where(
                            ProceduralMemory.namespace == NAMESPACE_RELAXATION
                        )
                    )
                )
            ),
        }
        session.rollback()
    return snapshot_id, payload


def _tier(session: Session, at: object) -> int | None:
    view = get_preference(session, namespace=NAMESPACE_RELAXATION, key="preferred_tier", at=at)  # type: ignore[arg-type]
    if view is None:
        return None
    return int(view.value["tier"])


def test_timeline_writes_all_records(seeded: tuple[str, dict[str, object]]) -> None:
    _snapshot, payload = seeded
    written = payload["episodic_ids"]
    assert isinstance(written, set)
    expected = {record.memory_id() for record in timeline_records()}
    assert expected <= written
    assert len(expected) == 122


def test_every_episodic_gold_id_exists(seeded: tuple[str, dict[str, object]]) -> None:
    """探针里的 `epi:` gold **一个都不许是编的**。"""
    _snapshot, payload = seeded
    written = payload["episodic_ids"]
    assert isinstance(written, set)
    gold = {
        doc_id.removeprefix("epi:")
        for item in build_full()
        for doc_id in item["expected_doc_ids"]
        if doc_id.startswith("epi:")
    }
    assert gold, "探针集里一条情景 gold 都没有，分层错了"
    assert gold <= written


def test_every_entity_and_rule_gold_id_exists(seeded: tuple[str, dict[str, object]]) -> None:
    """`ent:` 与 `rule:` 的 gold 必须真的在语料里。"""
    _snapshot, payload = seeded
    corpus_ids = payload["corpus_ids"]
    assert isinstance(corpus_ids, set)
    gold = {
        doc_id
        for item in build_full()
        for doc_id in item["expected_doc_ids"]
        if doc_id.startswith(("ent:", "rule:"))
    }
    assert gold <= corpus_ids, sorted(gold - corpus_ids)


def test_every_procedural_gold_key_exists(seeded: tuple[str, dict[str, object]]) -> None:
    """`proc:<namespace>/<key>` 的两段都要能在偏好表里找到。"""
    _snapshot, payload = seeded
    keys = payload["procedural_keys"]
    assert isinstance(keys, set)
    gold = {
        tuple(doc_id.removeprefix("proc:").split("/", 1))
        for item in build_full()
        for doc_id in item["expected_doc_ids"]
        if doc_id.startswith("proc:")
    }
    assert gold <= keys, sorted(gold - keys)


def test_preference_has_two_versions(seeded: tuple[str, dict[str, object]]) -> None:
    """★ 时效探针（MEM-PRO-001/002）的前提：同一条偏好确实有两个版本。

    第 4 周那条来自「对话推断」（可信度 1），第 20 周蒸馏出的来自「排班确认记录」
    （可信度 2）——**严格更高才覆盖**。同档来源是不会自动改写的，它会升级人工
    （§6.4 ③），这一点在 W11 造数据时实测撞到过，`memory_seed.py` 顶部记了。
    """
    _snapshot, payload = seeded
    assert payload["tier_at_week4"] == 0
    assert payload["tier_at_week8"] == 0
    assert payload["tier_at_week20"] == 1
    assert payload["procedural_versions"] == 2
