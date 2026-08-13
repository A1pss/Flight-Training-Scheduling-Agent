"""契约校验与五类失败模式（v6 §7.7.1 第 1 行 / §12.5.1）。

五类枚举是**外部口径**：v6 §15.2 的难负例挖掘按它取样，§12.5.1 的「硬地板 x」
只能从 `entity_hallucination` 的占比观测。所以这组用例不只验「校验挡住了」，
还要验**归到了正确的桶里**——归错桶等于把 W13 的判断依据搞乱。
"""

from __future__ import annotations

import pytest

from backend.harness.tools import TOOL_CATALOG
from backend.harness.types import FailureMode
from backend.harness.validation import (
    StaticEntityIndex,
    ToolCallValidator,
    build_error_feedback,
    iter_entity_fields,
)
from backend.llm.types import RawToolCall
from tests.fixtures.harness_fixtures import baseline_entity_index

ROUTE_TOOLS = ("resolve_person", "resolve_week", "ask_user")
KNOWLEDGE_TOOLS = ("prereq_cte", "sql_query", "memory.search")
PLANNER_TOOLS = ("estimate_scope", "check_authority", "propose_solve_intent")


@pytest.fixture
def validator() -> ToolCallValidator:
    return ToolCallValidator(baseline_entity_index())


# ─── 正常路径 ────────────────────────────────────────────────────────


def test_valid_call_passes(validator: ToolCallValidator) -> None:
    call, failure = validator.validate(
        RawToolCall(name="resolve_person", arguments={"surface": "何超"}), ROUTE_TOOLS
    )
    assert failure is None
    assert call is not None
    assert call.arguments == {"surface": "何超"}


def test_arguments_may_arrive_as_json_string(validator: ToolCallValidator) -> None:
    """有的模型把参数写成 JSON 字符串——这是合法形态，不该判失败。"""
    call, failure = validator.validate(
        RawToolCall(name="resolve_person", arguments='{"surface": "何超"}'), ROUTE_TOOLS
    )
    assert failure is None and call is not None


def test_defaults_are_filled_in(validator: ToolCallValidator) -> None:
    call, failure = validator.validate(
        RawToolCall(name="prereq_cte", arguments={"person_id": "P08", "mission_id": "missionB-1"}),
        KNOWLEDGE_TOOLS,
    )
    assert failure is None and call is not None


# ─── 五类失败模式 ────────────────────────────────────────────────────


def test_missing_field(validator: ToolCallValidator) -> None:
    _, failure = validator.validate(RawToolCall(name="resolve_person", arguments={}), ROUTE_TOOLS)
    assert failure is not None
    assert failure.mode is FailureMode.MISSING_FIELD
    assert failure.field_path == "surface"


def test_empty_required_string_counts_as_missing(validator: ToolCallValidator) -> None:
    """`surface=""` 语义上就是没给，判 `missing_field` 而不是类型错。"""
    _, failure = validator.validate(
        RawToolCall(name="resolve_person", arguments={"surface": ""}), ROUTE_TOOLS
    )
    assert failure is not None and failure.mode is FailureMode.MISSING_FIELD


def test_type_error(validator: ToolCallValidator) -> None:
    _, failure = validator.validate(
        RawToolCall(name="memory.search", arguments={"query": "何超", "top_k": "五条"}),
        KNOWLEDGE_TOOLS,
    )
    assert failure is not None
    assert failure.mode is FailureMode.TYPE_ERROR
    assert failure.field_path == "top_k"
    assert "五条" in failure.actual


def test_extra_field_is_a_type_error(validator: ToolCallValidator) -> None:
    _, failure = validator.validate(
        RawToolCall(name="resolve_person", arguments={"surface": "何超", "person_name": "何超"}),
        ROUTE_TOOLS,
    )
    assert failure is not None and failure.mode is FailureMode.TYPE_ERROR


def test_entity_hallucination_when_a_name_is_passed_as_an_id(
    validator: ToolCallValidator,
) -> None:
    """把「何超」填进 `person_id`——这正是 §12.5.1 硬地板的典型样子。"""
    _, failure = validator.validate(
        RawToolCall(name="prereq_cte", arguments={"person_id": "何超", "mission_id": "missionB-1"}),
        KNOWLEDGE_TOOLS,
    )
    assert failure is not None
    assert failure.mode is FailureMode.ENTITY_HALLUCINATION
    assert failure.field_path == "person_id"


def test_entity_hallucination_when_id_is_well_formed_but_unknown(
    validator: ToolCallValidator,
) -> None:
    """格式完全合法、快照里没这个人——只有比对索引才认得出来。"""
    _, failure = validator.validate(
        RawToolCall(name="prereq_cte", arguments={"person_id": "P99", "mission_id": "missionB-1"}),
        KNOWLEDGE_TOOLS,
    )
    assert failure is not None
    assert failure.mode is FailureMode.ENTITY_HALLUCINATION
    assert "P99" in failure.actual


def test_enum_out_of_range(validator: ToolCallValidator) -> None:
    _, failure = validator.validate(
        RawToolCall(name="ask_user", arguments={"question": "周期起点？", "resolution": "随便"}),
        ROUTE_TOOLS,
    )
    assert failure is not None
    assert failure.mode is FailureMode.ENUM_OUT_OF_RANGE


def test_numeric_out_of_range_is_enum_out_of_range(validator: ToolCallValidator) -> None:
    _, failure = validator.validate(
        RawToolCall(
            name="check_authority", arguments={"actor_role": "排班员", "requested_tier": 7}
        ),
        PLANNER_TOOLS,
    )
    assert failure is not None
    assert failure.mode is FailureMode.ENUM_OUT_OF_RANGE
    assert failure.field_path == "requested_tier"


def test_json_malformed(validator: ToolCallValidator) -> None:
    _, failure = validator.validate(
        RawToolCall(name="resolve_person", arguments='{"surface": "何超"'), ROUTE_TOOLS
    )
    assert failure is not None
    assert failure.mode is FailureMode.JSON_MALFORMED


def test_json_array_arguments_is_malformed(validator: ToolCallValidator) -> None:
    _, failure = validator.validate(
        RawToolCall(name="resolve_person", arguments='["何超"]'), ROUTE_TOOLS
    )
    assert failure is not None and failure.mode is FailureMode.JSON_MALFORMED


def test_unknown_tool_name_is_enum_out_of_range(validator: ToolCallValidator) -> None:
    _, failure = validator.validate(
        RawToolCall(name="resolve_pilot", arguments={"surface": "何超"}), ROUTE_TOOLS
    )
    assert failure is not None
    assert failure.mode is FailureMode.ENUM_OUT_OF_RANGE
    assert failure.field_path == "name"


def test_empty_tool_name(validator: ToolCallValidator) -> None:
    _, failure = validator.validate(RawToolCall(arguments={}), ROUTE_TOOLS)
    assert failure is not None and failure.field_path == "name"


def test_five_modes_are_all_reachable(validator: ToolCallValidator) -> None:
    """五类一个不少地都能被触发——枚举里有个到不了的值等于口径缺一块。"""
    cases = [
        RawToolCall(name="resolve_person", arguments={}),  # missing_field
        RawToolCall(name="memory.search", arguments={"query": "x", "top_k": "五"}),  # type_error
        RawToolCall(
            name="prereq_cte", arguments={"person_id": "何超", "mission_id": "missionA-1"}
        ),  # entity_hallucination
        RawToolCall(name="ask_user", arguments={"question": "x", "resolution": "??"}),  # enum
        RawToolCall(name="resolve_person", arguments="{"),  # json_malformed
    ]
    tools = ROUTE_TOOLS + KNOWLEDGE_TOOLS
    modes = {validator.validate(c, tools)[1].mode for c in cases}  # type: ignore[union-attr]
    assert modes == set(FailureMode)


# ─── SQL 只读（§7.7.2 最后一行的参数级强制）─────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM sorties",
        "UPDATE personnel SET name='x'",
        "INSERT INTO plans VALUES (1)",
        "DROP TABLE data_snapshots",
        "SELECT 1; DROP TABLE plans",
        "GRANT ALL ON plans TO PUBLIC",
    ],
)
def test_sql_query_rejects_writes(validator: ToolCallValidator, sql: str) -> None:
    _, failure = validator.validate(
        RawToolCall(name="sql_query", arguments={"sql": sql}), KNOWLEDGE_TOOLS
    )
    assert failure is not None
    assert failure.field_path == "sql"


def test_sql_query_accepts_reads(validator: ToolCallValidator) -> None:
    call, failure = validator.validate(
        RawToolCall(
            name="sql_query", arguments={"sql": "SELECT person_id FROM personnel WHERE role='学员'"}
        ),
        KNOWLEDGE_TOOLS,
    )
    assert failure is None and call is not None


# ─── 无索引时的行为 ──────────────────────────────────────────────────


def test_without_index_only_format_is_checked() -> None:
    """没有实体索引时**不臆断**成员关系：格式合法就放行。"""
    validator = ToolCallValidator(StaticEntityIndex())
    call, failure = validator.validate(
        RawToolCall(name="prereq_cte", arguments={"person_id": "P99", "mission_id": "missionZ-9"}),
        ("prereq_cte",),
    )
    assert failure is None and call is not None


# ─── 回灌消息 ────────────────────────────────────────────────────────


def test_feedback_names_field_expected_and_actual(validator: ToolCallValidator) -> None:
    """v6 §7.7.1：必须给出「哪个字段、期望什么、实际收到什么」。"""
    _, failure = validator.validate(
        RawToolCall(name="prereq_cte", arguments={"person_id": "何超", "mission_id": "missionB-1"}),
        KNOWLEDGE_TOOLS,
    )
    assert failure is not None
    text = build_error_feedback([failure], [{"name": "prereq_cte"}])
    assert "person_id" in text
    assert "何超" in text
    assert "entity_hallucination" in text
    assert "resolve_person" in text  # 编造实体时要提示先解析编号
    assert "prereq_cte" in text


def test_feedback_lists_available_tools(validator: ToolCallValidator) -> None:
    _, failure = validator.validate(RawToolCall(name="resolve_person", arguments={}), ROUTE_TOOLS)
    assert failure is not None
    text = build_error_feedback([failure], [{"name": t} for t in ROUTE_TOOLS])
    assert "ask_user" in text


# ─── 实体字段发现 ────────────────────────────────────────────────────


def test_iter_entity_fields_finds_annotated_paths() -> None:
    assert dict(iter_entity_fields(TOOL_CATALOG["prereq_cte"].params_model)) == {
        "person_id": "person",
        "mission_id": "mission",
    }


def test_iter_entity_fields_recurses_into_nested_models() -> None:
    """嵌套模型也走一遍——今天 `SolveIntent` 没标注，将来标了就自动生效。"""
    paths = dict(iter_entity_fields(TOOL_CATALOG["propose_solve_intent"].params_model))
    assert paths["iso_week"] == "week"
