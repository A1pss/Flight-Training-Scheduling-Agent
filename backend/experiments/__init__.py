"""§12 五组实验的执行与聚合（M9-B）。

**本包只做「跑实验、算指标、落盘」三件事**，不参与任何生产路径 ——
`backend/api`、`backend/graph`、六个确定性节点都不 import 它。

与既有评测底座的分工（不重复造轮子）：

| 已有 | 归属 | 本包如何用 |
|---|---|---|
| `tests/scenarios/run_suite.py` | M2-C | 实验二的 200 场景三重验证，直接调 |
| `backend/training/` | M7 | 实验四 §12.5.1 的两配置结果，**读盘不重跑** |
| `backend/datasets/` | M9-A | 九集数据的加载与校验 |
| `backend/planner/calibration.py` | M4-B | 实验一的 ECE / 可靠性图 / 校准器拟合 |

铁律 6 在本包里的落地：**指标函数只接受实测数据**，没有任何一处允许传入
「预期值」或缺省估计 —— 跑不出来的指标由调用方显式记 `未跑 + 原因`，
而不是让聚合层填一个数。
"""

from __future__ import annotations

__all__: list[str] = []
