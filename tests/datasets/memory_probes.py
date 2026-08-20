"""`memory_320` 的探针（v6 §12.4）。

本文件当前交付的是**32 条送审样例**（语义 12 / 情景 12 / 程序 8，按 120:120:80
的比例分层）。全量 320 条待业务方确认口径后在同一套构造下铺开。

## 四条易错事实是硬性覆盖项

v6 §12.4 点名要求语义类必须覆盖 M1~M4，一条不许少。它们在样例里就是
`MEM-SEM-001` ~ `MEM-SEM-004`，`tests/datasets/test_memory_320.py` 断言它们
逐条在位、且 `expected_answer` 与 v6 表格逐字一致。

## `absent` 探针为什么必须有

三类各留了负例（问一个库里没有的实体 / 没有的周次 / 没有的偏好）。它们的
gold 集为空，**不进 Recall@5 的分母**，单独统计误召回率。全是正例的探针集
测不出「系统会不会硬答一个不存在的东西」，而那正是 RAG 系统最常见的幻觉形态。
"""

from __future__ import annotations

from typing import Any

from tests.datasets.memory_catalog import (
    RELAXATION_DOC_ID,
    as_of,
    phrasing_doc_id,
    timeline,
)

W20 = as_of(20)
TL = timeline()


def _probe(
    item_id: str,
    memory_type: str,
    probe_kind: str,
    query: str,
    docs: list[str],
    at: str,
    rationale: str,
    *,
    answer: str | None = None,
    written: str | None = None,
    week: int | None = None,
) -> dict[str, Any]:
    return {
        "item_id": item_id,
        "memory_type": memory_type,
        "probe_kind": probe_kind,
        "query": query,
        "expected_doc_ids": docs,
        "as_of": at,
        "written_at": written,
        "timeline_week": week,
        "expected_answer": answer,
        "rationale": rationale,
    }


def semantic_sample() -> list[dict[str, Any]]:
    """12 条语义探针，含 v6 §12.4 点名的四条易错事实。"""
    return [
        _probe(
            "MEM-SEM-001",
            "semantic",
            "fact",
            "刘斌的仪表等级什么时候到期？",
            ["ent:person:P04"],
            W20,
            "★ 易错事实 M1。正确答案取**总表**口径 2026-01-07，明细表的 02-07 是笔误"
            "（SPEC_DECISIONS §C.1 / v6 §1.2.1）。答错的后果：复训窗口整体偏移一个月，"
            "S-11 的强制复训会排到错误的一周。",
            answer="2026-01-07",
        ),
        _probe(
            "MEM-SEM-002",
            "semantic",
            "fact",
            "AC73 是什么机型？",
            ["ent:aircraft:AC73"],
            W20,
            "★ 易错事实 M2。v5.2 全文把 AC73 标为 JL-9，v6 按 aircraft.pdf 更正为 JL-8。"
            "答错的后果：周转时间（30 vs 40 分钟）与可用跑道（RWY-1/2 vs 仅 RWY-1）全错。",
            answer="JL-8",
        ),
        _probe(
            "MEM-SEM-003",
            "semantic",
            "prereq",
            "何超能不能排 missionB-1？",
            ["ent:person:P08", "ent:mission:missionB-1"],
            W20,
            "★ 易错事实 M3。不能 —— B 类的先修是「A 类整体达标」（S-01），何超只完成了 "
            "A-1，缺 A-2。这条的答案**只有递归 CTE 算得出来**，任何一句摘要文本里都没有；"
            "`Z-22` 实测到的正是这件事：关掉 SQL 精确路之后召回其实是对的，但答不出结论。",
            answer="不能，缺 missionA-2",
        ),
        _probe(
            "MEM-SEM-004",
            "semantic",
            "fact",
            "学员飞 missionA-1 需要教员吗？",
            ["ent:mission:missionA-1"],
            W20,
            "★ 易错事实 M4。不需要 —— D-1 裁定 A-1/A-2 的带飞列为「否」，且学员的 A 类"
            "等级为单飞。答错的后果：教员容量估算偏高 4 倍，§12.3 的 I1 构造会跟着错。",
            answer="不需要",
        ),
        _probe(
            "MEM-SEM-005",
            "semantic",
            "fact",
            "missionE-2 要飞多长时间？",
            ["ent:mission:missionE-2"],
            W20,
            "课目时长（69 分钟，全部 12 门里最长的一门）。它同时是 §12.3 的 I4 构造"
            "「训练窗压到 30 分钟」能封死所有课目的依据。",
            answer="69 分钟",
        ),
        _probe(
            "MEM-SEM-006",
            "semantic",
            "fact",
            "IFR Route 的同时段容量是多少？",
            ["ent:airspace:IFR"],
            W20,
            "空域容量（1）。S-10 把它并入约束6 做成**硬约束**，所以这个数直接决定 "
            "missionC-1/C-2 能不能同时排。",
            answer="1",
        ),
        _probe(
            "MEM-SEM-007",
            "semantic",
            "fact",
            "何超已经完成哪些课目？",
            ["ent:person:P08"],
            W20,
            "进度事实。★ 读的是 `person_completed_missions` 事实表，不是 "
            "`training_progress.status`（Z-16：只翻 status 会「显示已完成、先修却解锁不了」）。",
            answer="missionA-1",
        ),
        _probe(
            "MEM-SEM-008",
            "semantic",
            "prereq",
            "罗磊现在能排 missionC-2 吗？",
            ["ent:person:P05", "ent:mission:missionC-2"],
            W20,
            "先修判定的**正例**，与 MEM-SEM-003 的负例配对。C-2 的先修是逐门的 "
            "missionC-1，罗磊已完成 → 能。只有负例没有正例的话，一个「一律答不能」的"
            "系统也能拿满分。",
            answer="能",
        ),
        _probe(
            "MEM-SEM-009",
            "semantic",
            "rule_text",
            "约束7 的周转时间是从哪一刻算起的？",
            ["rule:1.3.0:07"],
            W20,
            "规则原文召回。S-06 裁定：上一架次**着陆** → 下一架次**起飞**。"
            "规则条文在语料里**禁止拆分**（§5.3），所以 gold 是整条。",
            answer="上一架次着陆到下一架次起飞",
        ),
        _probe(
            "MEM-SEM-010",
            "semantic",
            "rule_text",
            "约束9 的 20 分钟窗口是按跑道算还是全场算？",
            ["rule:1.3.0:09"],
            W20,
            "★ D-2 的口径：**20 分钟窗口按跑道分组，7 分钟间隔全场统一**。一条规则的"
            "两个半句口径不同，是本项目最容易被抹平的一处细节。",
            answer="20 分钟窗口按跑道分组；7 分钟间隔全场统一",
        ),
        _probe(
            "MEM-SEM-011",
            "semantic",
            "aggregate",
            "现在一共有几架 JL-8？",
            [
                "ent:aircraft:AC10",
                "ent:aircraft:AC27",
                "ent:aircraft:AC34",
                "ent:aircraft:AC49",
                "ent:aircraft:AC61",
                "ent:aircraft:AC73",
            ],
            W20,
            "汇总类：gold 是**六条实体摘要的集合**，Recall@5 在这类题上天然拿不满"
            "（Top-5 装不下 6 条）。★ 这是一条要在报数时单独说明的题型 —— 汇总类的"
            "正确判据是「答案对不对」，不是「六条是不是都进了 Top-5」。",
            answer="6 架（AC10/27/34/49/61/73，含 AC73）",
        ),
        _probe(
            "MEM-SEM-012",
            "semantic",
            "absent",
            "AC99 是什么机型？",
            [],
            W20,
            "★ 负例：AC99 不在实体表里。正确行为是回答「没有这架飞机」，"
            "**不是**从 AC95 或 AC49 里挑一个近似的答上去。编号只固定前缀不限位数"
            "（Z-4），所以 AC99 在**形态上完全合法** —— 这正是它比 'XYZ' 更难的地方。",
            answer="系统里没有 AC99",
        ),
    ]


def episodic_sample() -> list[dict[str, Any]]:
    """12 条情景探针：5 回忆 + 5 衰减 + 1 时效 + 1 负例。"""
    return [
        _probe(
            "MEM-EPI-001",
            "episodic",
            "episode_recall",
            "第 3 周为什么把 missionB-1 推迟了？",
            [TL.doc_id(3, "conflict_resolution")],
            as_of(20),
            "情景回忆：归因型问题，答案在当周的 `conflict_resolution` 摘要里"
            "（RT2 容量为 1，与 missionB-2 冲突）。v6 §12.4 情景类原文举的例子就是这一族。",
            written=as_of(3),
            week=3,
        ),
        _probe(
            "MEM-EPI-002",
            "episodic",
            "episode_recall",
            "上次 AC73 定检是哪一周？",
            [TL.doc_id(18, "conflict_resolution")],
            as_of(20),
            "★ v6 §12.4 情景类原文举的例子（「上次 AC73 维护是哪天」）。"
            "注意它**不是**语义事实 —— 维护计划表里的是「计划」，这里问的是"
            "「上一次实际因它推迟了什么」，只有会话历史里有。\n"
            "★ 关键在「上次」：AC73 定检在第 2、10、18 周各出现一次，第 20 周提问时"
            "正确答案是**最近那一次（第 18 周）**。召回到第 2 周那条算错 —— "
            "这条同时在考时间排序，而不只是关键词命中。",
            written=as_of(18),
            week=18,
        ),
        _probe(
            "MEM-EPI-003",
            "episodic",
            "episode_recall",
            "第 12 周批准的方案用了哪一档松弛？",
            [TL.doc_id(12, "approval")],
            as_of(20),
            "批准记录召回。第 12 周在 Tier 切换点（第 8 周）之后，应答 Tier 1；"
            "它同时是程序记忆里 `preferred_tier` 那条偏好的支撑证据之一。",
            written=as_of(12),
            week=12,
        ),
        _probe(
            "MEM-EPI-004",
            "episodic",
            "episode_recall",
            "第 7 周用户驳回第一版的理由是什么？",
            [TL.doc_id(7, "user_rejection")],
            as_of(20),
            "驳回记录召回。★ 驳回理由是 §15.2 ⑥ 难负例挖掘的直接输入 —— 用户不接受的"
            "方案形态，比用户接受的更有信息量。",
            written=as_of(7),
            week=7,
        ),
        _probe(
            "MEM-EPI-005",
            "episodic",
            "episode_recall",
            "第 5 周一共排了多少架次？",
            [TL.doc_id(5, "schedule_session")],
            as_of(20),
            "会话摘要召回（数值型）。这一族最容易出现「召回对了但把 12 说成 14」的"
            "生成层错误，是 §12.4.1 Faithfulness 判定的典型对象。",
            written=as_of(5),
            week=5,
        ),
        _probe(
            "MEM-EPI-006",
            "episodic",
            "decay",
            "第 1 周排班时有多少条阻塞项？",
            [TL.doc_id(1, "schedule_session")],
            as_of(20),
            "★ 衰减测试的最远点：第 1 周写入、第 20 周提问，中间隔着 122 条记忆。"
            "目标是情景记忆 20 周后召回率 ≥85%。★ 注意归档线是 60 周（20 周 cycle × 3，"
            "Z-18），**这条不是因为被归档才可能召不回**，是语料变大之后的排序退化。",
            written=as_of(1),
            week=1,
        ),
        _probe(
            "MEM-EPI-007",
            "episodic",
            "decay",
            "第 4 周是哪位领导批准的方案？",
            [TL.doc_id(4, "approval")],
            as_of(20),
            "衰减测试第 4 周点。问的是人名（张主任/李科长/王参谋三选一），"
            "★ 三个名字在语料里高频重复，是典型的「召回到了但选错了那一条」场景。",
            written=as_of(4),
            week=4,
        ),
        _probe(
            "MEM-EPI-008",
            "episodic",
            "decay",
            "第 8 周用户提了哪几条修订？",
            [TL.doc_id(8, "user_revision")],
            as_of(20),
            "衰减测试第 8 周点。修订原话在 20 周里重复出现 2~3 次，"
            "★ 所以这条同时考「能不能定位到**第 8 周那一次**」而不是随便召回一条含"
            "同样原话的记录 —— 时效与情景的交叉点。",
            written=as_of(8),
            week=8,
        ),
        _probe(
            "MEM-EPI-009",
            "episodic",
            "decay",
            "第 12 周有哪门课被推迟了？",
            [TL.doc_id(12, "conflict_resolution")],
            as_of(20),
            "衰减测试第 12 周点。",
            written=as_of(12),
            week=12,
        ),
        _probe(
            "MEM-EPI-010",
            "episodic",
            "decay",
            "第 16 周选的松弛档欠了多少项账？",
            [TL.doc_id(16, "relaxation_choice")],
            as_of(20),
            "衰减测试第 16 周点。欠账数是 Tier 1 松弛的代价，★ 它必须被显式披露"
            "（Z-9：欠账显式披露是 Tier 1 的前提），所以这条也验了披露内容有没有进记忆。",
            written=as_of(16),
            week=16,
        ),
        _probe(
            "MEM-EPI-011",
            "episodic",
            "temporal_validity",
            "第 7 周的时候 IFR Route 还能用吗？",
            [TL.ifr_outage()],
            as_of(7),
            "★ 时效正确率的核心用例。第 6 周写入「容量降为 0」（带 valid_to = 第 9 周），"
            "第 9 周写入「恢复为 1」。**在第 7 周这个时点提问，正确答案是那条已经失效"
            "的旧记录** —— 这正是 `Z-18` 说的「`superseded_by` 是链接不是墓碑，有效性只由 "
            "[valid_from, valid_to) 决定」。把它当作废标记会让这条查询返回空。",
            written=as_of(6),
            week=6,
            answer="不能用，容量降为 0",
        ),
        _probe(
            "MEM-EPI-012",
            "episodic",
            "absent",
            "第 25 周排了多少架次？",
            [],
            as_of(20),
            "★ 负例：时间线只有 20 周。正确行为是回答「没有第 25 周的记录」，"
            "而不是把第 20 周的数字拿来充数。这类「超出范围的时间点」是情景记忆里"
            "最容易被硬答的一类。",
        ),
    ]


def procedural_sample() -> list[dict[str, Any]]:
    """8 条程序探针：偏好召回 + 版本时效 + 负例。"""
    return [
        _probe(
            "MEM-PRO-001",
            "procedural",
            "preference",
            "我习惯的松弛顺序是什么？",
            [RELAXATION_DOC_ID],
            as_of(20),
            "★ v6 §12.4 程序类原文举的例子。偏好由 `distill()` 从 20 条批准记录蒸馏而来"
            "（不是手写的）：第 8 周之后的批准都是 Tier 1，第 20 周蒸馏得到 "
            "Tier 1（支持度 12），来源「排班确认记录」。",
            answer="Tier 1",
            written=as_of(20),
        ),
        _probe(
            "MEM-PRO-002",
            "procedural",
            "temporal_validity",
            "第 8 周的时候我偏好哪一档松弛？",
            [RELAXATION_DOC_ID],
            as_of(8),
            "★ 同一个 key 的**两个版本**，靠 `as_of` 区分（版本维度不进 id）：第 4 周"
            "用户在对话里说过 Tier 0（来源「对话推断」，可信度 1），第 20 周的蒸馏结果"
            "（来源「排班确认记录」，可信度 2）**严格更高才把它覆盖**。所以在第 8 周这个"
            "时点，有效版本仍是 Tier 0。\n"
            "⚠️ 这条的构造方式是 W11 实测改过的：原本打算「蒸馏两次得到两个版本」，"
            "真库上跑不出来 —— 同档来源的偏好不会被自动改写，只会升级人工（§6.4 ③ / "
            "FTS-2001）。**偏好不能被自动覆盖是刻意设计**，不是 bug。",
            answer="Tier 0",
            written=as_of(8),
        ),
        _probe(
            "MEM-PRO-003",
            "procedural",
            "preference",
            "我常说的「往后挪挪」是什么意思？",
            [phrasing_doc_id("往后挪挪")],
            as_of(20),
            "表述偏好召回。`distill()` 把重复出现 ≥2 次的修订原话蒸馏成 "
            "`phrasing/<sha256前16位>`；这条原话在 20 周里出现 3 次。",
            written=as_of(20),
        ),
        _probe(
            "MEM-PRO-004",
            "procedural",
            "preference",
            "我一般怎么表达把架次推后？",
            [phrasing_doc_id("往后挪挪"), phrasing_doc_id("晚点飞")],
            as_of(20),
            "★ 与上一条**同一批偏好、换一种问法**：上一条给出原话查含义，这一条给出"
            "含义反查原话。两条都对才说明程序记忆是按语义召回的，而不是靠字符串命中。",
            written=as_of(20),
        ),
        _probe(
            "MEM-PRO-005",
            "procedural",
            "preference",
            "「别排周三」这个说法我用过几次？",
            [phrasing_doc_id("别排周三")],
            as_of(20),
            "偏好的 `support` 字段召回（2 或 3 次）。★ 支持度是 `min_support` 的直接体现"
            "——「一次不算习惯」这条规则能不能被答出来，验的是蒸馏结果有没有把证据带上。",
            written=as_of(20),
        ),
        _probe(
            "MEM-PRO-006",
            "procedural",
            "preference",
            "我常用的修订说法有哪些？",
            [
                phrasing_doc_id("晚点飞"),
                phrasing_doc_id("往后挪挪"),
                phrasing_doc_id("早一点起飞"),
                phrasing_doc_id("别排周三"),
                phrasing_doc_id("教员换成孙军"),
            ],
            as_of(20),
            "多键召回：gold 给 5 条（Top-5 恰好装得下）。★ 全部 24 条 phrasing 偏好里"
            "取哪 5 条是**按 key 字典序前 5** 定的，避免「挑几条最容易召回的」这种"
            "自我实现的标注。",
            written=as_of(20),
        ),
        _probe(
            "MEM-PRO-007",
            "procedural",
            "preference",
            "带飞教员这件事我有什么习惯？",
            [phrasing_doc_id("带飞教员固定一个"), phrasing_doc_id("教员换成孙军")],
            as_of(20),
            "语义相近的两条偏好要一起召回。★ 它同时是一条**边界样本**：v6 §6.2 提到的"
            "「教员排班习惯」（`NAMESPACE_INSTRUCTOR`）至今没有可测的定义，"
            "M5 按铁律 5 没有自造 —— 所以这条只能靠 `phrasing` 命名空间回答。",
            written=as_of(20),
        ),
        _probe(
            "MEM-PRO-008",
            "procedural",
            "absent",
            "我偏好的目标函数三项权重是多少？",
            [],
            as_of(20),
            "★ 负例：目标权重（进度/扰动/均衡）属于 R3 偏好，但**系统从来没有蒸馏过"
            "这类偏好** —— `distill()` 只提炼松弛档与修订表述两族。正确行为是回答"
            "「没有记录到这类偏好」，而不是从松弛档偏好里编一个出来。",
        ),
    ]


def build_sample() -> list[dict[str, Any]]:
    return [*semantic_sample(), *episodic_sample(), *procedural_sample()]


# ══════════════════════════════════════════════════════════════════════
# 全量 320（语义 120 / 情景 120 / 程序 80）
# ══════════════════════════════════════════════════════════════════════

from backend.datasets.entities import (  # noqa: E402 —— 全量部分的依赖，放这里避免样例段被污染
    AIRCRAFT,
    AIRSPACES,
    MISSIONS,
    PERSONS,
)
from tests.datasets.memory_catalog import (  # noqa: E402
    EARLY_PREFERENCE_WEEK,
    REVISION_PHRASES,
    phrasing_key,
)

#: 学员已完成的课目（v6 §1.3.1）——先修判定的正负例都从这里推
COMPLETED: dict[str, tuple[str, ...]] = {
    "P05": ("missionA-1", "missionA-2", "missionB-1", "missionB-2", "missionC-1"),
    "P06": ("missionA-1", "missionA-2"),
    "P07": ("missionA-1", "missionA-2", "missionB-1"),
    "P08": ("missionA-1",),
}

TURNAROUND: dict[str, int] = {"JL-8": 30, "JL-9": 40}


def _sem(n: int) -> str:
    return f"MEM-SEM-{n:03d}"


def semantic_facts() -> list[dict[str, Any]]:
    """54 条事实召回：身份 8 · 机型资质 6 · 已完成 3 · 机型 7 · 周转 8 · 时长 11 ·
    频率 6 · 空域 5。

    **每类实体都被问遍**是刻意的 —— 漏掉的那条实体摘要在指标上是不可见的盲区：
    它可能根本没进语料，而 Recall 会把这件事平均掉。

    ⚠️ **没有跑道探针**：`entity_docs()` 只为 person / aircraft / mission / airspace
    四类发实体摘要文档，跑道在语料里**没有召回单位**。硬给它安一个 gold（比如塞给
    约束9 那条规则）会让这条题在测「规则召回」而不是「跑道事实」—— 那是自欺。
    这件事写进了卡片的已知局限。
    """
    specs: list[tuple[str, list[str], str, str]] = []
    for pid, (name, role) in PERSONS.items():
        specs.append(
            (
                f"{name}是什么身份？",
                [f"ent:person:{pid}"],
                role,
                f"身份事实（{name}/{pid}）。身份决定带飞规则（D-1：需带飞 = 课目带飞列为是 "
                f"∧ 身份为学员），答错会连带排错机组编成。",
            )
        )
    for pid in ("P01", "P03", "P04", "P05", "P07", "P08"):
        name = PERSONS[pid][0]
        types = "JL-8" if pid in COMPLETED else "JL-8、JL-9"
        specs.append(
            (
                f"{name}持有哪些机型资质？",
                [f"ent:person:{pid}"],
                types,
                f"机型资质（{name}/{pid}）。★ 学员只持 JL-8 —— 这是 D/E/G/H 五门课目"
                f"不生成任何学员候选的一半原因（v6 §1.4.1 的双重排除）。",
            )
        )
    for pid in ("P05", "P06", "P07"):
        specs.append(
            (
                f"{PERSONS[pid][0]}已经完成哪些课目？",
                [f"ent:person:{pid}"],
                "、".join(COMPLETED[pid]),
                f"进度事实（{PERSONS[pid][0]}/{pid}）。读 `person_completed_missions` 事实表，"
                f"不是 `training_progress.status`（Z-16）。",
            )
        )
    for plane, kind in AIRCRAFT.items():
        if plane == "AC73":
            continue  # 已在 MEM-SEM-002（易错事实 M2）
        specs.append(
            (
                f"{plane} 是什么机型？",
                [f"ent:aircraft:{plane}"],
                kind,
                f"机型事实（{plane}）。八架全覆盖，与 M2 那条一起把「AC73 是 JL-8」放进"
                f"一组同构的题里 —— 只问 AC73 一架的话，答对可能只是背下来了。",
            )
        )
    for plane, kind in AIRCRAFT.items():
        specs.append(
            (
                f"{plane} 的周转时间是多少分钟？",
                [f"ent:aircraft:{plane}"],
                f"{TURNAROUND[kind]} 分钟",
                f"周转时间（{plane}，{kind} → {TURNAROUND[kind]} 分钟）。★ 它由机型决定，"
                f"所以机型答错这条必然跟着错 —— 两题成对，能把错误定位到「机型认错」"
                f"而不是「周转记错」。",
            )
        )
    for mission, (minutes, _freq, _space) in MISSIONS.items():
        if mission == "missionE-2":
            continue  # 已在 MEM-SEM-005
        specs.append(
            (
                f"{mission} 要飞多长时间？",
                [f"ent:mission:{mission}"],
                f"{minutes} 分钟",
                f"课目时长（{mission}，{minutes} 分钟）。时长直接决定 §12.3 的 I4 构造"
                f"（训练窗压到 30 分钟）能封死哪些课目。",
            )
        )
    for mission in (
        "missionA-1",
        "missionA-2",
        "missionB-1",
        "missionC-1",
        "missionG-1",
        "missionH-1",
    ):
        freq = MISSIONS[mission][1]
        specs.append(
            (
                f"{mission} 多长时间要飞一次？",
                [f"ent:mission:{mission}"],
                f"每 {freq} 天 ≥1 次",
                f"频率事实（{mission}，freq_days={freq}）。★ 各课目用**自己的** freq_days "
                f"开滑动窗口（B.4）：A 类 3 天、B~F 类 7 天、G/H 类 14 天 —— "
                f"统一成 7 天是本项目最早裁掉的一个误读。三档取值各出现两次。",
            )
        )
    for space, capacity in AIRSPACES.items():
        if space == "IFR":
            continue  # 已在 MEM-SEM-006
        specs.append(
            (
                f"{space} 的同时段容量是多少？",
                [f"ent:airspace:{space}"],
                str(capacity),
                f"空域容量（{space}，{capacity}）。S-10 把它并入约束6 做成**硬约束**，"
                f"所以这个数直接决定绑定该空域的课目能不能同时排。",
            )
        )
    return [
        _probe(_sem(13 + i), "semantic", "fact", query, docs, W20, why, answer=answer)
        for i, (query, docs, answer, why) in enumerate(specs)
    ]


#: 先修判定的 18 个 (人, 课目) 组合。**正负例都要有** —— 只有负例的话，
#: 一个「一律答不能」的系统也能拿满分。
PREREQ_CASES: tuple[tuple[str, str, bool, str], ...] = (
    ("P08", "missionB-2", False, "A 类未整体达标（缺 missionA-2）"),
    ("P08", "missionC-1", False, "同上，何超的五条阻塞项之一"),
    ("P08", "missionC-2", False, "双重未达标：A 类没齐，C-1 也没飞"),
    ("P08", "missionF-1", False, "F 类先修同为 A 类整体"),
    ("P06", "missionB-1", True, "张勇 A 类已整体完成 → 解锁 B 类"),
    ("P06", "missionB-2", True, "同上"),
    ("P06", "missionC-1", True, "同上，C 类先修也是 A 类整体"),
    ("P06", "missionC-2", False, "★ C-2 的先修是**逐门的** missionC-1，不是「C 类整体」"),
    ("P06", "missionF-1", True, "F 类先修为 A 类整体"),
    ("P07", "missionB-2", True, "陈伟已完成 A 类与 B-1"),
    ("P07", "missionC-1", True, "A 类整体达标"),
    ("P07", "missionC-2", False, "缺 missionC-1（逐门先修）"),
    ("P07", "missionF-1", True, "A 类整体达标"),
    ("P05", "missionF-1", True, "罗磊进度最靠前，A/B/C-1 都完成了"),
    ("P05", "missionG-1", False, "★ 双重排除：G-1 走 JL-9（学员无机型资质）且先修含 F 类"),
    ("P04", "missionD-1", True, "刘斌全 12 门完成、全资质，D 类先修（B 类 + C 类）达标"),
    ("P04", "missionE-2", True, "E-2 的先修是逐门的 missionE-1，刘斌已完成"),
    ("P04", "missionG-1", True, "先修 A 类 + F 类都已完成，且持 JL-9 资质"),
)


def semantic_prereq() -> list[dict[str, Any]]:
    """18 条先修判定。

    这一族的答案**只有递归 CTE 算得出来**（`prereq_cte`），任何一句摘要文本里
    都没有 —— 它是 §12.4「语义类走 SQL 精确通道」这条结构性依靠的核心证据，
    也是 `Z-22` 那条消融「损失在可答性而非召回率」的直接观测点。
    """
    rows: list[dict[str, Any]] = []
    for offset, (pid, mission, ok, why) in enumerate(PREREQ_CASES):
        rows.append(
            _probe(
                _sem(67 + offset),
                "semantic",
                "prereq",
                f"{PERSONS[pid][0]}现在能排 {mission} 吗？",
                [f"ent:person:{pid}", f"ent:mission:{mission}"],
                W20,
                f"先修判定（{PERSONS[pid][0]}/{pid} × {mission}）：{why}。",
                answer="能" if ok else "不能",
            )
        )
    return rows


#: 18 条规则原文探针。约束7 与约束9 已在样例里，这里覆盖其余 12 条，
#: 另给 6 条「同一条规则的第二个问法」——**规格的细节往往藏在第二问里**。
RULE_PROBES: tuple[tuple[int, str, str, str], ...] = (
    (
        1,
        "训练窗是几点到几点？",
        "每日 06:00-18:00",
        "约束1 是 I4 构造（训练窗压到 30 分钟）唯一能收紧的旋钮",
    ),
    (
        2,
        "飞行员的资质到期之后还能飞吗？",
        "不能飞未持有效资质的课目",
        "★ 约束2 的字面语义被 S-11 授权改写：成熟飞行员到期资质转为强制复训",
    ),
    (
        3,
        "A 类课目每周必须飞吗？",
        "是，A 类整体至少 1 次",
        "S-02：A 类**整体**至少 1 次（飞 A-1 或 A-2 任一即可），不是每门都飞",
    ),
    (
        4,
        "带飞架次对教员有什么要求？",
        "需一名具备该课目资质的教员在位",
        "约束4 与约束3 一起决定带飞容量，是 I1 构造的封杀点",
    ),
    (5, "一个人同一时刻能出现在几个架次里？", "1 个", "人员不可重叠，最基础的一条资源约束"),
    (
        6,
        "同一时段一个空域能容纳几个架次？",
        "由该空域的容量决定，1 或 2",
        "★ S-10 把空域容量并入约束6 做成硬约束，对外仍称「14 条」",
    ),
    (8, "同一天的休息时间怎么算？", "仅同日内累计", "S-07：只在同日内累计，跨日不结转"),
    (
        10,
        "学员一天最多飞几次？",
        "受日上限约束",
        "日上限与周上限是两条不同的量，混用会让容量推演整体偏移",
    ),
    (
        11,
        "学员一周最多飞几次？",
        "受周上限约束",
        "★ 注入样本最爱篡改的就是这条（「学员周上限改为 20」）",
    ),
    (
        12,
        "飞机维护期间可以排班吗？",
        "不可以",
        "维护窗与周转时间同属约束7/12 的资源侧，AC73 的 01-09 定检走的就是它",
    ),
    (
        13,
        "课目的训练频率是怎么要求的？",
        "各课目按自己的 freq_days 开滑动窗口",
        "★ B.4 的核心：A 类 3 天、B~F 类 7 天、G/H 类 14 天，跨周由 last_done_date 锚点衔接",
    ),
    (
        14,
        "一周之内同一门课目最多排几次？",
        "ceil(7 / freq_days)：A 类 3，其余 1",
        "约束14 的 req_max 上界，防止把一周的额度全砸在一门课上",
    ),
    (
        3,
        "约束3 对已经完成全部课目的学员还生效吗？",
        "生效",
        "★ S-13：对**全部**学员生效、不论完成状态，语义是「保持熟练度」",
    ),
    (
        3,
        "整周都请假的学员还要满足每周必飞吗？",
        "不要，但约束13 不解开",
        "★ Z-9：判据**只看人在不在**，不看排不排得上；且它只解开约束3，"
        "有未完成课目的学员整周请假仍是 INFEASIBLE",
    ),
    (
        7,
        "飞机维护窗和周转时间是同一条约束吗？",
        "是，约束7 同时管两者",
        "约束7 是析取形态：相邻架次满足周转 ∧ 维护时段内不排",
    ),
    (
        13,
        "上一次飞行日期缺失的时候，频率窗口从哪天算起？",
        "视为从本周周一起算，不计欠账",
        "★ S-12：`last_done_date` 为 NULL 时不许当作已欠账（gap=999 会让基准周假性不可行）",
    ),
    (6, "空域容量算硬约束还是软目标？", "硬约束", "S-10 的裁定；写成软目标会让方案在空域上超卖"),
    (
        13,
        "跨周的首次执行截止日怎么算？",
        "first_exec_day ≤ max(0, freq_days − gap)",
        "★ D-4 的通式；SPEC_DECISIONS §B.4 第二分支的 −1 是笔误",
    ),
)


def semantic_rules() -> list[dict[str, Any]]:
    return [
        _probe(
            _sem(85 + offset),
            "semantic",
            "rule_text",
            query,
            [f"rule:1.3.0:{rule_id:02d}"],
            W20,
            f"规则原文召回（约束{rule_id}）。{why}。规则条文在语料里**禁止拆分**"
            f"（§5.3），所以 gold 是整条。",
            answer=answer,
        )
        for offset, (rule_id, query, answer, why) in enumerate(RULE_PROBES)
    ]


def semantic_aggregates() -> list[dict[str, Any]]:
    """9 条汇总类。gold 是**一组**实体摘要，Top-5 装不下的要在报数时单列。"""
    students = [f"ent:person:{p}" for p in ("P05", "P06", "P07", "P08")]
    instructors = [f"ent:person:{p}" for p in ("P01", "P02", "P03")]
    jl9 = [f"ent:aircraft:{a}" for a in ("AC84", "AC95")]
    specs: list[tuple[str, list[str], str, str]] = [
        (
            "一共有几名学员？",
            students,
            "4 名（罗磊、张勇、陈伟、何超）",
            "身份聚合；gold 4 条，Top-5 装得下",
        ),
        ("有几名教员？", instructors, "3 名（孙军、高超、吴鹏）", "同上，gold 3 条"),
        (
            "一共有几架 JL-9？",
            jl9,
            "2 架（AC84、AC95）",
            "★ 与「几架 JL-8」那条互为补集，两条都答对才说明 AC73 归类正确",
        ),
        (
            "哪些人持有 JL-9 的机型资质？",
            [*instructors, "ent:person:P04"],
            "三名教员 + 刘斌，学员一个都没有",
            "跨字段聚合；gold 4 条",
        ),
        (
            "哪些课目要飞 JL-9？",
            [
                f"ent:mission:{m}"
                for m in ("missionD-1", "missionE-1", "missionE-2", "missionG-1", "missionH-1")
            ],
            "D-1、E-1、E-2、G-1、H-1 五门",
            "★ gold 恰好 5 条，Top-5 刚好装满",
        ),
        (
            "哪些课目绑在 Small Area A？",
            [f"ent:mission:{m}" for m in ("missionA-1", "missionE-1", "missionE-2")],
            "missionA-1、missionE-1、missionE-2",
            "空域反查；gold 3 条",
        ),
        (
            "哪几门课目的频率是 14 天一次？",
            [f"ent:mission:{m}" for m in ("missionG-1", "missionH-1")],
            "missionG-1、missionH-1",
            "频率反查；G/H 两类",
        ),
        (
            "哪些空域的同时段容量只有 1？",
            [f"ent:airspace:{s}" for s in ("IFR", "RT1", "RT2", "RNG")],
            "IFR、RT1、RT2、RNG 四个",
            "容量反查；gold 4 条",
        ),
        (
            "学员能飞哪几类课目？",
            [
                "ent:person:P08",
                "ent:mission:missionA-1",
                "ent:mission:missionB-1",
                "ent:mission:missionC-1",
                "ent:mission:missionF-1",
            ],
            "A/B/C/F 四类",
            "★ 资质 + 机型的**双重排除**结论；gold 混合了人与课目两类文档",
        ),
    ]
    return [
        _probe(
            _sem(103 + i),
            "semantic",
            "aggregate",
            query,
            docs,
            W20,
            f"汇总类。{why}。这一族的正确判据是**答案对不对**，不是 gold 是否全进 Top-5。",
            answer=answer,
        )
        for i, (query, docs, answer, why) in enumerate(specs)
    ]


def semantic_absent() -> list[dict[str, Any]]:
    """9 条负例。**形态合法但不存在**才是难的那一类。"""
    specs: tuple[tuple[str, str, str], ...] = (
        (
            "P09 是谁？",
            "系统里没有 P09",
            "★ 编号只固定前缀不限位数（Z-4），P09 在形态上完全合法 —— 这正是它比 'XYZ' 难的地方",
        ),
        (
            "missionI-1 的先修是什么？",
            "系统里没有 missionI-1",
            "类别只到 H；I 类是个形态合法的空号",
        ),
        (
            "RWY-3 服务哪些机型？",
            "只有 RWY-1 与 RWY-2",
            "★ 跑道编号同样不限位数，多出一条跑道会让约束9 的分组静默失真",
        ),
        (
            "SAC 空域的容量是多少？",
            "没有 SAC 这个空域",
            "空域编号**不是枚举**（Z-4），所以更不能靠「不在枚举里」挡下来",
        ),
        ("AC00 现在能用吗？", "系统里没有 AC00", "机号空号"),
        ("有没有第 15 门课目？", "只有 12 门", "计数型负例"),
        ("约束15 是什么？", "规则集只有 14 条", "★ 规则条数是 14，多问一条应当答「没有」"),
        (
            "教员李伟本周排了几次？",
            "人员表里没有李伟",
            "★ 姓名型负例：与「孙俊」那种错别字不同，李伟不指向任何真实实体",
        ),
        (
            "JL-10 有几架？",
            "机型只有 JL-8 与 JL-9",
            "机型由上传数据决定（Z-4），所以这条也不能靠硬编码枚举挡",
        ),
    )
    return [
        _probe(
            _sem(112 + i),
            "semantic",
            "absent",
            query,
            [],
            W20,
            f"负例：{why}。正确行为是明确回答「没有」，而不是挑一个最接近的答上去。",
            answer=answer,
        )
        for i, (query, answer, why) in enumerate(specs)
    ]


# ── 情景类的全量构造 ────────────────────────────────────────────────

KIND_ORDER: tuple[str, ...] = (
    "schedule_session",
    "conflict_resolution",
    "user_revision",
    "user_rejection",
    "relaxation_choice",
    "approval",
)

QUERY_BY_KIND: dict[str, str] = {
    "schedule_session": "第 {w} 周一共排了多少架次？",
    "conflict_resolution": "第 {w} 周有哪门课被推迟了？为什么？",
    "user_revision": "第 {w} 周用户提了哪几条修订？",
    "user_rejection": "第 {w} 周用户驳回第一版的理由是什么？",
    "relaxation_choice": "第 {w} 周选的松弛档欠了多少项账？",
    "approval": "第 {w} 周是哪位领导批准的方案？",
}

WHY_BY_KIND: dict[str, str] = {
    "schedule_session": "会话摘要里的数值（架次/带飞/单飞/阻塞项）。★ 这一族最容易出现"
    "「召回对了但把 12 说成 14」的生成层错误，是 §12.4.1 Faithfulness "
    "判定的典型对象",
    "conflict_resolution": "归因型问题：哪门课被推迟、因为什么。答案只在当周的冲突解决"
    "摘要里，纸面规格推不出来",
    "user_revision": "修订原话召回。★ 同一句原话在 20 周里重复 2~3 次，所以这条同时考"
    "「能不能定位到**这一周**那一次」——时效与情景的交叉点",
    "user_rejection": "驳回理由召回。★ 用户不接受的方案形态比接受的更有信息量，"
    "是 §15.2 ⑥ 难负例挖掘的直接输入",
    "relaxation_choice": "松弛档与欠账数召回。欠账必须被显式披露（Z-9），"
    "所以这条也验了披露内容有没有进记忆",
    "approval": "批准记录召回（三位审批人循环出现）。★ 三个名字在语料里高频重复，"
    "是典型的「召回到了但选错那一条」场景",
}

#: 衰减测试的六个观测点（v6 §12.4：第 1/4/8/12/16/20 周写入的记忆在第 20 周的召回率）
DECAY_WEEKS: tuple[int, ...] = (1, 4, 8, 12, 16, 20)

#: 样例已占用的 (周, 事件类型)。全量构造必须避开它们，否则会出现两条问同一件事的探针。
_SAMPLE_PAIRS: frozenset[tuple[int, str]] = frozenset(
    {
        (3, "conflict_resolution"),
        (18, "conflict_resolution"),
        (12, "approval"),
        (7, "user_rejection"),
        (5, "schedule_session"),
        (1, "schedule_session"),
        (4, "approval"),
        (8, "user_revision"),
        (12, "conflict_resolution"),
        (16, "relaxation_choice"),
    }
)


def _epi(n: int) -> str:
    return f"MEM-EPI-{n:03d}"


#: 样例里**已经是衰减探针**的那几对。凑数时只能扣它们 —— 第 12 周还有一对
#: (12, approval) 被样例的**回忆**探针占着，那一对要避开，但不该算进衰减的配额。
_SAMPLE_DECAY_PAIRS: frozenset[tuple[int, str]] = frozenset(
    {
        (1, "schedule_session"),
        (4, "approval"),
        (8, "user_revision"),
        (12, "conflict_resolution"),
        (16, "relaxation_choice"),
    }
)


def _decay_pairs() -> list[tuple[int, str]]:
    """25 个新的衰减观测点：六个周次各凑满 5 条（样例里已有的衰减探针算在内）。"""
    pairs: list[tuple[int, str]] = []
    for week in DECAY_WEEKS:
        taken = sum(1 for w, _k in _SAMPLE_DECAY_PAIRS if w == week)
        need = 5 - taken
        for kind in KIND_ORDER:
            if need == 0:
                break
            if (week, kind) in _SAMPLE_PAIRS or (week, kind) in pairs:
                continue
            pairs.append((week, kind))
            need -= 1
    return pairs


def episodic_decay() -> list[dict[str, Any]]:
    """25 条衰减探针（+ 样例 5 条 = 30）。

    六个观测点写入的记忆，**统一在第 20 周提问**。目标是情景记忆 20 周后召回率
    ≥85%（§12.4）。★ 归档线是 60 周（最长 cycle_weeks 20 × 3，`Z-18`），所以
    这些记忆**一条都没有被归档** —— 测的是语料变大之后的排序退化，不是遗忘策略。
    把两件事混起来会得出「衰减是因为被归档了」的错误结论。
    """
    return [
        _probe(
            _epi(88 + i),
            "episodic",
            "decay",
            QUERY_BY_KIND[kind].format(w=week),
            [TL.doc_id(week, kind)],
            as_of(20),
            f"衰减观测点第 {week} 周（写入时距提问 {20 - week} 周）。{WHY_BY_KIND[kind]}。",
            written=as_of(week),
            week=week,
        )
        for i, (week, kind) in enumerate(_decay_pairs())
    ]


def episodic_recall() -> list[dict[str, Any]]:
    """75 条情景回忆（+ 样例 5 条 = 80）。覆盖 20 周 × 6 类事件里剩下的组合。"""
    used = set(_SAMPLE_PAIRS) | set(_decay_pairs())
    rows: list[dict[str, Any]] = []
    for week in range(1, 21):
        for kind in KIND_ORDER:
            if len(rows) == 75:
                return rows
            if (week, kind) in used:
                continue
            rows.append(
                _probe(
                    _epi(13 + len(rows)),
                    "episodic",
                    "episode_recall",
                    QUERY_BY_KIND[kind].format(w=week),
                    [TL.doc_id(week, kind)],
                    as_of(20),
                    f"第 {week} 周的{kind}事件。{WHY_BY_KIND[kind]}。",
                    written=as_of(week),
                    week=week,
                )
            )
    return rows


def episodic_temporal() -> list[dict[str, Any]]:
    """4 条时效探针（+ 样例 1 条 = 5）。

    全部围绕那对成对事件：IFR Route 第 6 周起容量降为 0（写入时带 `valid_to` =
    第 9 周），第 9 周恢复为 1。**同一个问题在不同时点有不同的正确答案** ——
    这正是 `Z-18` 那句「`superseded_by` 是链接不是墓碑，有效性只由
    `[valid_from, valid_to)` 决定」的可测形态。把它当作废标记，
    「第 7 周 IFR 能用吗」会一律返回空。
    """
    specs: tuple[tuple[int, str, str, str, str], ...] = (
        (
            8,
            "第 8 周的时候 IFR 还在检修吗？",
            TL.ifr_outage(),
            "还在，容量仍为 0",
            "第 8 周仍落在 [第6周, 第9周) 区间内 —— 旧版本此刻是**有效版本**，不是历史版本",
        ),
        (
            10,
            "第 10 周 IFR Route 能用吗？",
            TL.ifr_restored(),
            "能用，容量已恢复为 1",
            "跨过第 9 周之后，有效版本换成了恢复那一条",
        ),
        (
            20,
            "现在 IFR Route 的容量是多少？",
            TL.ifr_restored(),
            "1",
            "★ 「现在」= 提问时点；返回最新有效版本，并应显式标注历史版本数量（§6.4）",
        ),
        (
            9,
            "IFR 的检修是什么时候结束的？",
            TL.ifr_restored(),
            "第 9 周",
            "问的是变更本身而不是当前状态，正确落点仍是恢复那一条",
        ),
    )
    return [
        _probe(
            _epi(113 + i),
            "episodic",
            "temporal_validity",
            query,
            [doc],
            as_of(week),
            f"时效判定（提问时点 = 第 {week} 周）。{why}。",
            answer=answer,
            written=as_of(6 if doc == TL.ifr_outage() else 9),
            week=6 if doc == TL.ifr_outage() else 9,
        )
        for i, (week, query, doc, answer, why) in enumerate(specs)
    ]


def episodic_absent() -> list[dict[str, Any]]:
    """4 条负例（+ 样例 1 条 = 5）：时间线之外的时点。"""
    specs: tuple[tuple[str, str], ...] = (
        ("第 0 周排了多少架次？", "时间线从第 1 周起，没有第 0 周"),
        ("第 30 周的批准记录是谁签的？", "时间线只到第 20 周"),
        (
            "2025 年 12 月有哪些排班会话？",
            "★ 用日期而不是周次问，同样落在时间线之外 ——换一种问法不该换一种行为",
        ),
        ("有没有第 21 周的驳回记录？", "紧贴边界的一周；比问第 30 周更容易被硬答"),
    )
    return [
        _probe(
            _epi(117 + i),
            "episodic",
            "absent",
            query,
            [],
            as_of(20),
            f"负例：{why}。正确行为是回答「没有记录」，而不是把最近一周的数字拿来充数。",
        )
        for i, (query, why) in enumerate(specs)
    ]


# ── 程序类的全量构造 ────────────────────────────────────────────────


def _pro(n: int) -> str:
    return f"MEM-PRO-{n:03d}"


#: 松弛档偏好的 7 种换问法（+ 样例 1 条 = 8）。同一条偏好、同一个 gold，
#: **换问法是刻意的**：程序记忆走「key 前缀 + 语义」两路，只有一种问法测不出语义那一路。
RELAXATION_PHRASINGS: tuple[tuple[str, str], ...] = (
    ("我一般选哪一档松弛？", "最直白的问法"),
    ("我的松弛偏好是保守还是激进？", "把档位换成形容词，靠语义而非关键词命中"),
    ("系统记住我偏好哪个 Tier 了吗？", "元问题形态：问的是「有没有这条记忆」"),
    ("我批准过的方案大多是哪一档？", "★ 从证据侧问 —— 这条偏好正是从 20 条批准记录蒸馏来的"),
    ("我的松弛档偏好支持度是多少？", "问 `support` 字段，验蒸馏结果有没有把证据带上"),
    ("松弛这件事我有什么习惯？", "最模糊的问法，最依赖语义召回"),
    ("我偏好的 Tier 是几？", "英文档位词"),
)


def procedural_preferences() -> list[dict[str, Any]]:
    """54 条偏好召回（+ 样例 6 条 = 60）：relaxation 7 + phrasing 46 + 多键 1。

    **24 个 phrasing key 一个不落**，每个 key 两种问法（问含义 / 问次数）——
    这样某个 key 根本没被蒸馏出来时，会有两条题一起掉，而不是被平均掉。
    """
    rows: list[dict[str, Any]] = []
    for query, why in RELAXATION_PHRASINGS:
        rows.append(
            _probe(
                _pro(9 + len(rows)),
                "procedural",
                "preference",
                query,
                [RELAXATION_DOC_ID],
                W20,
                f"松弛档偏好，换一种问法。{why}。gold 与 MEM-PRO-001 相同 —— "
                f"七条同 gold 不同问法，量的是语义召回而不是字符串命中。",
                answer="Tier 1",
                written=W20,
            )
        )
    for phrase in REVISION_PHRASES:
        if phrase != "往后挪挪":  # 样例 MEM-PRO-003 已问过它的含义
            rows.append(
                _probe(
                    _pro(9 + len(rows)),
                    "procedural",
                    "preference",
                    f"我说「{phrase}」的时候是什么意思？",
                    [phrasing_doc_id(phrase)],
                    W20,
                    f"表述偏好（key={phrasing_key(phrase)}）。`distill()` 把重复出现 "
                    f"≥2 次的修订原话蒸馏成 `phrasing/<sha256 前 16 位>`；"
                    f"出现一次的不算习惯，这是 `min_support` 的直接体现。",
                    written=W20,
                )
            )
    for phrase in REVISION_PHRASES:
        if phrase != "别排周三":  # 样例 MEM-PRO-005 已问过它的次数
            rows.append(
                _probe(
                    _pro(9 + len(rows)),
                    "procedural",
                    "preference",
                    f"「{phrase}」这个说法我用过几次？",
                    [phrasing_doc_id(phrase)],
                    W20,
                    f"同一条偏好的 `support` 字段（{phrase}）。★ 与「是什么意思」那条配对："
                    f"两条题指向同一个 gold，一条掉了另一条也该掉 —— 成对出现让"
                    f"「某个 key 压根没蒸馏出来」这件事在指标上看得见。",
                    written=W20,
                )
            )
    rows.append(
        _probe(
            _pro(9 + len(rows)),
            "procedural",
            "preference",
            "我常用的表述里跟时间有关的有哪些？",
            [
                phrasing_doc_id("晚点飞"),
                phrasing_doc_id("往后挪挪"),
                phrasing_doc_id("早一点起飞"),
                phrasing_doc_id("这天别安排了"),
                phrasing_doc_id("周末不排"),
            ],
            W20,
            "多键召回（第二条）：按语义挑出与时间有关的五条表述。★ 与 MEM-PRO-006 "
            "「我常用的修订说法有哪些」不同 —— 那条不带筛选条件、gold 按 key 字典序取前 5，"
            "这条要求**理解筛选条件**，两条一起才能区分「会召回」与「会按条件召回」。",
            written=W20,
        )
    )
    return rows


def procedural_temporal() -> list[dict[str, Any]]:
    """11 条版本时效（+ 样例 1 条 = 12）。

    `relaxation/preferred_tier` 有两个版本：第 4 周的「对话推断」Tier 0，
    第 20 周蒸馏出的「排班确认记录」Tier 1（可信度**严格更高**才覆盖）。
    于是同一个问题在第 5~19 周答 Tier 0、第 20 周答 Tier 1。

    ⚠️ 这个构造是 W11 实测改过的：原打算「蒸馏两次得到两个版本」，真库上
    跑不出来 —— 同档来源的偏好不会被自动改写，只会升级人工（§6.4 ③ / FTS-2001）。
    **偏好不能被自动覆盖是刻意设计**，不是 bug。
    """
    specs: tuple[tuple[int, str, str], ...] = (
        (5, "第 5 周的时候我偏好哪一档？", "Tier 0"),
        (7, "第 7 周我习惯用哪一档松弛？", "Tier 0"),
        (9, "第 9 周那会儿我的松弛偏好是什么？", "Tier 0"),
        (11, "第 11 周的时候系统记的是哪一档？", "Tier 0"),
        (13, "第 13 周我偏好保守档还是激进档？", "Tier 0"),
        (15, "第 15 周的松弛偏好是什么？", "Tier 0"),
        (17, "第 17 周我一般选几档？", "Tier 0"),
        (19, "第 19 周的时候我的偏好还是老样子吗？", "Tier 0"),
        (20, "现在我的松弛偏好是哪一档？", "Tier 1"),
        (20, "我最新的松弛档偏好是什么时候定下来的？", "第 20 周（排班确认记录）"),
        (20, "我的松弛偏好被改过吗？", "改过一次：Tier 0 → Tier 1"),
    )
    return [
        _probe(
            _pro(63 + i),
            "procedural",
            "temporal_validity",
            query,
            [RELAXATION_DOC_ID],
            as_of(week),
            f"版本时效（提问时点 = 第 {week} 周）。"
            + (
                "第 4 周写入的「对话推断」版本此刻仍有效 —— 时效正确率判的是"
                "**返回提问时点有效的那一版**，不是最新版。"
                if answer == "Tier 0"
                else "第 20 周的蒸馏结果已经覆盖旧版；应返回新版并显式标注历史版本数量（§6.4）。"
            ),
            answer=answer,
            written=as_of(EARLY_PREFERENCE_WEEK if answer == "Tier 0" else 20),
            week=EARLY_PREFERENCE_WEEK if answer == "Tier 0" else 20,
        )
        for i, (week, query, answer) in enumerate(specs)
    ]


def procedural_absent() -> list[dict[str, Any]]:
    """7 条负例（+ 样例 1 条 = 8）：系统从来没有蒸馏过的偏好类型。"""
    specs: tuple[tuple[str, str, str], ...] = (
        (W20, "我偏好哪个空域？", "`distill()` 只提炼松弛档与修订表述两族，没有空域偏好"),
        (W20, "我习惯几点开始飞？", "时间偏好同样不在蒸馏范围内"),
        (W20, "我偏好的每日架次上限是多少？", "密度偏好不在蒸馏范围内"),
        (W20, "我喜欢用哪架飞机？", "没有机型/机号偏好这一族"),
        (
            W20,
            "我对跑道有偏好吗？",
            "★ 跑道偏好听起来很合理，但系统里确实没有 —— 这类「听起来该有」的负例比明显越界的更难",
        ),
        (
            as_of(2),
            "第 2 周的时候我有什么偏好记录？",
            "★ 时点负例：第一条偏好写于第 4 周，第 2 周时偏好表是空的",
        ),
        (
            as_of(10),
            "第 10 周的时候系统记住我说过「往后挪挪」吗？",
            "★ 最容易错的一条：`phrasing` 偏好是第 20 周蒸馏出来的，`valid_from` 就是第 20 周 —— "
            "在第 10 周提问时它**还不存在**。返回它就是把「后来才知道的事」当成当时就知道",
        ),
    )
    return [
        _probe(
            _pro(74 + i),
            "procedural",
            "absent",
            query,
            [],
            at,
            f"负例：{why}。正确行为是回答「没有这类记录」，不是从相邻的偏好里编一个。",
        )
        for i, (at, query, why) in enumerate(specs)
    ]


def build_full() -> list[dict[str, Any]]:
    """全量 320：语义 120 / 情景 120 / 程序 80。"""
    return [
        *semantic_sample(),
        *semantic_facts(),
        *semantic_prereq(),
        *semantic_rules(),
        *semantic_aggregates(),
        *semantic_absent(),
        *episodic_sample(),
        *episodic_recall(),
        *episodic_decay(),
        *episodic_temporal(),
        *episodic_absent(),
        *procedural_sample(),
        *procedural_preferences(),
        *procedural_temporal(),
        *procedural_absent(),
    ]
