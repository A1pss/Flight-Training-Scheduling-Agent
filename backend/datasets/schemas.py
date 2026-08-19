"""各评测数据集的条目契约。**加载即校验，不通过就不许用。**

字段名沿用系统里已有的枚举（`backend.schemas.intent.Intent` 六类意图、
`RevisionKind` 六种增量约束），**不另起一套** —— 数据集与被测系统用两套词表，
最后一定会在「这条算不算答对」上吵起来。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.datasets.entities import is_known_entity
from backend.schemas.intent import Intent, RevisionKind

#: §12.2 的六层。`ambiguous` 层的期望动作恒为 `ask_clarify`（「正确地反问」计为成功）。
NLLayer = Literal[
    "standard_schedule",
    "targeted_schedule",
    "disrupted_reschedule",
    "info_query",
    "ambiguous",
    "adversarial",
]

#: 系统对一条用户输入的**最终动作**。§12.2 的端到端任务完成率只考核这一列。
#:
#: - `solve` / `reschedule`：进排班链路（`planner → compile_spec → solve → …`）
#: - `answer`：`KnowledgeAgent` 直接作答
#: - `ask_clarify`：反问澄清 —— **合法期望动作**，答对了计成功
#: - `route_ingest` / `route_export`：转对应端点（不在对话图内跑）
#: - `refuse`：超纲或注入，拒绝并说明理由
ExpectedAction = Literal[
    "solve",
    "reschedule",
    "answer",
    "ask_clarify",
    "route_ingest",
    "route_export",
    "refuse",
]

#: 对抗样本的子类（§12.2 对抗层 + §15.2 ⑥ 难负例）。
AdversarialKind = Literal[
    "typo",
    "near_confusable",
    "colloquial",
    "multi_intent",
    "out_of_scope",
    "injection",
]

#: 约束修饰的类型。前六个与 `RevisionKind` 逐字对齐，`OTHER` 收容那些说得出、
#: 但当前 DSL 表达不了的修饰（它们的正确去向是反问或忽略，不是硬塞一个 kind）。
ModifierKind = Literal[
    "FORBID",
    "PIN_TIME",
    "PIN_RESOURCE",
    "SHIFT_WINDOW",
    "REDUCE_DENSITY",
    "PIN_RUNWAY",
    "OTHER",
]

_WEEK_RE = re.compile(r"^\d{4}W\d{2}$")


class DatasetItem(BaseModel):
    """全部数据集条目的共同基类。

    只强制一件事：**每条都有唯一编号**。加载器据此查重（`item_id` 重复意味着
    有人复制粘贴之后忘了改号，那两条在统计里会被当成一条）。
    """

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)


class ConstraintModifier(BaseModel):
    """一条约束修饰槽位。`surface` 留原话片段，供人工复核时对得上。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ModifierKind
    surface: str = Field(min_length=1, description="用户原话里的对应片段")
    targets: list[str] = Field(default_factory=list, description="实体编号；无具体对象则空")

    @field_validator("targets")
    @classmethod
    def _known(cls, value: list[str]) -> list[str]:
        for target in value:
            if not is_known_entity(target):
                raise ValueError(f"{target!r} 不在 v6 §1.3 的基准实体表里")
        return value


class NLSlots(BaseModel):
    """§12.2 的五类槽位。槽位 F1 就按这五类分别算。"""

    model_config = ConfigDict(extra="forbid")

    persons: list[str] = Field(default_factory=list, description='person_id 或 ["ALL"]')
    aircraft: list[str] = Field(default_factory=list)
    missions: list[str] = Field(default_factory=list)
    week: str | None = Field(default=None, description="ISO 周，如 2026W02；说不清就 None")
    constraint_modifiers: list[ConstraintModifier] = Field(default_factory=list)

    @field_validator("persons", "aircraft", "missions")
    @classmethod
    def _known(cls, value: list[str]) -> list[str]:
        for entity in value:
            if not is_known_entity(entity):
                raise ValueError(f"{entity!r} 不在 v6 §1.3 的基准实体表里")
        return value

    @field_validator("week")
    @classmethod
    def _iso_week(cls, value: str | None) -> str | None:
        if value is not None and not _WEEK_RE.match(value):
            raise ValueError(f"周次要写成 2026W02 这种形态，实际 {value!r}")
        return value


class NLItem(DatasetItem):
    """`nl_360` 的一条标注（§12.2）。"""

    item_id: str = Field(pattern=r"^NL-[A-Z]{3}-\d{3}$")
    layer: NLLayer
    utterance: str = Field(min_length=1)
    expected_intent: Intent
    expected_slots: NLSlots
    expected_action: ExpectedAction
    #: 构造理由：这条为什么长这样、考的是什么。**人工复核时读的就是它。**
    rationale: str = Field(min_length=1)
    #: 仅对抗层非空
    adversarial_kind: AdversarialKind | None = None
    #: 该条若涉及修订翻译，期望的 `RevisionKind`（供 §12.6 修订层交叉引用）
    revision_kind: RevisionKind | None = None

    @model_validator(mode="after")
    def _layer_consistency(self) -> NLItem:
        """两条跨字段规则，写在这里而不是靠人自觉。

        ① `adversarial_kind` 与对抗层**互为充要** —— 标了子类却不在对抗层，
           分层统计会当场对不上；
        ② 歧义层的期望动作**恒为 `ask_clarify`**。这一层的全部意义就是
           「说不清的时候系统该反问而不是猜一个」（§12.2 主指标口径），
           哪怕只有一条被标成 `solve`，误执行率这个数就失去意义了。
        """
        if (self.adversarial_kind is not None) != (self.layer == "adversarial"):
            raise ValueError(
                f"{self.item_id}：adversarial_kind 只在对抗层出现且必须出现，"
                f"实际 layer={self.layer} kind={self.adversarial_kind}"
            )
        if self.layer == "ambiguous" and self.expected_action != "ask_clarify":
            raise ValueError(
                f"{self.item_id}：歧义层的期望动作只能是 ask_clarify，实际 {self.expected_action}"
            )
        return self


# ══════════════════════════════════════════════════════════════════════
# memory_320（v6 §12.4）
# ══════════════════════════════════════════════════════════════════════

MemoryType = Literal["semantic", "episodic", "procedural"]

#: 探针的考点。`absent` 是**负例**：正确行为是一条都不召回（或明确回答「没有记录」），
#: 它不参与 Recall@5 的分母，单独统计误召回率 —— 全是正例的探针集测不出这件事。
ProbeKind = Literal[
    "fact",
    "prereq",
    "rule_text",
    "aggregate",
    "episode_recall",
    "temporal_validity",
    "decay",
    "preference",
    "absent",
]

#: 召回单位的 id 形态。四类分别对应：
#: 实体摘要句（`backend.retrieval.corpus.entity_docs`）、规则原文、情景记忆摘要、
#: 程序记忆偏好。**前三类与运行时代码里的 id 逐字一致**；`proc:` 是本数据集
#: 引入的约定（当前 `preference_docs()` 只返回句子、不发 id，见数据集卡片的已知局限）。
_DOC_ID_RE = re.compile(
    r"^(?:"
    r"ent:(?:person|aircraft|mission|airspace):[A-Za-z0-9-]+"
    r"|rule:\d+\.\d+\.\d+:\d{2}"
    r"|epi:epi_[0-9a-f]{24}"
    r"|proc:(?:relaxation|phrasing|instructor)/[A-Za-z0-9_-]+"
    r")$"
)


class MemoryItem(DatasetItem):
    """`memory_320` 的一条探针（§12.4）。"""

    item_id: str = Field(pattern=r"^MEM-(?:SEM|EPI|PRO)-\d{3}$")
    memory_type: MemoryType
    probe_kind: ProbeKind
    query: str = Field(min_length=1)
    #: 期望召回的文档 id 集合（gold）。Recall@5 判的就是它与 Top-5 的交集。
    expected_doc_ids: list[str] = Field(default_factory=list)
    #: **提问时点**。时效正确率判的是「返回的是这个时点有效的版本吗」（§6.4）。
    as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    #: 该记忆的写入时点；语义类事实随快照走，没有独立写入时点，填 None。
    written_at: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    #: 20 周时间线上的第几周（衰减测试用）。语义类与不挂时间线的探针为 None。
    timeline_week: int | None = Field(default=None, ge=1, le=20)
    #: 期望答案（语义类必填 —— 四条易错事实的正确答案就靠它钉住）
    expected_answer: str | None = None
    rationale: str = Field(min_length=1)

    @field_validator("expected_doc_ids")
    @classmethod
    def _doc_id_shape(cls, value: list[str]) -> list[str]:
        for doc_id in value:
            if not _DOC_ID_RE.match(doc_id):
                raise ValueError(f"{doc_id!r} 不是合法的召回 id 形态")
            if doc_id.startswith("ent:"):
                entity = doc_id.split(":", 2)[2]
                if not is_known_entity(entity):
                    raise ValueError(f"{doc_id!r} 指向的实体不在 v6 §1.3 基准表里")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> MemoryItem:
        """四条跨字段规则。

        ① 编号前缀与记忆类型必须一致（`MEM-SEM-*` 只能是 semantic）；
        ② **除 `absent` 外，gold 集不许为空** —— 空 gold 的探针在 Recall@5 里
           既不算对也不算错，是一条会污染分母的静默样本；
        ③ `absent` 的 gold 必须为空，否则它就不是负例了；
        ④ `decay` 探针必须挂在时间线上，否则「第几周写入的记忆」无从谈起。
        """
        prefix = {"semantic": "SEM", "episodic": "EPI", "procedural": "PRO"}[self.memory_type]
        if not self.item_id.startswith(f"MEM-{prefix}-"):
            raise ValueError(f"{self.item_id}：编号前缀与 memory_type={self.memory_type} 不符")
        if self.probe_kind == "absent":
            if self.expected_doc_ids:
                raise ValueError(f"{self.item_id}：absent 探针的 gold 集必须为空")
        elif not self.expected_doc_ids:
            raise ValueError(f"{self.item_id}：非 absent 探针必须给出期望召回的文档 id")
        if self.probe_kind == "decay" and self.timeline_week is None:
            raise ValueError(f"{self.item_id}：decay 探针必须标出写入所在的时间线周次")
        return self


__all__ = [
    "AdversarialKind",
    "ConstraintModifier",
    "DatasetItem",
    "ExpectedAction",
    "MemoryItem",
    "MemoryType",
    "ModifierKind",
    "NLItem",
    "NLLayer",
    "NLSlots",
    "ProbeKind",
]
