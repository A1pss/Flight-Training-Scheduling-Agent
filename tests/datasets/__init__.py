"""评测数据集的构造代码（`datasets/` 下的数据由这里生成）。

放在 `tests/` 而不是 `backend/` 的理由与 :mod:`tests.scenarios.catalog` 一致：
**它是造评测数据的工具，不是被交付的运行时代码**。生产路径 import 不到它，
离线交付包里也不需要它。
"""
