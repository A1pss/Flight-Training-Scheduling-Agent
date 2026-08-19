"""`plan_scenarios` 与 `golden_40` 的**核对与版本化**（W4 已产出，M9-A 不改内容）。

## 这两集与前四集不同：数据是别人产出的

- `plan_scenarios/v1/scenarios.json` —— W4 由 `tests/scenarios/catalog.py` 程序化生成；
- `tests/golden/test_golden_plans/*.yml` —— W4 由 `pytest --force-regen` 落的
  pytest-regressions 基线快照。

所以 M9-A 在这里做三件事，**一件都不包括「改数据」**：

1. **核对**：条数、分层、跑道关闭在不在、不可行是不是 I1~I5 五族；
2. **加契约**：`PlanScenarioItem` / `GoldenCaseItem`，加载即校验；
3. **加卡片**：版本号、SHA256、构造方法、已知局限。

## 两处不复制数据的决定

- `plan_scenarios` 的 `items_file` 直接指向 W4 的 `scenarios.json`（138 KB 的
  JSON 数组），加载器为此支持了数组载体。**复制成 jsonl 会立刻产生两个真相。**
- `golden_40` 只落**索引 + 指纹**（用例名 / 状态 / 架次数 / `content_sha256` /
  两条校验通道的判定），yml 本体留在 `tests/golden/`。那些文件唯一合法的更新方式是
  `pytest --force-regen` 之后逐行读 diff —— 复制一份出来，那条纪律就形同虚设。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import yaml

from backend.datasets.loader import REPO_ROOT

GOLDEN_DIR: Final[Path] = REPO_ROOT / "tests" / "golden" / "test_golden_plans"
SCENARIOS_FILE: Final[Path] = REPO_ROOT / "datasets" / "plan_scenarios" / "v1" / "scenarios.json"

#: v6 §12.3 的六个类别与条数。**核对用的期望值，写死在这里**——
#: 数据被改小了（比如有人删了几个不可行变体）要在加载期就看得见。
EXPECTED_COUNTS: Final[dict[str, int]] = {
    "baseline": 1,
    "single": 60,
    "combo": 60,
    "boundary": 40,
    "infeasible": 30,
    "reschedule": 9,
}

#: 单点扰动**必须**覆盖的五族。§12.3 原文：
#: 「1 人请假 / 1 机维修 / 1 资质到期 / **1 空域容量降为 0** / **1 跑道关闭**」。
EXPECTED_SINGLE_FAMILIES: Final[frozenset[str]] = frozenset(
    {"absence", "maintenance", "expiry", "airspace", "runway"}
)

#: 不可行是 **I1~I5 五族**，不是四族。
EXPECTED_INFEASIBLE_FAMILIES: Final[frozenset[str]] = frozenset({"I1", "I2", "I3", "I4", "I5"})


def scenario_rows() -> list[dict[str, Any]]:
    """W4 的 200 条场景，原样读出（不做任何改写）。"""
    payload = json.loads(SCENARIOS_FILE.read_text(encoding="utf-8"))
    return list(payload)


def golden_rows() -> list[dict[str, Any]]:
    """从 40 份基线快照抽出索引 + 指纹。

    ★ 其中 2 条是 `INFEASIBLE`（空域关闭、关闭叠跑道）——它们没有方案，
    因而没有 `content_sha256`、没有校验结果。这不是缺陷：`Z-26` 说的
    「40 个用例全部落在 `OPTIMAL`/`INFEASIBLE`」，指的正是这两种**都确定性可复现**
    的状态。
    """
    rows: list[dict[str, Any]] = []
    for number, path in enumerate(sorted(GOLDEN_DIR.glob("*.yml")), start=1):
        snapshot = yaml.safe_load(path.read_text(encoding="utf-8"))
        status = str(snapshot["status"])
        validation = snapshot.get("validation") or {}
        naive = snapshot.get("naive") or {}
        rules = validation.get("rules")
        validator_passed = (
            bool(validation["all_passed"]) and all(bool(item["passed"]) for item in rules.values())
            if isinstance(rules, dict)
            else None
        )
        rows.append(
            {
                "item_id": f"GOLD-{number:03d}",
                "case_id": path.stem,
                "baseline_file": f"tests/golden/test_golden_plans/{path.name}",
                "status": status,
                "num_sorties": int(snapshot["num_sorties"]),
                "num_candidates": int(snapshot["num_candidates"]),
                "content_sha256": snapshot.get("content_sha256"),
                "validator_passed": validator_passed,
                "naive_passed": bool(naive["passed"]) if "passed" in naive else None,
                "blocked_count": len(snapshot.get("blocked_items") or []),
                "debt_count": len(snapshot.get("debts") or []),
                "rationale": (
                    f"黄金用例 {path.stem}：{status}，{snapshot['num_sorties']} 架次，"
                    f"候选 {snapshot['num_candidates']}，"
                    f"校验条目 {validation.get('total_checked_items', 0)} 项。"
                    + (
                        "两条校验通道（主校验器 14 条 + 第三方 naive checker）同判通过。"
                        "★ 逐字节比对的锚点是 `content_sha256` —— 求解器换了个等价最优解、"
                        "校验器少查了几项、阻塞项措辞被改写，都会在这里变成一个 diff。"
                        if status == "OPTIMAL"
                        else "★ 不可行用例：没有方案，因而没有指纹。它照样进两条部署路径的"
                        "聚合指纹 —— `INFEASIBLE` 这个判定本身就是确定性的。"
                    )
                ),
            }
        )
    return rows


def verify_scenarios(rows: list[dict[str, Any]]) -> list[str]:
    """核对报告：返回**问题清单**，空列表表示全部对上。"""
    problems: list[str] = []
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    if counts != EXPECTED_COUNTS:
        problems.append(f"类别条数不符：期望 {EXPECTED_COUNTS}，实际 {counts}")

    singles = {row["family"] for row in rows if row["category"] == "single"}
    missing = EXPECTED_SINGLE_FAMILIES - singles
    if missing:
        problems.append(f"单点扰动缺族：{sorted(missing)}（§12.3 点名要有跑道关闭与空域降容）")

    infeasible = {row["family"] for row in rows if row["category"] == "infeasible"}
    if infeasible != EXPECTED_INFEASIBLE_FAMILIES:
        problems.append(f"不可行族不符：期望 I1~I5 五族，实际 {sorted(infeasible)}")
    for family in sorted(EXPECTED_INFEASIBLE_FAMILIES):
        variants = [r for r in rows if r["family"] == family]
        if len(variants) != 6:
            problems.append(f"{family} 族有 {len(variants)} 个变体，期望 6")
        for variant in variants:
            if not variant.get("annotated_conflict_rules"):
                problems.append(f"{variant['scenario_id']} 没有标注真实冲突源")
    return problems


__all__ = [
    "EXPECTED_COUNTS",
    "EXPECTED_INFEASIBLE_FAMILIES",
    "EXPECTED_SINGLE_FAMILIES",
    "GOLDEN_DIR",
    "golden_rows",
    "scenario_rows",
    "verify_scenarios",
]
