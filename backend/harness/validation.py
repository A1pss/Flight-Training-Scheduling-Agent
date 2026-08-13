"""工具契约校验与失败模式分类（v6 §7.7.1 第 1 行 / §12.5.1）。

两件事：

**一、校验。** 模型返回的 tool call 先过 Pydantic 契约，失败则把「哪个字段、
期望什么类型、实际收到什么」回灌重试 ≤2 次。笼统的「参数错误」对 14B 没有
纠正价值——它需要知道自己错在哪个字段上。

**二、分类。** 每次失败归入 `FailureMode` 五类之一。这张分布表是 v6 §15.2
难负例挖掘的直接输入，也是 §12.5.1「硬地板 x」的唯一观测口：
`entity_hallucination` 这类失败重试多少轮都救不回来（模型不知道「何超」对应
哪个 `person_id`，回灌一百次它还是在猜），它占比多少就直接决定最终通过率的
天花板。**所以分类不能含糊**：把实体编造归到 `type_error` 里，W13 就再也看不出
97% 能不能上调回 98%。

分类判序（先到先得）：

| # | 判据 | 归类 |
|---|---|---|
| 1 | 输出/参数不是合法 JSON | `json_malformed` |
| 2 | 工具名不在本次工具表里 | `enum_out_of_range` |
| 3 | 必填字段缺失、或必填字符串给了空串 | `missing_field` |
| 4 | 出错字段带 `x-entity` 标注 | `entity_hallucination` |
| 5 | 字面量/枚举取值越界、数值越界 | `enum_out_of_range` |
| 6 | 其余（类型不符、多余字段、格式不符、自定义校验失败） | `type_error` |
| 7 | 校验通过但实体编号在快照里不存在 | `entity_hallucination` |

第 3 条把「必填字符串给了空串」判成 `missing_field` 而不是 `type_error`：
`person_id=""` 在语义上就是没给，判成类型错会让「模型漏填」这个最常见的失败
被稀释到别的桶里。第 7 条是 `entity_hallucination` 的**主战场**——格式完全
合法、库里没有这个人，只有比对快照才认得出来。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Protocol, cast

from pydantic import BaseModel, ValidationError

from backend.harness.tools import ENTITY_SCHEMA_KEY, TOOL_CATALOG
from backend.harness.types import (
    FailureMode,
    ToolSpec,
    ValidatedCall,
    ValidationFailure,
)
from backend.llm.types import RawToolCall
from backend.schemas.common import EntityKind

#: pydantic 错误类型 → 失败模式。未列出的一律 `type_error`。
_ERROR_TYPE_MAP: Final[dict[str, FailureMode]] = {
    "missing": FailureMode.MISSING_FIELD,
    "string_too_short": FailureMode.MISSING_FIELD,
    "literal_error": FailureMode.ENUM_OUT_OF_RANGE,
    "enum": FailureMode.ENUM_OUT_OF_RANGE,
    "greater_than": FailureMode.ENUM_OUT_OF_RANGE,
    "greater_than_equal": FailureMode.ENUM_OUT_OF_RANGE,
    "less_than": FailureMode.ENUM_OUT_OF_RANGE,
    "less_than_equal": FailureMode.ENUM_OUT_OF_RANGE,
    "multiple_of": FailureMode.ENUM_OUT_OF_RANGE,
    "json_invalid": FailureMode.JSON_MALFORMED,
    "json_type": FailureMode.JSON_MALFORMED,
}


# ─────────────────────────────────────────────────────────────────────
# 实体索引
# ─────────────────────────────────────────────────────────────────────


class EntityIndex(Protocol):
    """快照里真实存在的实体编号。

    Harness 不自己查库——它拿到的是一份「这个快照有哪些人/机/课目」的只读视图。
    这样单测能用手工构造的索引，生产端由 M4-B 从快照装配，两边同一个接口。
    """

    def known(self, kind: EntityKind) -> frozenset[str]:
        """该类实体的全部合法编号。**空集表示「这一类没有索引」**，跳过成员校验。"""
        ...


class StaticEntityIndex:
    """内存实体索引。"""

    def __init__(self, entities: dict[EntityKind, Iterable[str]] | None = None) -> None:
        self._entities: dict[EntityKind, frozenset[str]] = {
            kind: frozenset(values) for kind, values in (entities or {}).items()
        }

    def known(self, kind: EntityKind) -> frozenset[str]:
        return self._entities.get(kind, frozenset())


#: 没有索引时的默认值：只做格式校验，不做成员校验。
EMPTY_ENTITY_INDEX: Final[StaticEntityIndex] = StaticEntityIndex()


def iter_entity_fields(
    model: type[BaseModel], prefix: str = ""
) -> tuple[tuple[str, EntityKind], ...]:
    """递归收集模型里带 `x-entity` 标注的字段路径。

    嵌套的既有契约（如 `SolveIntent`）没有这个标注，它们靠自身的 `pattern`
    约束把关——这里不替它们猜哪个字段是实体，猜错比不猜更糟。
    """
    found: list[tuple[str, EntityKind]] = []
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        extra = field.json_schema_extra
        if isinstance(extra, dict):
            kind = extra.get(ENTITY_SCHEMA_KEY)
            if isinstance(kind, str):
                found.append((path, cast(EntityKind, kind)))
                continue
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            found.extend(iter_entity_fields(annotation, prefix=f"{path}."))
    return tuple(found)


# ─────────────────────────────────────────────────────────────────────
# 校验器
# ─────────────────────────────────────────────────────────────────────


class ToolCallValidator:
    """把一次 `RawToolCall` 变成 `ValidatedCall`，或变成一条带归因的失败。"""

    def __init__(self, entity_index: EntityIndex | None = None) -> None:
        self._entities = entity_index or EMPTY_ENTITY_INDEX

    def validate(
        self, call: RawToolCall, allowed_tools: Sequence[str]
    ) -> tuple[ValidatedCall | None, ValidationFailure | None]:
        """校验单个工具调用。成功返回 `(ValidatedCall, None)`，失败返回 `(None, 失败)`。"""
        # ① 工具名
        if not call.name:
            return None, ValidationFailure(
                mode=FailureMode.ENUM_OUT_OF_RANGE,
                field_path="name",
                expected=f"下列之一：{sorted(allowed_tools)}",
                actual="（空）",
                message="没给工具名",
            )
        if call.name not in allowed_tools or call.name not in TOOL_CATALOG:
            return None, ValidationFailure(
                mode=FailureMode.ENUM_OUT_OF_RANGE,
                tool=call.name,
                field_path="name",
                expected=f"下列之一：{sorted(allowed_tools)}",
                actual=call.name,
                message="工具名不在本次可用工具表里",
            )
        spec = TOOL_CATALOG[call.name]

        # ② 参数 JSON
        arguments, decode_error = call.decoded_arguments()
        if decode_error is not None or arguments is None:
            return None, ValidationFailure(
                mode=FailureMode.JSON_MALFORMED,
                tool=call.name,
                field_path="arguments",
                expected="JSON 对象",
                actual=_preview(call.arguments),
                message=decode_error or "参数无法解析为 JSON 对象",
            )

        # ③ Pydantic 契约
        try:
            params = spec.params_model.model_validate(arguments)
        except ValidationError as exc:
            return None, self._classify(spec, exc)

        # ④ 实体成员校验（格式合法但库里没有 → 编造）
        data = params.model_dump(mode="json")
        entity_failure = self._check_entities(spec, data)
        if entity_failure is not None:
            return None, entity_failure

        return ValidatedCall(name=call.name, arguments=data), None

    # ── 分类 ─────────────────────────────────────────────────────────
    def _classify(self, spec: ToolSpec, exc: ValidationError) -> ValidationFailure:
        """取**第一条**错误做归因。

        为什么只取第一条：回灌给模型的信息越聚焦纠正率越高，一次甩十条错误
        它多半只改第一条还改错。剩下的错误在下一轮重试里自然会再暴露。
        """
        error = exc.errors()[0]
        path = ".".join(str(p) for p in error["loc"])
        err_type = str(error["type"])
        entity_fields = dict(iter_entity_fields(spec.params_model))

        mode = _ERROR_TYPE_MAP.get(err_type, FailureMode.TYPE_ERROR)
        # 实体字段上的任何格式失败都是编造 —— 模型多半填了个人名
        base_path = path.split(".")[0]
        if (path in entity_fields or base_path in entity_fields) and mode not in (
            FailureMode.MISSING_FIELD,
        ):
            mode = FailureMode.ENTITY_HALLUCINATION

        return ValidationFailure(
            mode=mode,
            tool=spec.name,
            field_path=path,
            expected=_expected_of(spec, path, error),
            actual=_preview(error.get("input")),
            message=str(error["msg"]),
        )

    def _check_entities(self, spec: ToolSpec, data: dict[str, Any]) -> ValidationFailure | None:
        for path, kind in iter_entity_fields(spec.params_model):
            known = self._entities.known(kind)
            if not known:
                continue  # 这一类没有索引：只做了格式校验，不臆断成员关系
            for value in _values_at(data, path):
                if value not in known:
                    return ValidationFailure(
                        mode=FailureMode.ENTITY_HALLUCINATION,
                        tool=spec.name,
                        field_path=path,
                        expected=f"当前快照中存在的 {kind} 编号（共 {len(known)} 个）",
                        actual=str(value),
                        message="编号格式合法，但当前快照里没有这个实体",
                    )
        return None


# ─────────────────────────────────────────────────────────────────────
# 回灌
# ─────────────────────────────────────────────────────────────────────


def build_error_feedback(
    failures: Sequence[ValidationFailure],
    tool_schemas: Sequence[dict[str, Any]] | None = None,
) -> str:
    """构造回灌给模型的纠错消息（v6 §7.7.1 第 1 行）。

    必须同时给出**错在哪**与**该怎么写**：只说「参数错误」等于让模型重猜一次，
    实测上这种重试的纠正率接近 0。
    """
    lines = ["上一次工具调用没有通过契约校验，请**只**修正下面指出的问题后重新给出调用："]
    lines.extend(f"{i}. {f.as_feedback_line()}" for i, f in enumerate(failures, start=1))
    if any(f.mode is FailureMode.ENTITY_HALLUCINATION for f in failures):
        lines.append(
            "注意：实体编号不要凭印象写。先用 resolve_person / resolve_aircraft / "
            "resolve_week 把名称解析成编号，再填进参数。"
        )
    if tool_schemas:
        names = ", ".join(str(s.get("name", "")) for s in tool_schemas)
        lines.append(f"本次可用工具：{names}")
    return "\n".join(lines)


def _values_at(data: dict[str, Any], path: str) -> tuple[Any, ...]:
    """按 `a.b` 路径取值，list 展开为多个值。"""
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return ()
        current = current[part]
    if current is None:
        return ()
    if isinstance(current, list):
        return tuple(v for v in current if isinstance(v, str))
    return (current,) if isinstance(current, str) else ()


def _expected_of(spec: ToolSpec, path: str, error: Mapping[str, Any]) -> str:
    """给「期望什么」一句人话。优先用 JSON Schema 里的类型/枚举描述。"""
    schema = spec.json_schema()
    props = schema.get("properties", {})
    field_schema = props.get(path.split(".")[0], {})
    bits: list[str] = []
    if "type" in field_schema:
        bits.append(str(field_schema["type"]))
    if "enum" in field_schema:
        bits.append(f"取值 ∈ {field_schema['enum']}")
    if "pattern" in field_schema:
        bits.append(f"匹配 {field_schema['pattern']}")

    # pydantic 的 ctx.expected 常常与上面的 enum 是同一句话的两种写法。
    # 回灌消息越短越聚焦纠正率越高，重复的那份就别往里塞了。
    ctx = error.get("ctx")
    if isinstance(ctx, dict) and "expected" in ctx and "enum" not in field_schema:
        bits.append(str(ctx["expected"]))
    return "、".join(bits)


def _preview(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = [
    "EMPTY_ENTITY_INDEX",
    "EntityIndex",
    "StaticEntityIndex",
    "ToolCallValidator",
    "build_error_feedback",
    "iter_entity_fields",
]
