"""**只在契约测试里用**的最小 ASGI 应用：Excel 模板契约的可被 schemathesis 消费的形态。

## 为什么它在 tests/ 而不是 backend/api/

M3 交付的是报告层，不是 HTTP 接口（API 是 M4 的窗口）。但 v6 §12 的测试矩阵把
**「Excel 模板契约」列在 schemathesis 名下** —— schemathesis 只会消费 OpenAPI，
所以要让它作用在 Excel 契约上，就得给这个契约一个 OpenAPI 形态。

做法是把契约包成两个端点，放在测试目录里：

| 端点 | 契约含义 |
|---|---|
| `GET /reports/template-contract` | 模板契约本身：工作表名与顺序、三张表的表头、七个区块、区块1 的必需字段标签 |
| `POST /reports/workbook` | **写出→回读**这条路：请求体是 `SchedulePlan` 的 JSON Schema，合法输入必须渲染出能反解回同一个对象的 xlsx，非法输入必须 422，**任何输入都不许 5xx** |

第二个端点才是这组契约测试的价值所在：schemathesis 按 `SchedulePlan` 的
JSON Schema 生成大量畸形/边界载荷（缺字段、错枚举、越界时间、空机组…），
用它们轰渲染器。渲染器要么在契约层被挡下（422），要么产出一份能通过闸门3 的表；
**崩溃、静默吞掉、或者产出一份反解不回来的表，都会被抓出来。**

放在 `tests/` 的另一层意思是：它不进交付包，M4 写真的 API 时不必迁就它。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from backend.report.excel import render_workbook
from backend.report.template import template_contract
from backend.schemas.plan import SchedulePlan
from backend.validator.workbook import parse_workbook
from tests.fixtures.report_bundle import sample_bundle
from tests.fixtures.validator_facts import baseline_context


def create_app() -> FastAPI:
    app = FastAPI(title="FTS Excel 模板契约", version="1.0.0")

    @app.get("/reports/template-contract")
    def get_contract() -> dict[str, Any]:
        return template_contract()

    @app.post("/reports/workbook")
    def post_workbook(plan: SchedulePlan) -> Response:
        """渲染 + 回读。契约不合的载荷由 FastAPI 直接 422，到不了这里。"""
        bundle = sample_bundle(plan=plan, ctx=baseline_context())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contract.xlsx"
            render_workbook(path, bundle, readback_passed=True)
            name_map = {c.name: c.person_id for s in plan.sorties for c in s.crew}
            parsed = parse_workbook(path, name_map=name_map)
            return JSONResponse(
                status_code=200,
                content={
                    "sheets": list(parsed.sheet_names),
                    "readback_errors": parsed.errors,
                    "sorties": 0 if parsed.plan is None else len(parsed.plan.sorties),
                },
            )

    return app


__all__ = ["create_app"]
