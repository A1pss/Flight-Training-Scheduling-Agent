"""先修链递归 CTE（v6 §6.1）+ S-01 类引用展开。

**先修链用递归 CTE 而非图数据库**：当前最深 2 跳
（`missionE-2 ← missionE-1 ← C类 ← {C-1, C-2}` 展开后 2 跳；
`missionG-1 ← {A类, F类}` 为 1 跳），CTE 的性能与表达力完全够用。
重新评估引入图数据库的触发条件：**人员规模 >100 或先修链深度 >4 跳**。

## `prereq_ref` 的两种形态

`mission_prereq.prereq_ref` 可以是**课目编号**（`missionC-1`）或**类别**
（`A类`）。类别引用按 S-01 展开为「该类全部课目」，**展开不在 SQL 里做**
（v6 §6.1 / §6.3）—— SQL 只按课目编号连边，遇到类别引用自然停在那一跳，
展开交给 :func:`expand_prereq_refs`。

## 为什么展开函数放在这里

S-01 的展开逻辑 `compile_spec_node`（M2）要用，摄取侧物化 `training_progress`
的 `prereq_met` 也要用。**同一个语义有两份实现就一定会漂**，所以只写一份放在
这个模块里，两边都 import 它。这与铁律 2 的 solver/validator 隔离不冲突：
那条禁令针对的是「约束表达代码」，先修链展开是数据层的图遍历，且它不属于
`validator/` 与 `solver/` 任何一侧。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.orm import Session

#: 防环上限（v6 §6.1）
MAX_PREREQ_DEPTH: Final[int] = 8

#: v6 §6.1 的递归 CTE。`depth < 8` 限深、`NOT p.mission_id = ANY(c.path)` 防环。
#:
#: 相对 v6 原文的两处**必要**改动：
#: ① 加 `snapshot_id` 过滤 —— 事实表按快照作用域建模，不带过滤会跨快照连边；
#: ② `path` 显式 `::text` —— `mission_id` 是 `VARCHAR(16)`，`ARRAY[mission_id]`
#:    在非递归项里是 `varchar(16)[]`、在递归项里拼接后退化为 `varchar[]`，
#:    PG 会直接报 `recursive query ... has type ... in non-recursive term but
#:    type ... overall`。这不是风格问题，不加就跑不起来。
PREREQ_CHAIN_SQL: Final[str] = """
WITH RECURSIVE prereq_chain AS (
    SELECT mission_id, prereq_ref, ref_kind, 1 AS depth,
           ARRAY[mission_id::text] AS path
    FROM mission_prereq
    WHERE mission_id = :target AND snapshot_id = :snapshot_id
  UNION ALL
    SELECT p.mission_id, p.prereq_ref, p.ref_kind, c.depth + 1,
           c.path || p.mission_id::text
    FROM mission_prereq p
    JOIN prereq_chain c ON p.mission_id = c.prereq_ref
    WHERE p.snapshot_id = :snapshot_id
      AND c.depth < :max_depth
      AND NOT p.mission_id::text = ANY(c.path)
)
SELECT mission_id, prereq_ref, ref_kind, depth, path FROM prereq_chain
ORDER BY depth, mission_id, prereq_ref
"""


@dataclass(frozen=True)
class PrereqEdge:
    """先修链上的一条边。"""

    mission_id: str
    prereq_ref: str
    ref_kind: str
    depth: int
    path: tuple[str, ...]


def fetch_prereq_chain(
    session: Session, mission_id: str, snapshot_id: str, *, max_depth: int = MAX_PREREQ_DEPTH
) -> list[PrereqEdge]:
    """查某门课目的完整先修链（含传递闭包，防环，限深）。"""
    rows = session.execute(
        text(PREREQ_CHAIN_SQL),
        {"target": mission_id, "snapshot_id": snapshot_id, "max_depth": max_depth},
    ).all()
    return [
        PrereqEdge(
            mission_id=row.mission_id,
            prereq_ref=row.prereq_ref,
            ref_kind=row.ref_kind,
            depth=row.depth,
            path=tuple(row.path),
        )
        for row in rows
    ]


def expand_class_ref(class_letter: str, mission_ids: Iterable[str]) -> tuple[str, ...]:
    """S-01：类引用 → 该类**全部**课目。

    `A类` → `(missionA-1, missionA-2)`。裁定是「该类全部课目完成」，所以
    展开成全集、判定时取合，而不是任一门。
    """
    prefix = f"mission{class_letter}-"
    return tuple(sorted(m for m in mission_ids if m.startswith(prefix)))


def expand_prereq_refs(
    prereqs: Sequence[tuple[str, str]], mission_ids: Iterable[str]
) -> tuple[str, ...]:
    """把 (prereq_ref, ref_kind) 列表展开成具体课目编号集合（S-01）。"""
    ids = list(mission_ids)
    expanded: list[str] = []
    for ref, kind in prereqs:
        if kind == "class":
            expanded.extend(expand_class_ref(ref[0], ids))
        else:
            expanded.append(ref)
    return tuple(sorted(set(expanded)))


def evaluate_prereq(
    prereqs: Sequence[tuple[str, str]],
    completed: Iterable[str],
    mission_ids: Iterable[str],
) -> tuple[bool, tuple[str, ...]]:
    """判定先修是否达标，返回 (是否达标, 缺失的课目编号)。

    S-01：类引用要求该类**全部**课目完成 —— 何超只完成 A-1，`A类` 先修
    因缺 `missionA-2` 而不达标，于是 B-1/B-2/C-1/F-1 全部 BLOCKED
    （与 v6 §1.4.2 的 7 条阻塞项逐条吻合）。
    """
    required = expand_prereq_refs(prereqs, mission_ids)
    done = set(completed)
    missing = tuple(m for m in required if m not in done)
    return (not missing), missing


def transitive_prereqs(
    mission_id: str,
    prereq_map: Mapping[str, Sequence[tuple[str, str]]],
    mission_ids: Iterable[str],
    *,
    max_depth: int = MAX_PREREQ_DEPTH,
) -> tuple[str, ...]:
    """纯 Python 侧的先修传递闭包（含类展开），供不连库的场景使用。

    与 :func:`fetch_prereq_chain` 的区别：那个是 SQL 侧、不展开类引用；
    这个展开类引用，给出的是「要飞这门课，一路上必须已完成哪些课目」。
    同样防环、同样限深。
    """
    ids = list(mission_ids)
    seen: set[str] = set()
    frontier = list(expand_prereq_refs(prereq_map.get(mission_id, ()), ids))
    depth = 0
    while frontier and depth < max_depth:
        nxt: list[str] = []
        for item in frontier:
            if item in seen or item == mission_id:
                continue
            seen.add(item)
            nxt.extend(expand_prereq_refs(prereq_map.get(item, ()), ids))
        frontier = nxt
        depth += 1
    return tuple(sorted(seen))


__all__ = [
    "MAX_PREREQ_DEPTH",
    "PREREQ_CHAIN_SQL",
    "PrereqEdge",
    "evaluate_prereq",
    "expand_class_ref",
    "expand_prereq_refs",
    "fetch_prereq_chain",
    "transitive_prereqs",
]
