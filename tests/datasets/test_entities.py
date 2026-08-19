"""基准实体表与摄取侧的规模常量必须对得上。"""

from __future__ import annotations

from backend.datasets import entities
from backend.ingestion.validate import BASELINE_ENTITY_COUNTS


def test_counts_match_ingestion_baseline() -> None:
    """两处各存一份基准规模，漂了就在这里断。

    `backend.ingestion.validate` 存的是**条数**（基准回归护栏），本包存的是
    **编号与属性**（标注校验）。两者独立演进的话，会出现「摄取说 8 架、
    标注表里只有 7 架」这种只在跑指标时才暴露的偏差。
    """
    assert len(entities.PERSONS) == BASELINE_ENTITY_COUNTS["persons"]
    assert len(entities.AIRCRAFT) == BASELINE_ENTITY_COUNTS["aircraft"]
    assert len(entities.MISSIONS) == BASELINE_ENTITY_COUNTS["missions"]
    assert len(entities.AIRSPACES) == BASELINE_ENTITY_COUNTS["airspaces"]
    assert len(entities.RUNWAYS) == BASELINE_ENTITY_COUNTS["runways"]


def test_ac73_is_jl8() -> None:
    """v6 §1.2 更正过一次的那一条，钉死在测试里（易错事实 M2）。"""
    assert entities.AIRCRAFT["AC73"] == "JL-8"
    assert sum(1 for kind in entities.AIRCRAFT.values() if kind == "JL-8") == 6
    assert sum(1 for kind in entities.AIRCRAFT.values() if kind == "JL-9") == 2


def test_near_confusable_names_both_exist() -> None:
    """「何超」与「高超」**双双是真实实体** —— 这是 nl_360 对抗层的全部前提。"""
    assert entities.NAME_TO_ID["何超"] == "P08"
    assert entities.NAME_TO_ID["高超"] == "P02"
    assert entities.PERSONS["P08"][1] == "学员"
    assert entities.PERSONS["P02"][1] == "教员"


def test_is_known_entity() -> None:
    assert entities.is_known_entity("ALL")
    assert entities.is_known_entity("missionH-1")
    assert entities.is_known_entity("RWY-2")
    assert not entities.is_known_entity("P09")
    assert not entities.is_known_entity("missionI-1")
