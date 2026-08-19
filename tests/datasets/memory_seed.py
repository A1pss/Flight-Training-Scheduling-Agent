"""把 `memory_320` 的 20 周时间线**真的写进库**，并蒸馏出程序记忆。

## 一处必须照实说的实测发现（2026-08-19，W11）

原本的设计是「第 8 周蒸馏一次、第 20 周再蒸馏一次，于是同一条偏好有两个版本」。
**跑真库跑不出来**：两次蒸馏的来源都是 `排班确认记录`（同档），而
`put_preference` 对「同档且值不同」的处置是**升级人工**（§6.4 ③ / FTS-2001），
不写新版本。也就是说 —— **偏好不会被自动改写，这是刻意的设计**，
`tests/integration/test_memory_live.py` 早就把这条行为钉住了。

所以两个版本必须走**可信度升级**这条真实存在的路：

| 时点 | 来源 | 可信度 | 值 | 结果 |
|---|---|---|---|---|
| 第 4 周 | `对话推断` | 1 | Tier 0 | 写入（用户在对话里说过「先来保守档」） |
| 第 20 周 | `排班确认记录` | 2 | Tier 1 | **严格更高 → 覆盖**，旧行 `valid_to` = 第 20 周 |

于是「第 8 周的时候我偏好哪一档」与「我现在习惯哪一档」是**两个不同的正确答案**，
时效探针（MEM-PRO-001 / 002）才有东西可测。这条路径不是我造的 —— 它就是
`put_preference` 文档里那张表的第二行。

## 幂等

`record_episode` 是内容寻址的（同一件事写两次是同一行），`put_preference`
在值没变时不产生新版本。本脚本可重复跑。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.memory.episodic import record_episode
from backend.memory.procedural import NAMESPACE_RELAXATION, distill, put_preference
from backend.memory.temporal import SOURCE_CONVERSATION
from tests.datasets.memory_catalog import (
    EARLY_PREFERENCE_TIER,
    EARLY_PREFERENCE_WEEK,
    IFR_OUTAGE_VALID_TO,
    at_hour,
    timeline_records,
)


def seed_timeline(session: Session) -> None:
    """写入 122 条情景记忆 + 一条对话期偏好 + 第 20 周的蒸馏。"""
    for record in timeline_records():
        valid_to = IFR_OUTAGE_VALID_TO if record.content.get("capacity") == 0 else None
        record_episode(session, record, valid_to=valid_to)
    session.flush()

    # 第 4 周：用户在对话里说过的偏好（可信度最低的一档）
    put_preference(
        session,
        namespace=NAMESPACE_RELAXATION,
        key="preferred_tier",
        value={"tier": EARLY_PREFERENCE_TIER, "note": "用户对话中提到先用保守档"},
        source=SOURCE_CONVERSATION,
        at=at_hour(EARLY_PREFERENCE_WEEK, 18),
    )
    session.flush()

    # 第 20 周：从 20 条批准记录蒸馏。来源是「排班确认记录」，可信度严格更高，
    # 于是覆盖上面那条 —— 这是 §6.4 ③ 表格的第二行，不是绕过冲突检测。
    distill(session, at=at_hour(20, 18))
    session.flush()


__all__ = ["seed_timeline"]
