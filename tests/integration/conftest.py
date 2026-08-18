"""集成测试的公共前置。

## 唯一的内容：把插桩期的求解墙钟设上（`Z-15` / `Z-23`）

`tests/conftest.py` 里那把是**函数作用域**的，**盖不住「在模块/会话 fixture 里
跑的求解」** —— pytest 按作用域从大到小建 fixture，模块 fixture 先建，那时候
环境变量还没设上，而预算是在 `compile_spec` 那一刻被烤进
`ConstraintSpec.solver_time_limit_s` 的。于是整次求解用的是**产品默认 60 s**。

M6 在 `test_api_live.py` 里踩过一次并就地修好，收工报告 §3.13 留了一句
「**凡是在 module / session fixture 里跑求解的测试，都要照抄这条**」——
但那句话没有被执行：M8 收工时全仓库有 **11 个集成模块**符合这个描述而
一个都没有那把预算。它们一直在 60 s 下跑，**靠运气过**。

**代价在 M8 的 PR #13 上兑现了**：一个只改了 27 行 markdown 的提交，CI 红在

```
FAILED tests/integration/test_graph_live.py::test_baseline_solution_is_optimal_and_fully_compliant
       AssertionError: assert 'FEASIBLE' == 'OPTIMAL'
```

同一份代码 34 分钟前刚在 CI 上全绿过。**这就是「会飘的门禁」** —— 它教人重跑，
而不是教人查问题。

## 为什么放在 conftest 而不是每个文件里抄一遍

11 份拷贝意味着第 12 个模块还是会漏。**包作用域 + autouse** 让它对
`tests/integration/` 下的每个模块自动生效，且**包作用域高于模块作用域**，
所以它一定在任何模块 fixture 之前建立 —— 那正是这件事成立的前提。

## 这不是放宽判据

`Z-23` 的 300 s 是业务方 2026-08-15 已经裁定的**插桩环境**预算，
**产品默认仍是 60 s，一个字没动**（`Settings.SOLVER_TIME_LIMIT_S`）。
14 条硬约束、三态判据、`OPTIMAL` 断言全部原样 —— 变的只是「让已裁定的预算
真正送到这些模块的求解上」。

按 `Z-15` 的护栏，动手前先量了三项（M8 收工报告 §12）：
`backend/solver/` 在两轮之间**零改动**、模型规模与 M2-A 逐字相同、
无插桩单次求解墙钟落在参照区间 18.8~26.0 s 之内。三项都指向**不是回归**。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from backend.core.config import get_settings
from tests.conftest import TEST_SOLVER_TIME_LIMIT_S


@pytest.fixture(scope="package", autouse=True)
def instrumented_solver_budget_for_integration() -> Iterator[None]:
    """给整个 `tests/integration/` 包设插桩期求解墙钟（`Z-23`：300 s）。

    **包作用域**：高于模块作用域，所以它在任何 module fixture 之前建立
    —— 这正是 `tests/conftest.py` 那把函数作用域的做不到的事。
    """
    previous = os.environ.get("SOLVER_TIME_LIMIT_S")
    os.environ["SOLVER_TIME_LIMIT_S"] = TEST_SOLVER_TIME_LIMIT_S
    get_settings.cache_clear()
    yield
    if previous is None:
        os.environ.pop("SOLVER_TIME_LIMIT_S", None)
    else:
        os.environ["SOLVER_TIME_LIMIT_S"] = previous
    get_settings.cache_clear()
