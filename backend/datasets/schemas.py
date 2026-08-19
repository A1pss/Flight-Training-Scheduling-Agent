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


__all__ = [
    "AdversarialKind",
    "ConstraintModifier",
    "DatasetItem",
    "ExpectedAction",
    "ModifierKind",
    "NLItem",
    "NLLayer",
    "NLSlots",
]
