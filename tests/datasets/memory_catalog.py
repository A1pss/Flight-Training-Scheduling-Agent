"""`memory_320` 的时间线与探针构造（v6 §12.4）。

## 三类记忆的召回单位各是什么

| 类型 | 召回单位 | id 形态 | 来源 |
|---|---|---|---|
| 语义 | 实体摘要句 / 规则原文 | `ent:person:P04`、`rule:1.3.0:07` | `retrieval.corpus` |
| 情景 | 情景记忆摘要 | `epi:epi_<24hex>` | `memory.episodic`（内容寻址） |
| 程序 | 偏好条目 | `proc:<namespace>/<key>` | `memory.procedural.distill()` |

**前两类的 id 与运行时代码里的逐字一致**（`ent:`/`rule:`/`epi:` 三个前缀都来自
`backend.retrieval.corpus`）。`proc:` 是本数据集引入的约定 —— 当前
`preference_docs()` 只返回句子、不发 id，卡片的已知局限里写了这件事。

## 情景记忆的 id 为什么算得出来

`EpisodeRecord.memory_id()` 是**内容寻址**的（session_id + kind + summary + content +
occurred_at 的 sha256 前 24 位）。所以标注里的 gold id 不是我编的，而是由这份
时间线**算出来**的 —— 只要 `memory_seed.py` 把同一批记录写进库，id 必然对得上。
`tests/datasets/test_memory_timeline_live.py` 在真库上验证这一点。

## 程序记忆的偏好从哪来（业务方 2026-08-19 裁定：方案 P-A）

**不是手写的**：先合成 20 周的会话历史落成情景记忆，再跑**现有的**
`procedural.distill()` 蒸馏出偏好。于是偏好由确定性代码算出，顺带验了
`min_support`（一次不算习惯）与版本管理（第 8 周与第 20 周蒸馏出的松弛档不同，
形成两个版本，供时效探针用）。

## 20 周时间线

第 k 周的周一 = `2026-01-05 + 7×(k−1)`，第 1 周即基准周，第 20 周 = 2026-05-18。
每周 6 条事件 × 20 周 = 120 条，另有 2 条**成对的时效事件**（IFR 容量降为 0 →
第 9 周恢复），合计 122 条。

> §6.4 的归档线是「快照里最长的 `cycle_weeks` × 3」= 20 × 3 = **60 周**，
> 所以这 20 周内**不会有任何记忆被归档** —— 衰减测试测的是召回质量随语料增长的
> 退化，不是归档策略。这两件事混起来会得出「衰减是因为被归档了」的错误结论。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final

from backend.memory.episodic import EpisodeRecord

#: 时间线第 1 周的周一（= 基准周）
TIMELINE_START: Final[date] = date(2026, 1, 5)
TIMELINE_WEEKS: Final[int] = 20


def week_monday(week: int) -> date:
    return TIMELINE_START + timedelta(days=7 * (week - 1))


def at_hour(week: int, hour: int) -> datetime:
    monday = week_monday(week)
    return datetime(monday.year, monday.month, monday.day, hour, 0)


#: 24 条修订原话。**每条在 20 周里出现 2~3 次** —— `distill()` 的 `min_support`
#: 默认是 2，出现一次的不算习惯（这正是要验的行为）。
REVISION_PHRASES: Final[tuple[str, ...]] = (
    "往后挪挪",
    "别排周三",
    "教员换成孙军",
    "这个架次取消",
    "改到上午",
    "把 AC73 换掉",
    "何超少排一次",
    "间隔拉开一点",
    "IFR 那门课先停",
    "跑道换成 RWY-1",
    "把周五腾出来",
    "这一版别动了",
    "刘斌的复训优先",
    "学员的 A 类别漏",
    "晚点飞",
    "早一点起飞",
    "换一架飞机",
    "这天别安排了",
    "把两个架次合并看看",
    "优先补落下的课",
    "密度降一点",
    "周末不排",
    "带飞教员固定一个",
    "先把阻塞项解开",
)

#: 每周被推迟的课目与理由（循环取用，保证每周的摘要都不同）
DEFERRALS: Final[tuple[tuple[str, str], ...]] = (
    ("missionF-1", "SAB 空域当日占满"),
    ("missionC-1", "AC73 全天定检"),
    ("missionB-1", "RT2 空域容量为 1，与 missionB-2 冲突"),
    ("missionC-2", "先修 missionC-1 当周未完成"),
    ("missionA-2", "训练窗被压到 08:00-16:00"),
    ("missionB-2", "带飞教员当日已达日上限"),
    ("missionG-1", "刘斌复训占用了同一时段"),
    ("missionH-1", "RWY-1 单日关闭"),
)

REJECTIONS: Final[tuple[str, ...]] = (
    "带飞教员集中在孙军一人身上",
    "周三排了 5 个架次，太密",
    "何超的 A 类被排在最后一天",
    "刘斌的复训与学员架次抢同一个空域",
    "两个架次的周转时间只留了 30 分钟",
)

APPROVERS: Final[tuple[str, ...]] = ("张主任", "李科长", "王参谋")

#: 松弛档在第 8 周之后从 Tier 0 转向 Tier 1。
#: 这个转折是**刻意的** —— 它让第 8 周与第 20 周蒸馏出的偏好不同，
#: 从而产生两个版本，`temporal_validity` 探针才有东西可测（Z-18）。
TIER_SWITCH_WEEK: Final[int] = 8


#: 第 4 周用户在对话里说过的偏好档（可信度 `对话推断`，会被第 20 周的蒸馏覆盖）。
#: 见 `memory_seed.py` 顶部那张表 —— 两个版本走的是**可信度升级**，
#: 不是「蒸馏两次」（同档来源不会自动改写偏好，只会升级人工）。
EARLY_PREFERENCE_WEEK: Final[int] = 4
EARLY_PREFERENCE_TIER: Final[int] = 0


def tier_of(week: int) -> int:
    return 0 if week <= TIER_SWITCH_WEEK else 1


def phrases_of(week: int) -> tuple[str, ...]:
    """第 k 周用户说的三句修订原话。"""
    return tuple(REVISION_PHRASES[(week * 3 + j) % len(REVISION_PHRASES)] for j in range(3))


def timeline_records() -> list[EpisodeRecord]:
    """122 条情景记忆：20 周 × 6 条 + 2 条成对的时效事件。"""
    records: list[EpisodeRecord] = []
    for week in range(1, TIMELINE_WEEKS + 1):
        session_id = f"m9a-w{week:02d}"
        sorties = 12 + (week % 5)
        dual = sorties - 5
        blocked = max(0, 7 - week // 3)
        mission, reason = DEFERRALS[(week - 1) % len(DEFERRALS)]
        tier = tier_of(week)
        arrears = 0 if tier == 0 else 1 + week % 3

        records.append(
            EpisodeRecord(
                session_id=session_id,
                kind="schedule_session",
                summary=(
                    f"第 {week} 周排班会话：共 {sorties} 架次（{dual} 带飞 / 5 单飞），"
                    f"阻塞项 {blocked} 条"
                ),
                content={
                    "week": week,
                    "week_start": week_monday(week).isoformat(),
                    "sorties": sorties,
                    "dual": dual,
                    "solo": 5,
                    "blocked": blocked,
                },
                occurred_at=at_hour(week, 9),
            )
        )
        records.append(
            EpisodeRecord(
                session_id=session_id,
                kind="conflict_resolution",
                summary=f"第 {week} 周 {mission} 因{reason}推迟到下周",
                content={"week": week, "mission": mission, "reason": reason},
                occurred_at=at_hour(week, 10),
            )
        )
        records.append(
            EpisodeRecord(
                session_id=session_id,
                kind="user_revision",
                summary=f"第 {week} 周用户提出 3 条修订：" + "、".join(phrases_of(week)),
                content={"week": week, "revision_utterances": list(phrases_of(week))},
                occurred_at=at_hour(week, 11),
            )
        )
        records.append(
            EpisodeRecord(
                session_id=session_id,
                kind="user_rejection",
                summary=(
                    f"第 {week} 周用户驳回第 1 版：{REJECTIONS[(week - 1) % len(REJECTIONS)]}"
                ),
                content={"week": week, "reason": REJECTIONS[(week - 1) % len(REJECTIONS)]},
                occurred_at=at_hour(week, 14),
            )
        )
        records.append(
            EpisodeRecord(
                session_id=session_id,
                kind="relaxation_choice",
                summary=f"第 {week} 周选用 Tier {tier} 松弛，欠账 {arrears} 项",
                content={"week": week, "tier": tier, "arrears": arrears},
                occurred_at=at_hour(week, 15),
            )
        )
        records.append(
            EpisodeRecord(
                session_id=session_id,
                kind="approval",
                summary=(
                    f"第 {week} 周方案由{APPROVERS[(week - 1) % len(APPROVERS)]}批准，"
                    f"松弛档 Tier {tier}"
                ),
                content={
                    "week": week,
                    "relaxation_tier": tier,
                    "approver": APPROVERS[(week - 1) % len(APPROVERS)],
                },
                occurred_at=at_hour(week, 16),
            )
        )

    # 成对的时效事件：第 6 周起 IFR 容量降为 0，第 9 周恢复。
    # 前者写入时就带 valid_to（见 memory_seed），于是「第 7 周时 IFR 能用吗」
    # 与「现在 IFR 能用吗」应当召回**不同**的条目 —— 时效正确率测的就是这个。
    records.append(
        EpisodeRecord(
            session_id="m9a-w06",
            kind="conflict_resolution",
            summary="IFR Route 因导航台检修，第 6 周起同时段容量降为 0",
            content={"airspace": "IFR", "capacity": 0, "from_week": 6},
            occurred_at=at_hour(6, 17),
        )
    )
    records.append(
        EpisodeRecord(
            session_id="m9a-w09",
            kind="conflict_resolution",
            summary="IFR Route 导航台检修完成，第 9 周起同时段容量恢复为 1",
            content={"airspace": "IFR", "capacity": 1, "from_week": 9},
            occurred_at=at_hour(9, 17),
        )
    )
    return records


#: 第 6 周那条时效事件的失效时点（= 第 9 周周一 17:00）
IFR_OUTAGE_VALID_TO: Final[datetime] = at_hour(9, 17)


def epi_doc_id(record: EpisodeRecord) -> str:
    """情景记忆在语料里的 doc id（`retrieval.corpus.episodic_docs` 的口径）。"""
    return f"epi:{record.memory_id()}"


@dataclass(frozen=True)
class Timeline:
    """按 (周, 类型) 索引的时间线，写探针时按语义取，不按下标数数。"""

    records: tuple[EpisodeRecord, ...]

    def doc_id(self, week: int, kind: str) -> str:
        for record in self.records:
            if record.session_id == f"m9a-w{week:02d}" and record.kind == kind:
                return epi_doc_id(record)
        raise KeyError(f"时间线里没有第 {week} 周的 {kind} 事件")

    def ifr_outage(self) -> str:
        return epi_doc_id(self.records[-2])

    def ifr_restored(self) -> str:
        return epi_doc_id(self.records[-1])


def timeline() -> Timeline:
    return Timeline(records=tuple(timeline_records()))


def phrasing_key(text: str) -> str:
    """与 `procedural.distill()` 里的 key 计算逐字一致。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def phrasing_doc_id(text: str) -> str:
    return f"proc:phrasing/{phrasing_key(text)}"


RELAXATION_DOC_ID: Final[str] = "proc:relaxation/preferred_tier"


def as_of(week: int) -> str:
    return week_monday(week).isoformat()


__all__ = [
    "EARLY_PREFERENCE_TIER",
    "EARLY_PREFERENCE_WEEK",
    "IFR_OUTAGE_VALID_TO",
    "RELAXATION_DOC_ID",
    "REVISION_PHRASES",
    "TIMELINE_START",
    "TIMELINE_WEEKS",
    "Timeline",
    "as_of",
    "at_hour",
    "epi_doc_id",
    "phrases_of",
    "phrasing_doc_id",
    "phrasing_key",
    "tier_of",
    "timeline",
    "timeline_records",
    "week_monday",
]
