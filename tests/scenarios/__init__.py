"""v6 §12.3 的 200 场景测试集：生成器、标定器与运行器。

- :mod:`tests.scenarios.catalog` —— 200 个场景的**程序化**构造（标签天然正确）
- :mod:`tests.scenarios.calibrate` —— 边界场景的「恰好」标定（单调旋钮二分）
- :mod:`tests.scenarios.runner` —— 逐场景求解 + 三重校验 + 冲突集度量
- :mod:`tests.scenarios.run_suite` —— CLI：生成 → 落 `datasets/plan_scenarios/` → 全量跑 → 出报告

**为什么不做成 pytest 用例**：200 个场景全跑一遍要一个多小时（基准周单次求解
实测约 20 秒，其中 8 秒是可复现性要求的单线程规范化阶段，砍不得）。所以全量跑
是一条 CLI 批任务，产物落 `reports/`；pytest 里只留一个**抽样冒烟**用例
（`tests/integration/test_scenario_suite_live.py`），CI 才跑得动。
"""
