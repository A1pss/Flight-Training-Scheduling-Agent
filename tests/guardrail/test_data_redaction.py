"""数据脱敏（v6 §11.5「数据脱敏」）。

> 日志中人员身份信息按配置脱敏；导出文件按角色控制字段可见性

## 业务方 2026-08-18 的裁定：**只脱敏日志，导出不分角色**

v6 §11.5 那一行的后半句（「导出文件按角色控制字段可见性」）**没有定义哪些字段
对哪个角色不可见**，属于铁律 5 的「设计方案没定义就停下来问」。业务方选定的是
**只脱敏日志、导出不分角色**：

> 所有能调到 `/export` 的角色（viewer 及以上）拿到完全相同的 xlsx。

理由是 Excel 是给人执行的作业单——排班员往下都要拿着表去调度真人，姓名是必需的；
而查看者已经通过 RBAC 被限制在只读。**这条裁定被本文件钉住**（
`test_export_is_identical_for_every_role`）：哪天有人给导出加了按角色裁字段的
逻辑，这里会红，提醒先去改 v6 §11.5 而不是直接改代码。

## 日志脱敏的两条路径

| 路径 | 例子 |
|---|---|
| **按键名** | `person_id` / `name` / `crew` / `instructor` … 整个值替换 |
| **按文本模式** | 自由文本里出现的 `P\\d+`（**不限位数**，`Z-4`） |
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from backend.core.config import Settings
from backend.core.logging import (
    bind_trace_id,
    clear_trace_id,
    configure_logging,
    get_logger,
    make_person_redactor,
)

pytestmark = pytest.mark.guardrail

PLACEHOLDER = "***"


def _redact(event: dict[str, Any]) -> dict[str, Any]:
    return dict(make_person_redactor(PLACEHOLDER)(None, "info", event))


# ═════════════════════════════════════════════════════════════════════
# ① 按键名脱敏
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "key", ["person_id", "name", "person_name", "crew", "instructor", "student", "operator"]
)
def test_person_keys_are_redacted(key: str) -> None:
    assert _redact({key: "何超"})[key] == PLACEHOLDER


def test_key_matching_is_case_insensitive() -> None:
    assert _redact({"Person_ID": "P05"})["Person_ID"] == PLACEHOLDER


def test_nested_person_values_are_redacted() -> None:
    """列表与字典里的姓名也要脱敏 —— 机组是个列表，那才是最常出现的形态。"""
    out = _redact({"crew": [{"name": "何超", "role": "学员"}, {"name": "张勇"}]})
    assert PLACEHOLDER in json.dumps(out, ensure_ascii=False)
    assert "何超" not in json.dumps(out, ensure_ascii=False)
    assert "张勇" not in json.dumps(out, ensure_ascii=False)


def test_non_person_keys_are_left_alone() -> None:
    """**不能把整条日志糊掉** —— 脱敏过头的日志与没有日志是一回事。"""
    out = _redact({"snapshot_id": "snap_abc", "sorties": 14, "week": "2026-W02"})
    assert out == {"snapshot_id": "snap_abc", "sorties": 14, "week": "2026-W02"}


# ═════════════════════════════════════════════════════════════════════
# ② 按文本模式脱敏（Pnn 不限位数）
# ═════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("pid", ["P01", "P05", "P99", "P100", "P1234"])
def test_person_ids_in_free_text_are_redacted(pid: str) -> None:
    """★ `Z-4`：编号**只固定前缀、不限位数**。

    原实现写的是 `\\bP\\d{2}\\b`，只盖得住 `P01`~`P99` —— 把 v6 §1.3 的基准
    数据集规模当成了系统上限。用户传 120 人的花名册时 `P100` 往后**静默不脱敏**。
    """
    out = _redact({"event": f"{pid} 的 A-1 架次已排入"})
    assert pid not in out["event"]
    assert PLACEHOLDER in out["event"]


def test_similar_looking_tokens_are_not_over_redacted() -> None:
    """不能把 `PDF` / `RWY-1` / `missionA-1` 这类误伤成占位符。"""
    out = _redact({"event": "从 PDF 抽取 missionA-1，跑道 RWY-1，机号 AC10"})
    assert out["event"] == "从 PDF 抽取 missionA-1，跑道 RWY-1，机号 AC10"


def test_redaction_can_be_turned_off_by_config() -> None:
    """脱敏是**可配置**的（v6 §11.5「按配置脱敏」）：内网排障时可以临时关掉。"""
    settings = Settings(_env_file=None, LOG_REDACT_PERSON=False)  # type: ignore[call-arg]
    assert settings.LOG_REDACT_PERSON is False
    assert Settings(_env_file=None).LOG_REDACT_PERSON is True, "默认必须是开着的"


def test_placeholder_is_configurable() -> None:
    assert make_person_redactor("[隐藏]")(None, "info", {"name": "何超"})["name"] == "[隐藏]"


# ═════════════════════════════════════════════════════════════════════
# ③ 端到端：真的走一遍日志管线
# ═════════════════════════════════════════════════════════════════════
def test_configured_logger_redacts_end_to_end(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(Settings(_env_file=None, LOG_FORMAT="json", LOG_REDACT_PERSON=True))  # type: ignore[call-arg]
    bind_trace_id("trace-redact")
    try:
        get_logger(__name__).info("架次已排入", person_id="P05", name="何超", sorties=14)
    finally:
        clear_trace_id()
        logging.shutdown()
    captured = capsys.readouterr().out
    assert "何超" not in captured
    assert "P05" not in captured
    assert PLACEHOLDER in captured
    assert '"sorties": 14' in captured, "业务量不该被脱敏 —— 那会让日志失去价值"
    assert "trace-redact" in captured


def test_business_data_is_never_redacted_only_logs_are() -> None:
    """★ 脱敏**只作用于日志**，不影响业务数据。

    排班结果里的姓名必须原样保留，否则 Excel 就没法看了 —— 那张表是给人
    拿去调度真人的作业单。
    """
    from backend.schemas import CrewMember

    member = CrewMember(person_id="P05", name="何超", role="学员")
    assert member.name == "何超"
    # 同一个对象进日志才脱敏
    assert _redact({"crew": [member.model_dump()]})["crew"][0]["name"] == PLACEHOLDER


# ═════════════════════════════════════════════════════════════════════
# ④ 导出不分角色（业务方 2026-08-18 裁定）
# ═════════════════════════════════════════════════════════════════════
def test_export_endpoint_has_no_role_dependent_field_filtering() -> None:
    """裁定的执行性表达：导出路径里**不存在**按角色裁字段的分支。

    这条是「规格裁定被写死在测试里」的一个例子：将来若业务方改主意要分级导出，
    这里会红，提醒先改 v6 §11.5 与本注释，而不是悄悄加一段 if。
    """
    from backend.core.config import PROJECT_ROOT

    source = (PROJECT_ROOT / "backend" / "api" / "routers" / "schedule.py").read_text(
        encoding="utf-8"
    )
    export_section = source[source.index('@router.get(\n    "/schedule/{trace_id}/export"') :]
    assert "principal.role" not in export_section, (
        "导出按角色裁字段了 —— 业务方 2026-08-18 裁定的是「只脱敏日志，导出不分角色」，"
        "要改先改 v6 §11.5"
    )
    assert 'require_role(principal, "viewer"' in export_section
