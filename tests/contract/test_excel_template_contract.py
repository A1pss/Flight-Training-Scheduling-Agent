"""契约测试：schemathesis × Excel 模板契约（v6 §12 测试矩阵「契约测试」行）。

schemathesis 按 `SchedulePlan` 的 JSON Schema 生成载荷去打 `POST /reports/workbook`：

- **不合契约的载荷**必须被挡在 422（渲染器一行都不该跑到）；
- **合契约的载荷**必须渲染出四张表、顺序固定，且不产生 5xx；
- 语义自洽（星期与日期对得上）的载荷，回读必须一条不差地还原全部架次。

最后一条为什么要加「语义自洽」这个前提：`weekday` 与 `date` 对不上属于**约束1
的违规**，不是契约违规 —— Pydantic 层放行是对的（那是闸门1 的活）。这类方案
写出来仍然是一张合法的表，只是回读时按星期反推的日期与源对象不符。契约测试
不该替校验器判这件事，所以只在自洽样本上断言往返相等。
"""

from __future__ import annotations

import pytest
import schemathesis
from hypothesis import HealthCheck, settings
from schemathesis import Case

from backend.validator.schema import WEEKDAY_ORDER
from backend.validator.workbook import SHEET_ORDER
from tests.contract.app import create_app

app = create_app()
schema = schemathesis.openapi.from_asgi("/openapi.json", app)

#: 契约测试跑的是**渲染真表**（每个用例都要写一次 xlsx），比纯 JSON 的 API 慢得多，
#: 所以例数压到 30、超时放宽 —— 覆盖面靠 schema 的结构化生成，不靠海量随机。
#: 契约层的合法拒绝：请求体不是合法 JSON 400、载荷不合契约 422、
#: 方法不对 405、媒体类型不对 415。**5xx 一个都不许有** —— 那是实现漏了边界。
ACCEPTABLE_REJECTIONS = frozenset({400, 405, 415, 422})

CONTRACT_SETTINGS = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


@schema.parametrize()
@CONTRACT_SETTINGS
def test_excel_contract_never_breaks(case: Case) -> None:
    response = case.call()
    assert response.status_code < 500, (
        f"{case.operation.label} 上出现服务端错误：{response.text[:400]}"
    )

    if response.status_code != 200:
        # schemathesis 的反向用例会顺带改方法与 content-type，405/415 是**正确**的拒绝
        assert response.status_code in ACCEPTABLE_REJECTIONS, f"意外状态码 {response.status_code}"
        return

    payload = response.json()
    if case.operation.label.endswith("/reports/template-contract"):
        assert payload["sheets"] == list(SHEET_ORDER)
        assert len(payload["blocks"]) == 7
        return

    assert payload["sheets"] == list(SHEET_ORDER)
    body = case.body if isinstance(case.body, dict) else {}
    sorties = body.get("sorties", [])
    self_consistent = all(
        WEEKDAY_ORDER.index(s["weekday"]) == (_days_from_monday(s["date"], body["week_start"]))
        for s in sorties
    )
    if self_consistent:
        assert payload["readback_errors"] == []
        assert payload["sorties"] == len(sorties)


def _days_from_monday(day: str, week_start: str) -> int:
    from datetime import date

    return (date.fromisoformat(day) - date.fromisoformat(week_start)).days


@pytest.mark.parametrize("operation", ["GET /reports/template-contract", "POST /reports/workbook"])
def test_contract_operations_are_documented(operation: str) -> None:
    """契约本身要在 OpenAPI 里可见 —— 否则 schemathesis 什么都没测。"""
    labels = {op.ok().label for op in schema.get_all_operations()}
    assert operation in labels
