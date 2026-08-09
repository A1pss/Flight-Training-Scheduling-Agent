"""先修链展开（§6.1 / S-01）与嵌入函数单测。"""

from __future__ import annotations

import pytest

from backend.core.config import PROJECT_ROOT
from backend.ingestion.adapters import extract_pdf
from backend.ingestion.parsers import parse_missions_document
from backend.memory.collections import ALL_COLLECTIONS
from backend.memory.embeddings import HASH_DIM, HashEmbedder, build_embedder
from backend.retrieval.prereq_cte import (
    MAX_PREREQ_DEPTH,
    evaluate_prereq,
    expand_class_ref,
    expand_prereq_refs,
    transitive_prereqs,
)

ORIGIN = PROJECT_ROOT / "data" / "origin"
ALL_MISSIONS = [
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
]


# ── S-01：类引用展开为「该类全部课目」 ───────────────────────────────
def test_expand_class_ref_returns_all_missions_of_the_class() -> None:
    assert expand_class_ref("A", ALL_MISSIONS) == ("missionA-1", "missionA-2")
    assert expand_class_ref("C", ALL_MISSIONS) == ("missionC-1", "missionC-2")
    assert expand_class_ref("G", ALL_MISSIONS) == ("missionG-1",)
    assert expand_class_ref("Z", ALL_MISSIONS) == ()


def test_expand_prereq_refs_mixes_class_and_mission_refs() -> None:
    refs = [("A类", "class"), ("missionC-1", "mission")]
    assert expand_prereq_refs(refs, ALL_MISSIONS) == (
        "missionA-1",
        "missionA-2",
        "missionC-1",
    )


# ── S-01 的判定：类达标 = 该类全部课目完成 ───────────────────────────
def test_he_chao_is_blocked_because_a_class_needs_both_missions() -> None:
    """何超只完成 A-1 → `A类` 先修不达标（缺 A-2）→ B-1 BLOCKED。

    这正是 v6 §1.4.2 那 7 条阻塞项的来源。
    """
    met, missing = evaluate_prereq([("A类", "class")], {"missionA-1"}, ALL_MISSIONS)
    assert met is False
    assert missing == ("missionA-2",)


def test_a_class_met_when_both_completed() -> None:
    met, missing = evaluate_prereq([("A类", "class")], {"missionA-1", "missionA-2"}, ALL_MISSIONS)
    assert met is True
    assert missing == ()


def test_no_prereq_is_always_met() -> None:
    assert evaluate_prereq([], set(), ALL_MISSIONS) == (True, ())


def test_c2_requires_c1_specifically() -> None:
    met, missing = evaluate_prereq(
        [("missionC-1", "mission")], {"missionA-1", "missionA-2"}, ALL_MISSIONS
    )
    assert met is False
    assert missing == ("missionC-1",)


# ── 传递闭包（含类展开、防环、限深） ─────────────────────────────────
@pytest.fixture(scope="module")
def prereq_map() -> dict[str, list[tuple[str, str]]]:
    missions = parse_missions_document(extract_pdf(ORIGIN / "missions.pdf"))
    return {m.mission_id: [(p.prereq_ref, p.ref_kind) for p in m.prereqs] for m in missions}


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("missionA-1", ()),
        ("missionC-2", ("missionA-1", "missionA-2", "missionC-1")),
        ("missionG-1", ("missionA-1", "missionA-2", "missionF-1")),
        (
            "missionD-1",
            ("missionA-1", "missionA-2", "missionB-1", "missionB-2", "missionC-1", "missionC-2"),
        ),
        (
            "missionE-2",
            ("missionA-1", "missionA-2", "missionC-1", "missionC-2", "missionE-1"),
        ),
    ],
)
def test_transitive_prereqs_on_real_missions(
    prereq_map: dict[str, list[tuple[str, str]]], target: str, expected: tuple[str, ...]
) -> None:
    assert transitive_prereqs(target, prereq_map, ALL_MISSIONS) == expected


def test_transitive_prereqs_survives_a_cycle() -> None:
    """构造 X→Y→X 的环，必须终止而不是转到栈溢出。"""
    cyclic = {
        "missionA-1": [("missionA-2", "mission")],
        "missionA-2": [("missionA-1", "mission")],
    }
    result = transitive_prereqs("missionA-1", cyclic, ALL_MISSIONS)
    assert set(result) <= {"missionA-1", "missionA-2"}


def test_max_depth_is_eight() -> None:
    assert MAX_PREREQ_DEPTH == 8


def test_depth_limit_truncates_a_long_chain() -> None:
    ids = [f"missionA-{i}" for i in range(10)]
    chain = {f"missionA-{i}": [(f"missionA-{i + 1}", "mission")] for i in range(9)}
    result = transitive_prereqs("missionA-0", chain, ids, max_depth=2)
    assert len(result) == 2


# ── 嵌入函数 ─────────────────────────────────────────────────────────
def test_hash_embedder_is_deterministic_and_normalized() -> None:
    emb = HashEmbedder()
    a = emb.embed(["何超（P08），学员"])[0]
    b = emb.embed(["何超（P08），学员"])[0]
    assert a == b
    assert len(a) == HASH_DIM
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9


def test_hash_embedder_separates_different_texts() -> None:
    emb = HashEmbedder()
    a, b = emb.embed(["何超（P08），学员，机型资质 JL-8", "完全不同的另一段文本内容"])
    cosine = sum(x * y for x, y in zip(a, b, strict=True))
    assert cosine < 0.9


def test_hash_embedder_handles_empty_string() -> None:
    assert len(HashEmbedder().embed([""])[0]) == HASH_DIM


def test_build_embedder_honours_provider_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.core.config import get_settings

    monkeypatch.setenv("EMBED_PROVIDER", "hash")
    get_settings.cache_clear()
    assert build_embedder().name == "hash-3gram"
    get_settings.cache_clear()


def test_collection_names_are_the_three_v6_names_plus_situations() -> None:
    assert ALL_COLLECTIONS == (
        "rule_texts",
        "entity_summaries",
        "historical_reports",
        "situation_docs",
    )
