"""基准实体表 —— **只给评测数据集的标注校验用**（v6 §1.3）。

## 这张表的合法用途只有一个

v6 §1.3 的告警框：8 人 / 8 机 / 12 课目 / 6 空域 / 2 跑道是「基准数据集长什么样」，
**不是「系统只能处理这么大」**。所以它绝不能被求解、摄取、校验、API 任何一条
业务路径 import。

本模块存在的理由是另一件事：**评测数据集是照着基准实体表构造的**，标注里写
`P09` 或 `missionI-1` 就是笔误。加载时拿这张表一比就能挡下来 —— 这类笔误
在跑到「Recall@5 为什么是 0」的时候才发现，代价高得多。

`tests/datasets/test_entities.py` 会把这里的条数与
`backend.ingestion.validate.BASELINE_ENTITY_COUNTS` 对齐，防止两处漂移。
"""

from __future__ import annotations

from typing import Final

#: 人员：编号 → (姓名, 身份)。v6 §1.3.1 逐行。
PERSONS: Final[dict[str, tuple[str, str]]] = {
    "P01": ("孙军", "教员"),
    "P02": ("高超", "教员"),
    "P03": ("吴鹏", "教员"),
    "P04": ("刘斌", "成熟飞行员"),
    "P05": ("罗磊", "学员"),
    "P06": ("张勇", "学员"),
    "P07": ("陈伟", "学员"),
    "P08": ("何超", "学员"),
}

#: 姓名 → 编号。**「高超」是 P02 教员，不是「何超」的错别字** —— 这一对同音近形
#: 实体双双真实存在，正是 v6 §6.5.1 与 §15.2 ⑥ 反复点名的那个长尾。
NAME_TO_ID: Final[dict[str, str]] = {name: pid for pid, (name, _role) in PERSONS.items()}

#: 飞机：机号 → 机型。v6 §1.3.2。**AC73 是 JL-8**（§1.2 更正过一次）。
AIRCRAFT: Final[dict[str, str]] = {
    "AC10": "JL-8",
    "AC27": "JL-8",
    "AC34": "JL-8",
    "AC49": "JL-8",
    "AC61": "JL-8",
    "AC73": "JL-8",
    "AC84": "JL-9",
    "AC95": "JL-9",
}

#: 课目：编号 → (时长分钟, freq_days, 绑定空域)。v6 §1.3.3。
MISSIONS: Final[dict[str, tuple[int, int, str]]] = {
    "missionA-1": (30, 3, "SAA"),
    "missionA-2": (27, 3, "SAB"),
    "missionB-1": (52, 7, "RT2"),
    "missionB-2": (54, 7, "RT1"),
    "missionC-1": (35, 7, "IFR"),
    "missionC-2": (56, 7, "IFR"),
    "missionD-1": (26, 7, "RNG"),
    "missionE-1": (46, 7, "SAA"),
    "missionE-2": (69, 7, "SAA"),
    "missionF-1": (40, 7, "SAB"),
    "missionG-1": (35, 14, "SAB"),
    "missionH-1": (50, 14, "RT1"),
}

#: 空域：编号 → 同时段容量。v6 §1.3.4。
AIRSPACES: Final[dict[str, int]] = {
    "SAA": 2,
    "SAB": 2,
    "IFR": 1,
    "RT1": 1,
    "RT2": 1,
    "RNG": 1,
}

#: 跑道：编号 → 服务机型。v6 §1.3.5。
RUNWAYS: Final[dict[str, tuple[str, ...]]] = {
    "RWY-1": ("JL-8", "JL-9"),
    "RWY-2": ("JL-8",),
}

#: 基准周（`SPEC_DECISIONS §C.3`）：2026W02 = 2026-01-05 ~ 2026-01-11。
BASELINE_ISO_WEEK: Final[str] = "2026W02"
BASELINE_WEEK_START: Final[str] = "2026-01-05"

#: 标注里允许出现的全部实体编号。
ALL_ENTITY_IDS: Final[frozenset[str]] = frozenset(
    {*PERSONS, *AIRCRAFT, *MISSIONS, *AIRSPACES, *RUNWAYS}
)


def is_known_entity(entity_id: str) -> bool:
    """标注里引用的编号是不是基准实体表里的。`ALL` 是范围通配，不算实体。"""
    return entity_id == "ALL" or entity_id in ALL_ENTITY_IDS


__all__ = [
    "AIRCRAFT",
    "AIRSPACES",
    "ALL_ENTITY_IDS",
    "BASELINE_ISO_WEEK",
    "BASELINE_WEEK_START",
    "MISSIONS",
    "NAME_TO_ID",
    "PERSONS",
    "RUNWAYS",
    "is_known_entity",
]
