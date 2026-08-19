"""各评测数据集的条目契约。**加载即校验，不通过就不许用。**

字段名沿用系统里已有的枚举（`backend.schemas.intent.Intent` 六类意图、
`RevisionKind` 六种增量约束），**不另起一套** —— 数据集与被测系统用两套词表，
最后一定会在「这条算不算答对」上吵起来。
"""

from __future__ import annotations

import re
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.datasets.entities import is_known_entity
from backend.harness.acl import ACL_MATRIX
from backend.harness.tools import TOOL_CATALOG
from backend.harness.types import ALL_COMPONENTS
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


# ══════════════════════════════════════════════════════════════════════
# trajectory_100（v6 §12.6）
# ══════════════════════════════════════════════════════════════════════

#: 五类流程 + 两处受控自治。§12.6.2 明确要求 **Knowledge 检索循环与 Diagnosis
#: 探测循环合计占标注集一半以上** —— 排班/重排的期望路径是固定序列（主体为静态
#: 工作流），在那两类上轨迹评估只验「没跑偏」；真正考察自主决策质量的是这两处。
TrajectoryFlow = Literal[
    "query",
    "diagnosis",
    "schedule",
    "reschedule",
    "revision",
    "ingest",
]

#: 图节点（`backend/graph/graph.py` 的 `add_node` 逐字对齐）
GRAPH_NODES: Final[frozenset[str]] = frozenset(
    {
        "route",
        "planner",
        "compile_spec",
        "solve",
        "validate",
        "explain",
        "knowledge",
        "diagnosis",
        "resume_guard",
        "human_gate",
        "commit_plan",
        "END",
    }
)

#: 图外的确定性阶段。摄取不在对话图内跑（走 `POST /api/v1/ingest`），
#: 但它的两段式 + 人工确认门禁同样是要被验证的路径。
PIPELINE_STAGES: Final[frozenset[str]] = frozenset(
    {"ingest.prepare", "ingest.gate", "ingest.commit"}
)


def _valid_path_element(element: str) -> bool:
    if element in GRAPH_NODES or element in PIPELINE_STAGES:
        return True
    return element.startswith("tool:") and element.removeprefix("tool:") in TOOL_CATALOG


class ToolStep(BaseModel):
    """轨迹里的一次工具调用及其期望参数。"""

    model_config = ConfigDict(extra="forbid")

    order: int = Field(ge=1)
    component: str = Field(description="发起调用的组件（ACL 矩阵的行）")
    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    #: 信息已足够时可以不调 —— 不调**不算缺失调用**
    optional: bool = False
    #: 等价工具：换成它们同样判对
    alternatives: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _tool_is_allowed(self) -> ToolStep:
        """★ 工具必须在目录里，**而且必须是该组件 ACL 行的子集**。

        这条校验挡的是标注本身的错误：给 `planner` 标一次 `probe_solve`
        看起来很合理（探一下影响面嘛），但 ACL 只把探针给了 `diagnosis`
        （§7.7.2 的唯一例外 + 独立预算池）。一份把越权调用标成「期望路径」的
        数据集，会把 §12.5.1 的越权拦截率直接测成负数。
        """
        if self.component not in ALL_COMPONENTS:
            raise ValueError(f"{self.component!r} 不是 ACL 矩阵里的组件")
        for name in (self.tool, *self.alternatives):
            if name not in TOOL_CATALOG:
                raise ValueError(f"{name!r} 不在工具目录里")
            if name not in ACL_MATRIX[self.component]:
                raise ValueError(f"{self.component} 无权调用 {name!r}（v6 §7.7.2 的权限矩阵）")
        # ★ 参数必须过该工具**真实的** Pydantic 契约。
        # 一份参数写错的标注会把「参数准确率」测成模型的问题，而实际错的是标注 ——
        # W11 造数据时这条校验就抓出了三处：`check_authority` 的字段名、
        # `rank_relaxations.prefer` 的枚举值、`propose_solve_intent.intent`
        # （它要的是完整的 SolveIntent，不是一个字典片段）。
        TOOL_CATALOG[self.tool].params_model.model_validate(self.params)
        return self


class TrajectoryItem(DatasetItem):
    """`trajectory_100` 的一条标注（§12.6.2）。"""

    item_id: str = Field(pattern=r"^TRJ-[A-Z]{3}-\d{3}$")
    flow: TrajectoryFlow
    utterance: str = Field(min_length=1)
    #: 前置状态（「已有一版已批准计划」「求解返回 INFEASIBLE」）。
    #: 轨迹是**有状态**的，同一句话在不同前置下的正确路径不同。
    setup: str = Field(min_length=1)
    expected_path: list[str] = Field(min_length=2)
    #: **可接受的替代路径**：不同但同样合理，判定时按对即可（§12.6.2 原文的要求）
    acceptable_paths: list[list[str]] = Field(default_factory=list)
    #: 明确判错的路径。没有它，「可接受」是没有边界的
    forbidden_paths: list[list[str]] = Field(default_factory=list)
    steps: list[ToolStep] = Field(default_factory=list)
    rationale: str = Field(min_length=1)

    @field_validator("expected_path")
    @classmethod
    def _path_shape(cls, value: list[str]) -> list[str]:
        for element in value:
            if not _valid_path_element(element):
                raise ValueError(f"{element!r} 既不是图节点、也不是 tool:<已登记工具>")
        return value

    @field_validator("acceptable_paths", "forbidden_paths")
    @classmethod
    def _alt_shape(cls, value: list[list[str]]) -> list[list[str]]:
        for path in value:
            for element in path:
                if not _valid_path_element(element):
                    raise ValueError(f"{element!r} 既不是图节点、也不是 tool:<已登记工具>")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> TrajectoryItem:
        """三条跨字段规则。

        ① 替代路径不许与期望路径相同（那不是替代，是抄一遍）；
        ② 禁止路径不许同时出现在可接受里（自相矛盾的标注会让判定器两边都对）；
        ③ `steps` 里的工具必须出现在期望路径**或某条可接受路径**上 —— 标了参数
           却哪条路径都不经过的步骤，判定时永远比不到，等于白标。
           （`optional=True` 的步骤经常只出现在替代路径里，所以并集是对的口径。）
        """
        expected = list(self.expected_path)
        for path in self.acceptable_paths:
            if path == expected:
                raise ValueError(f"{self.item_id}：替代路径与期望路径完全相同")
        for path in self.forbidden_paths:
            if path in self.acceptable_paths or path == expected:
                raise ValueError(f"{self.item_id}：同一条路径既被禁止又被接受")
        reachable = [*expected, *(e for path in self.acceptable_paths for e in path)]
        on_path = {e.removeprefix("tool:") for e in reachable if e.startswith("tool:")}
        for step in self.steps:
            if step.tool not in on_path:
                raise ValueError(
                    f"{self.item_id}：步骤 {step.tool!r} 在期望路径与全部可接受路径里都没出现"
                )
        return self


# ══════════════════════════════════════════════════════════════════════
# tool_calls_200（v6 §12.5.1）
# ══════════════════════════════════════════════════════════════════════

#: 三层。`valid` 是主体 200 条（按工具使用频率加权）；另两层各 30 条**用故障注入构造**。
ToolCallStratum = Literal["valid", "acl_violation", "budget_exhaustion"]

#: 期望结局。`accept` = 契约校验通过并执行；两个 `reject_*` 是护栏该拦下来的。
ToolCallExpectation = Literal["accept", "reject_acl", "reject_budget"]


class ToolCallItem(DatasetItem):
    """`tool_calls_200` 的一条场景（§12.5.1）。

    **标签天然正确**：场景由实体表 + 工具 schema 反向构造 —— 参数是从工具自己的
    `params_model` 生成并校验过的，越权对是拿 ACL 矩阵取补集算出来的，
    预算耗尽是把预算池设成 0 之后的必然结果。没有一处依赖人的判断。
    """

    item_id: str = Field(pattern=r"^TOOL-(?:VAL|ACL|BGT)-\d{3}$")
    stratum: ToolCallStratum
    component: str
    tool: str
    #: 该工具在目录里存不存在。**越权层里有一部分是模型凭空编出来的工具名** ——
    #: 14B 上这不是罕见事，而它与「有这个工具但没权限」是两种不同的失败模式
    #: （前者在没有第三层拦截时会以 `KeyError` 出现，统计与日志全错位，§7.7.2）。
    tool_exists: bool = True
    #: 触发这次调用的情形（自然语言）
    prompt_context: str = Field(min_length=1)
    #: 期望参数。`valid` 层里它必然过工具契约；另两层是「本来会发出的那次调用」
    expected_params: dict[str, Any] = Field(default_factory=dict)
    expectation: ToolCallExpectation
    #: 期望的错误码。越权 = FTS-4004；Harness 预算 = FTS-4003；
    #: **探针预算耗尽没有错误码** —— 它优雅返回 `BUDGET_EXHAUSTED` 载荷而不抛错
    expected_error_code: str | None = None
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def _consistency(self) -> ToolCallItem:
        """四条跨字段规则，把「标签天然正确」这句话变成可执行的断言。"""
        prefix = {"valid": "VAL", "acl_violation": "ACL", "budget_exhaustion": "BGT"}[self.stratum]
        if not self.item_id.startswith(f"TOOL-{prefix}-"):
            raise ValueError(f"{self.item_id}：编号前缀与 stratum={self.stratum} 不符")
        if self.component not in ALL_COMPONENTS:
            raise ValueError(f"{self.component!r} 不是 ACL 矩阵里的组件")
        if self.tool_exists and self.tool not in TOOL_CATALOG:
            raise ValueError(f"{self.tool!r} 不在工具目录里（tool_exists=True）")
        if self.stratum == "valid":
            if self.tool not in ACL_MATRIX[self.component]:
                raise ValueError(
                    f"{self.item_id}：valid 层却越权（{self.component} 无权调 {self.tool}）"
                )
            if self.expectation != "accept":
                raise ValueError(f"{self.item_id}：valid 层的期望结局只能是 accept")
            # ★ 参数必须真的过工具契约 —— 这一层的全部意义就是「标签天然正确」
            TOOL_CATALOG[self.tool].params_model.model_validate(self.expected_params)
        if self.stratum == "acl_violation":
            if self.expectation != "reject_acl" or self.expected_error_code != "FTS-4004":
                raise ValueError(f"{self.item_id}：越权层必须期望 reject_acl / FTS-4004")
            if self.tool_exists and self.tool in ACL_MATRIX[self.component]:
                raise ValueError(
                    f"{self.item_id}：标成越权，但 {self.component} 其实有权调 {self.tool}"
                )
        if self.stratum == "budget_exhaustion" and self.expectation != "reject_budget":
            raise ValueError(f"{self.item_id}：超预算层的期望结局只能是 reject_budget")
        return self


__all__ = [
    "GRAPH_NODES",
    "PIPELINE_STAGES",
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
    "ToolCallExpectation",
    "ToolCallItem",
    "ToolCallStratum",
    "ToolStep",
    "TrajectoryFlow",
    "TrajectoryItem",
]
