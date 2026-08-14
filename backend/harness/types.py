"""Harness 的公共类型（v6 §7.7）。

这里只放**跨模块共享**的名字：组件名、失败模式、工具规格、调用结果。
放在一处是为了让 `acl` / `validation` / `registry` / `harness` 四个模块对
「什么是一个工具」有唯一一份定义——工具表分裂过一次，权限矩阵就废了。
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.llm.types import CallMode, ToolSchema

#: v6 §7.7.2 权限矩阵的六列。命名与目录一致：
#: `components/` 的 route / planner / extract / explain 四个 LLM 节点，
#: `agents/` 的 knowledge / diagnosis 两个 Agent（v6 §7.2.2、§7.2.3）。
ComponentName = Literal["route", "planner", "extract", "knowledge", "diagnosis", "explain"]

#: 权限矩阵列名 → 中文标签（日志与错误信息里用，对齐 v6 §7.7.2 的表头）
COMPONENT_LABELS: Final[dict[ComponentName, str]] = {
    "route": "意图路由",
    "planner": "Planner",
    "extract": "摄取抽取",
    "knowledge": "Knowledge",
    "diagnosis": "Diagnosis",
    "explain": "解释生成",
}

ALL_COMPONENTS: Final[tuple[ComponentName, ...]] = (
    "route",
    "planner",
    "extract",
    "knowledge",
    "diagnosis",
    "explain",
)


class FailureMode(StrEnum):
    """契约校验失败的五类归因（v6 §12.5.1「失败模式分类」）。

    **这五个枚举值是外部口径，不许改名、不许加减。** 两个下游直接依赖它：

    - v6 §15.2 的难负例挖掘按这张分布表选样本；
    - v6 §12.5.1 的「硬地板 x」只能从 `ENTITY_HALLUCINATION` 的占比观测——
      W13 判断最终通过率目标能不能从 97% 上调回 98%，靠的就是这个数。

    改了名字或者把两类合并，上面两件事就都失去数据基础。
    """

    #: 必填字段没给
    MISSING_FIELD = "missing_field"
    #: 字段类型不对（把 int 写成 str、把对象写成数组…）
    TYPE_ERROR = "type_error"
    #: 实体编号是编的：格式对但库里没有，或者干脆填了个人名
    ENTITY_HALLUCINATION = "entity_hallucination"
    #: 枚举/字面量取值越界（`freeze_policy="随便"`）
    ENUM_OUT_OF_RANGE = "enum_out_of_range"
    #: 输出根本不是合法 JSON
    JSON_MALFORMED = "json_malformed"


class ValidationFailure(BaseModel):
    """一次契约校验失败。**必须能回答「哪个字段、期望什么、实际收到什么」**
    ——v6 §7.7.1 第 1 行要求把这三样回灌给模型，笼统的「参数错误」没用。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: FailureMode
    tool: str = ""
    field_path: str = ""
    expected: str = ""
    actual: str = ""
    message: str = Field(min_length=1)

    def as_feedback_line(self) -> str:
        """回灌给模型的一行人话。"""
        where = f"{self.tool}.{self.field_path}" if self.field_path else self.tool or "输出"
        parts = [f"[{self.mode.value}] {where}：{self.message}"]
        if self.expected:
            parts.append(f"期望 {self.expected}")
        if self.actual:
            parts.append(f"实际收到 {self.actual}")
        return "；".join(parts)


class ValidatedCall(BaseModel):
    """通过契约校验的工具调用。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: 校验后的参数（Pydantic 归一化过，可直接喂给 handler）
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """一次工具执行的结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    ok: bool = True
    value: Any = None
    error: str = ""
    cached: bool = False


#: 工具处理函数：入参已通过契约校验，出参必须是可 JSON 序列化的值
#: （要进 trace、进 Redis 缓存，不可序列化的对象在重放时对不上）。
ToolHandler = Callable[[dict[str, Any]], Any]


class ToolSpec(BaseModel):
    """一个工具的完整规格。

    `params_model` 是**唯一**的入参真相：JSON Schema 由它导出（给模型看），
    校验也由它执行（给运行时用）。手写一份 schema 再手写一遍校验，两边迟早
    会漂移，而漂移的表现是「模型按 schema 填了参数，运行时说不合法」。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    params_model: type[BaseModel]
    #: 确定性工具（同 snapshot_id + 同参数 → 同结果）才允许进缓存（§7.7.1 第 6 行）
    deterministic: bool = True
    #: 是否写数据。除 `memory.write` 外一律 False —— §7.7.2 最后一行的架构级禁令
    writes: bool = False
    #: 预算池。`probe` 走 §3.9.2 的独立池，不与常规工具调用共享
    budget_pool: Literal["default", "probe"] = "default"

    def json_schema(self) -> dict[str, Any]:
        """导出给模型的 JSON Schema。"""
        return self.params_model.model_json_schema()

    def to_tool_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.json_schema(),
        )


class AgentSpec(BaseModel):
    """一个 LLM 组件的调用规格（v6 §7.7 伪码里的 `AgentSpec`）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: ComponentName
    #: 本次调用要暴露给模型的工具子集。必须是该组件 ACL 行的子集，
    #: 由 `Harness` 在构造请求前强制核对（少给可以，多给不行）。
    tools: tuple[str, ...] = ()
    #: 提示词键（`prompts/<component>/<key>.md`），随 trace 记 `prompt_version`
    prompt_key: str = "system"
    #: 契约重试次数上限。v6 §7.7.1：失败回灌重试 **≤2 次**
    max_retries: int = Field(default=2, ge=0, le=2)
    #: 是否要求模型必须给出至少一个工具调用（纯生成型组件为 False）
    requires_tool_call: bool = True
    #: `constrained_json` 模式下的目标 schema；为空时用工具表的联合 schema
    output_schema: dict[str, Any] | None = None

    @property
    def structured_output(self) -> bool:
        """本次调用要的是**受约束的结构化输出**，不是工具调用。

        v6 §7.2.1 的意图路由兜底就是这一形态：「受约束解码到 6 类枚举 + 槽位」
        —— 产物是一个 `{"intent": ..., "slots": ...}` 对象，不是 tool call。
        判据是三者同时成立：给了 `output_schema`、没给工具、也不要求工具调用。

        **不要把它和「纯生成」混起来**：`explain_llm` 写一段给人看的解释同样
        不给工具，但它没有 `output_schema`，走的是 `native`。区别就在这一个字段。
        """
        return self.output_schema is not None and not self.tools and not self.requires_tool_call


class AttemptRecord(BaseModel):
    """单次尝试的结果（首次 + 重试各算一次），供 §12.5.1 统计。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt: int = Field(ge=0)
    mode: CallMode
    failures: tuple[ValidationFailure, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.failures


class AgentOutput(BaseModel):
    """`Harness.call()` 的返回。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component: ComponentName
    calls: tuple[ValidatedCall, ...] = ()
    results: tuple[ToolResult, ...] = ()
    text: str = ""
    mode: CallMode = "native"
    attempts: tuple[AttemptRecord, ...] = ()
    #: 重试耗尽 → FTS-4002 转人工表单（v6 §7.7 伪码的 `degrade_to_form`）
    degraded: bool = False
    #: 降级或预算熔断时的对外错误契约
    error_code: str = ""
    error_message: str = ""
    #: 本次调用消耗的 LLM 请求数（含重试），即 §12.5.1 的「重试系数」分子
    llm_calls: int = 0
    prompt_version: str = ""

    @property
    def first_pass(self) -> bool:
        """首次输出即通过契约校验（§12.5.1 的「一次通过率」分子）。"""
        return bool(self.attempts) and self.attempts[0].passed

    @property
    def retries(self) -> int:
        """契约重试次数（首次不算）。"""
        return max(len(self.attempts) - 1, 0)

    @property
    def worst_failure_mode(self) -> FailureMode | None:
        """本次调用出现过的**最严重**失败模式；全程没失败则为 None。

        严重度序按「回灌能不能救回来」排：`entity_hallucination` 最重——它是
        §12.5.1 的硬地板，模型不知道「何超」对应哪个 `person_id`，回灌一百次
        还是在猜；`missing_field` 最轻——指出来基本就补上了。
        """
        seen = {f.mode for a in self.attempts for f in a.failures}
        for mode in _FAILURE_SEVERITY_ORDER:
            if mode in seen:
                return mode
        return None

    def calibration_features(self) -> dict[str, Any]:
        """置信度校准的 Harness 侧特征（v6 §7.3.5，`Z-11`）。

        业务方 2026-08-13 裁定把原方案的「序列 logprob」换成这组特征——本机
        Ollama v0.6.8 不返回任何 logprob 字段（M4-A 实测），而升级推理端会踩
        M0 记过的 CUDA 版本坑。

        **这三个量是免费的**：Harness 本来就要记它们（§12.5.1 的统计口径），
        校准器直接取，不额外发一次请求。M4-B 的 `calibrated_confidence()`
        把它们与 self-consistency 一致率一起喂给逻辑回归。
        """
        worst = self.worst_failure_mode
        return {
            "first_pass": self.first_pass,
            "retries": self.retries,
            "worst_failure_mode": worst.value if worst is not None else "",
            "degraded": self.degraded,
            "llm_calls": self.llm_calls,
        }


#: 失败模式的严重度序（重 → 轻），仅用于 `AgentOutput.worst_failure_mode`。
_FAILURE_SEVERITY_ORDER: Final[tuple[FailureMode, ...]] = (
    FailureMode.ENTITY_HALLUCINATION,
    FailureMode.JSON_MALFORMED,
    FailureMode.ENUM_OUT_OF_RANGE,
    FailureMode.TYPE_ERROR,
    FailureMode.MISSING_FIELD,
)


__all__ = [
    "ALL_COMPONENTS",
    "COMPONENT_LABELS",
    "AgentOutput",
    "AgentSpec",
    "AttemptRecord",
    "ComponentName",
    "FailureMode",
    "ToolHandler",
    "ToolResult",
    "ToolSpec",
    "ValidatedCall",
    "ValidationFailure",
]
