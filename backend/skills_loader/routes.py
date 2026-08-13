"""Skill 路由：确定性，不消耗 LLM 调用（v6 §7.8.3）。

```python
SKILL_ROUTES = {
    ("Ingest", "doc_type=personnel"): "doc-parsing/personnel",
    ("Ingest", "doc_type=aircraft"):  "doc-parsing/aircraft",
    ("Ingest", "doc_type=exception"): "doc-parsing/exception",
    ("Diagnosis", "*"):               "rule-interpretation",
    ("Diagnosis", "has_conflict"):    "relaxation-playbook",
    ("Explain", "*"):                 ["rule-interpretation", "report-writing"],
}
```

> 文档类型已由分类器判定、冲突状态已在黑板上，路由完全可由规则决定。
> **不引入「让 LLM 选 skill」这一步**——多一次 LLM 调用换一个规则可判定的
> 选择，不划算，且引入了新的不确定性。

## 条件的语义

`"*"` 是无条件命中；其余条件是**上下文里必须为真的键**。调用方给一组条件
（如 `{"doc_type=personnel"}`），路由取「无条件项 + 条件命中项」的并集，
**按 `SKILL_ROUTES` 的书写顺序**去重——顺序稳定，`render_skills` 拼出来的
上下文才稳定，重放才对得上。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Final

#: 组件名（v6 §7.8.3 用的是首字母大写的口语名，与 §7.7.2 的矩阵列名对应）
SkillConsumer = str

#: 无条件命中的通配符
ANY: Final[str] = "*"

#: v6 §7.8.3 的路由表，逐条照抄（含顺序）。
SKILL_ROUTES: Final[tuple[tuple[tuple[SkillConsumer, str], tuple[str, ...]], ...]] = (
    (("Ingest", "doc_type=personnel"), ("doc-parsing/personnel",)),
    (("Ingest", "doc_type=aircraft"), ("doc-parsing/aircraft",)),
    (("Ingest", "doc_type=mission"), ("doc-parsing/mission",)),
    (("Ingest", "doc_type=rules"), ("doc-parsing/rules",)),
    (("Ingest", "doc_type=exception"), ("doc-parsing/exception",)),
    (("Diagnosis", ANY), ("rule-interpretation",)),
    (("Diagnosis", "has_conflict"), ("relaxation-playbook",)),
    (("Explain", ANY), ("rule-interpretation", "report-writing")),
)

#: Harness 组件名（§7.7.2 矩阵的列）→ 路由表里的口语名。
#: 两套名字并存是 v6 的原样，这里做一次显式映射而不是改任何一边——
#: 改 §7.7.2 的列名会牵动 ACL 矩阵，改 §7.8.3 的键会牵动这张路由表。
COMPONENT_TO_CONSUMER: Final[Mapping[str, SkillConsumer]] = {
    "extract": "Ingest",
    "diagnosis": "Diagnosis",
    "explain": "Explain",
}

#: 明确**不加载任何 skill** 的组件。
#:
#: `route` 与 `planner` 不在路由表里，这是 v6 §7.8.3 的原样，也是有道理的：
#: 意图路由与求解规划的产物直接决定排哪些人、用哪档松弛，离「排什么班」最近。
#: 知识层不进这两个组件的上下文，红线就少一处可能被绕过的地方。
NO_SKILL_COMPONENTS: Final[frozenset[str]] = frozenset({"route", "planner", "knowledge"})


def route_skills(consumer: SkillConsumer, conditions: Iterable[str] = ()) -> tuple[str, ...]:
    """按消费方与条件选出要加载的 skill 名，顺序稳定、结果确定。"""
    active = set(conditions)
    out: list[str] = []
    for (routed_consumer, condition), names in SKILL_ROUTES:
        if routed_consumer != consumer:
            continue
        if condition != ANY and condition not in active:
            continue
        for name in names:
            if name not in out:
                out.append(name)
    return tuple(out)


def route_for_component(component: str, conditions: Iterable[str] = ()) -> tuple[str, ...]:
    """按 Harness 组件名路由。不在映射里的组件返回空元组（不加载任何 skill）。"""
    consumer = COMPONENT_TO_CONSUMER.get(component)
    if consumer is None:
        return ()
    return route_skills(consumer, conditions)


def ingest_conditions(doc_type: str) -> tuple[str, ...]:
    """摄取侧的条件构造器。文档类型由分类器判定，不由模型选。"""
    return (f"doc_type={doc_type}",)


def diagnosis_conditions(*, has_conflict: bool) -> tuple[str, ...]:
    """诊断侧的条件构造器。冲突状态已在黑板上。"""
    return ("has_conflict",) if has_conflict else ()


def all_routed_skills() -> tuple[str, ...]:
    """路由表里出现过的全部 skill 名，供「目录与路由表不漂移」的测试用。"""
    out: list[str] = []
    for _, names in SKILL_ROUTES:
        for name in names:
            if name not in out:
                out.append(name)
    return tuple(sorted(out))


def missing_from_library(available: Sequence[str]) -> tuple[str, ...]:
    """路由表点名、但库里没有的 skill。"""
    have = set(available)
    return tuple(name for name in all_routed_skills() if name not in have)


__all__ = [
    "ANY",
    "COMPONENT_TO_CONSUMER",
    "NO_SKILL_COMPONENTS",
    "SKILL_ROUTES",
    "SkillConsumer",
    "all_routed_skills",
    "diagnosis_conditions",
    "ingest_conditions",
    "missing_from_library",
    "route_for_component",
    "route_skills",
]
