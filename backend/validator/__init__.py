"""独立校验器（v6 §4）—— 三道闸门里的前两道半。

```
闸门1  validator/checks.py    14 条规则重算（另一套代码，只读解）
闸门2  validator/schema.py    Pydantic 契约 + 外键 + 三表交叉一致性
闸门3  validator/workbook.py  Excel 回读反解 + 与源对象深度相等
```

**本包不引用求解器的任何模块，也不依赖 OR-Tools**（CLAUDE.md 铁律 2，由
import-linter 的禁令一强制）。规则参数与两侧共用的 `rules/*.yaml` 由
`backend.core.ruleset` 读取——它只做类型化，不表达任何约束。

典型用法：

```python
with session_scope() as session:
    ctx = load_context(session, snapshot_id=snap, week_start=monday)
report = run_all_checks(plan, ctx)          # 闸门1
fmt = verify_format(plan, ctx)              # 闸门2
book = verify_workbook(path, plan, ctx=ctx) # 闸门3
```
"""

from backend.validator.checks import ALL_CHECKS, RULE_TITLES, run_all_checks
from backend.validator.context import ValidationContext, context_from_rows, load_context
from backend.validator.schema import (
    FormatCheckReport,
    ThreeTableProjection,
    check_cross_table_consistency,
    check_referential_integrity,
    project_plan,
    validate_plan_schema,
    verify_format,
)
from backend.validator.workbook import parse_workbook, verify_workbook

__all__ = [
    "ALL_CHECKS",
    "RULE_TITLES",
    "FormatCheckReport",
    "ThreeTableProjection",
    "ValidationContext",
    "check_cross_table_consistency",
    "check_referential_integrity",
    "context_from_rows",
    "load_context",
    "parse_workbook",
    "project_plan",
    "run_all_checks",
    "validate_plan_schema",
    "verify_format",
    "verify_workbook",
]
