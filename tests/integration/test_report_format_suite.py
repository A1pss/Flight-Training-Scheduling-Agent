"""200 场景的格式校验通过率（v6 §0.3 的不可调指标之二：**格式 100%**）。

口径与分母的说明见 `tests/scenarios/format_suite.py` 的模块文档：
分母是 **97 个出解场景**（另 103 个 INFEASIBLE 没有方案可导，不进分母）。
"""

from __future__ import annotations

import pytest

from tests.scenarios.format_suite import run_suite

pytestmark = pytest.mark.integration


def test_every_deliverable_plan_round_trips() -> None:
    summary, results = run_suite()
    assert summary.scenarios_total == 200
    assert summary.plans_total > 0
    assert summary.rendered == summary.plans_total, "有场景连渲染都没跑完"
    assert summary.failures == (), f"格式校验未通过的场景：{summary.failures}"
    assert summary.format_pass_rate == 1.0
    assert all(r.error is None for r in results)
    assert all(r.diff == () for r in results)
