"""把 `tool_calls_200` 的一条场景渲染成给模型的提示词（v6 §12.5.1 的缺口）。

## 为什么需要这一层

§12.5.1 定义了指标（「首次输出即通过 Pydantic 契约校验的比例」）与方法
（「200 条工具调用场景 × 三种模型配置 × 3 轮」），**但没定义「场景怎么变成
提示词」**。数据集里的 `prompt_context` 是构造期的自述句
（`planner 组件需要「评估相对基线方案的影响面」，第 1 个变体`），
里面**没有 `expected_params` 的任何一个取值** —— 直接拿它当用户消息，
模型只能凭空编一组参数，于是：

- 一次通过率**偏高**（随便编一组合法参数就算过）；
- 参数对不对**测不出来**（没有可比的基准）。

所以本模块把 `expected_params` 渲染成一段**中文任务陈述**：值出现在提示词里，
字段名不出现。模型要做的是「把业务语义映射到工具契约的字段上」——
那正是 §15.2 样本配比里「工具调用（参数正确性）」要练的能力。

> **业务方 2026-08-20 裁定**：按本渲染口径跑，主指标仍是 §12.5.1 的**契约口径**
> 一次通过率，另报**参数精确匹配率**作诊断，**后者不作准入门禁**。

## 字段角色短语从哪来

优先取字段自己的 `description`（工具 schema 里本来就会连同字段一起交给模型，
所以这不构成额外泄漏）；没有 description 的 32 个字段在 `FIELD_ROLES` 里逐条
补中文角色短语。**补的是业务角色，不是字段名** —— 写成「changed_aircraft：AC84」
就等于把答案抄给模型了。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from backend.harness.tools import TOOL_CATALOG

#: 没有 `description` 的字段的中文角色短语（`工具.字段` → 短语）。
#:
#: **这里刻意不写字段名**。渲染出来的是「有变动的飞机：AC84」而不是
#: 「changed_aircraft：AC84」——后者把映射这一步直接送给模型，
#: 一次通过率会虚高，而 §15.2 要练的恰恰是这一步。
FIELD_ROLES: Final[dict[str, str]] = {
    "escalate.reason": "升级原因",
    "escalate.severity": "严重程度",
    "assess_disruption.changed_persons": "有变动的人员",
    "assess_disruption.changed_aircraft": "有变动的飞机",
    "propose_solve_intent.intent": "本次求解意图",
    "classify_doc.filename": "文件名",
    "diff_snapshot.base_snapshot_id": "作为基准的那个快照",
    "diff_snapshot.new_snapshot_id": "要比对过去的新快照",
    "propose_change.entity_kind": "要改的是哪一类实体",
    "propose_change.entity_id": "要改的那个实体的编号",
    "propose_change.field": "要改哪一个字段",
    "propose_change.old_value": "原值",
    "propose_change.new_value": "改成什么",
    "propose_change.reason": "为什么要改",
    "sql_query.limit": "最多返回多少行",
    "vector_search.query": "检索问题",
    "vector_search.top_k": "取前几条",
    "bm25_search.query": "检索问题",
    "bm25_search.top_k": "取前几条",
    "rrf_fuse.top_k": "融合后取前几条",
    "rerank.query": "重排所依据的问题",
    "rerank.top_k": "重排后取前几条",
    "memory.search.query": "要回忆的问题",
    "memory.search.top_k": "取前几条",
    "memory.write.kind": "记忆类型",
    "memory.write.key": "记忆的键",
    "memory.write.content": "要记住的内容",
    "memory.write.valid_from": "从哪天起生效",
    "min_conflict_set.scope_persons": "限定在哪些人身上找冲突",
    "rank_relaxations.prefer": "排序时更看重什么",
    "render_workbook.plan_id": "方案编号",
    "compose_report.plan_id": "方案编号",
}

#: 任务陈述的固定尾句。**不提「工具」的具体名字**，选哪个工具由模型判断。
_INSTRUCTION: Final[str] = "请调用最合适的工具完成这件事。参数必须严格符合该工具的契约。"


def role_of(tool: str, field: str) -> str:
    """字段的中文角色短语。

    三级回退：字段自己的 `description` → `FIELD_ROLES` → 字段名本身。
    最后一级只会在工具目录新增字段而这里忘了补的时候走到，
    此时渲染出的提示词会**露出字段名**——`test_every_field_has_a_role`
    会先一步把它拦下来。
    """
    spec = TOOL_CATALOG.get(tool)
    if spec is not None:
        info = spec.params_model.model_fields.get(field)
        if info is not None and info.description:
            # description 里常带一句给模型的附注（「；首轮排班留空」「，如「73 号机」」），
            # 那是 schema 该说的话，不是任务陈述该说的话 —— 截到第一个分号为止。
            # 附注本身照常随 schema 一起交给模型，这里截掉不构成信息损失。
            return info.description.split("；")[0].strip()
    return FIELD_ROLES.get(f"{tool}.{field}", field)


def _render_value(value: Any, indent: int = 0) -> str:
    """把一个参数取值渲染成人读得懂的形态。

    列表拍平成顿号分隔；嵌套对象（`propose_solve_intent.intent` 那个完整的
    `SolveIntent`）逐层缩进展开——压成一行 JSON 会让模型照抄 JSON 而不是
    按契约重建对象，那测的就不是同一件事了。
    """
    pad = "  " * indent
    if isinstance(value, Mapping):
        lines = []
        for key, sub in value.items():
            rendered = _render_value(sub, indent + 1)
            joiner = "\n" if "\n" in rendered else " "
            lines.append(f"{pad}  · {key}：{joiner}{rendered}")
        return "\n".join(lines)
    if isinstance(value, (list, tuple)):
        if not value:
            return "（空）"
        if any(isinstance(v, (Mapping, list, tuple)) for v in value):
            return "\n".join(_render_value(v, indent + 1) for v in value)
        return "、".join(str(v) for v in value)
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return "（未给）"
    return str(value)


def render_task(
    tool: str,
    expected_params: Mapping[str, Any],
    *,
    task: str = "",
) -> str:
    """渲染一条场景的用户消息。

    形态刻意固定（任务一行 + 已知条件逐条 + 一句指令），三种提示词配置共用
    同一份渲染——**配置之间只差系统提示词**，任务侧完全一致，
    否则三行数字不可比。
    """
    heading = task or (TOOL_CATALOG[tool].description if tool in TOOL_CATALOG else tool)
    lines = [f"【任务】{heading}"]
    if expected_params:
        lines.append("【已知条件】")
        for field, value in expected_params.items():
            rendered = _render_value(value, 0)
            joiner = "\n" if "\n" in rendered else ""
            lines.append(f"- {role_of(tool, field)}：{joiner}{rendered}")
    else:
        lines.append("【已知条件】（无额外条件）")
    lines.append("")
    lines.append(_INSTRUCTION)
    return "\n".join(lines)


#: 两种场景渲染口径。**它们测的是两件不同的事，数字不可互相替代。**
#:
#: | 口径 | 用户消息 | 模型拿得到参数取值吗 | 测什么 |
#: |---|---|---|---|
#: | `task` | 任务陈述（本模块渲染） | **拿得到** | 「业务语义 → 契约字段」的映射 |
#: | `context` | 数据集的 `prompt_context` 原文 | **拿不到** | 「没有已知条件时能不能自己产出合法参数」 |
#:
#: 业务方 2026-08-20 先定了 `task`，2026-08-21 追加 `context` —— 因为 `task` 下
#: 一次通过率饱和（失败模式分布表全空），而 §15.2 ⑥ 的难负例挖掘要的正是那张表。
#: `context` 是**更难的那一侧**：`expected_params` 里的取值一个都不出现在提示词里，
#: 模型必须自己编，于是 `entity_hallucination` / `missing_field` 才会真的显形。
RenderingName = Literal["task", "context"]

ALL_RENDERINGS: Final[tuple[RenderingName, ...]] = ("task", "context")


def render_item(item: Mapping[str, Any], rendering: RenderingName = "task") -> str:
    """从数据集条目渲染用户消息。

    `task` 用 `tool` + `expected_params` 渲染任务陈述；`context` 原样取数据集的
    `prompt_context`。**两者只差这一条消息** —— 系统提示词、工具表、Harness
    全都一样，否则两组数字之间的差就不只是「给不给已知条件」了。
    """
    if rendering == "context":
        return str(item["prompt_context"])
    return render_task(str(item["tool"]), item.get("expected_params") or {})


def describe_rendering(rendering: RenderingName) -> str:
    """渲染口径的一句话说明，进结果文件与报告的表头。"""
    return {
        "task": "口径 A：参数渲染成中文任务陈述，模型拿得到每个取值",
        "context": "口径 B：原样用数据集的 prompt_context，模型拿不到任何取值",
    }[rendering]


def params_match(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    """参数是否精确匹配（诊断指标，**不作准入门禁**）。

    比的是**归一化之后的值**：键序无关，数字与字符串按 JSON 规范形态比。
    `expected` 里没给的可选字段，`actual` 里若填了默认值也算不匹配——
    那说明模型在编内容，而不是照着已知条件填。
    """
    return _canonical(expected) == _canonical(actual)


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def missing_roles() -> tuple[str, ...]:
    """工具目录里**既没有 description、也没在 `FIELD_ROLES` 里**的字段。

    非空即意味着渲染会露出字段名（把答案抄给模型），`tests/` 里据此断言。
    """
    gaps: list[str] = []
    for name, spec in TOOL_CATALOG.items():
        for field, info in spec.params_model.model_fields.items():
            if info.description:
                continue
            if f"{name}.{field}" not in FIELD_ROLES:
                gaps.append(f"{name}.{field}")
    return tuple(sorted(gaps))


def render_batch(
    items: Sequence[Mapping[str, Any]], rendering: RenderingName = "task"
) -> tuple[str, ...]:
    """批量渲染，顺序与入参一致（可复现，铁律 9）。"""
    return tuple(render_item(item, rendering) for item in items)


__all__ = [
    "ALL_RENDERINGS",
    "FIELD_ROLES",
    "RenderingName",
    "describe_rendering",
    "missing_roles",
    "params_match",
    "render_batch",
    "render_item",
    "render_task",
    "role_of",
]
