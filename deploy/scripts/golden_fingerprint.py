"""黄金用例指纹 —— **两条部署路径的可比对产物**（v6 §11.4 + §13 M8 出口标准）。

> native 与 compose 两条路径产出的 `content_sha256` 逐字节相同

## 它算的是什么

对 `tests/golden` 的 40 个固定场景逐个 `solve()`，取每个方案的
`content_sha256`，再按用例名排序拼起来算一次 sha256 —— 这个聚合值就是
**这套部署跑出来的黄金指纹**。

一个数就能比对两条路径，是刻意的：40 个 sha256 逐个比对在 shell 里既难写又
难读，而任何一个用例不同都会让聚合值不同。真不同时再用 `--per-case` 看是哪个。

## 为什么用黄金用例而不是基准周

基准周单次求解约 20 秒（含铁律 9 要求的单线程规范化），装一次跑一次可以，但
两条路径各跑一次再加 install.sh 里那次就是一分钟起步；而黄金用例 40 个合成场景
约 8 秒，且**全部证到 OPTIMAL** —— 那正是 v6 §3.11.1 说的「逐字节可复现」
的成立条件（`FEASIBLE` 不保证）。拿一个不保证可复现的量去比对两条路径，
比出来的差异说明不了任何问题。

基准周的逐字节可复现另有专测（`tests/integration/test_solver_baseline_live.py`）。

## 用法

```bash
python -m deploy.scripts.golden_fingerprint              # 只打聚合指纹
python -m deploy.scripts.golden_fingerprint --per-case   # 逐个用例
python -m deploy.scripts.golden_fingerprint --json out.json
```

退出码：0 = 全部用例落在**可复现的两种终态**（`OPTIMAL` / `INFEASIBLE`）
且算出了指纹；1 = 有用例停在 `FEASIBLE` 或 `UNKNOWN`（那时候解不唯一、指纹
不可比，**不输出一个看起来没问题的数**）。

⚠️ 40 个用例里有两个（`g29_airspace_closed` / `g38_closure_plus_runway`）
**设计上就是 INFEASIBLE** —— 空域整周关闭本来就排不出班。「不可行」同样是
确定性结论，参与指纹时记作 `INFEASIBLE` 这个字面量。把它们当失败会让这个
工具永远返回 1（第一版就是这么写的）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - 供 `bash` 直接调用时用
    sys.path.insert(0, str(REPO_ROOT))

from backend.solver.solve import solve  # noqa: E402
from tests.golden.test_golden_plans import GOLDEN_CASES, _scenario  # noqa: E402

#: 逐字节可复现的两种终态（v6 §3.11.1）。`FEASIBLE`/`UNKNOWN` 不在其中。
REPRODUCIBLE_STATUSES = ("OPTIMAL", "INFEASIBLE")


def per_case_digests() -> dict[str, str]:
    """逐个黄金用例的可比对值。

    - `OPTIMAL` → 方案的 `content_sha256`；
    - `INFEASIBLE` → 字面量 `INFEASIBLE`（确定性结论，照样进指纹）；
    - 其它 → `!<状态>`，会让整体退出码变 1。
    """
    out: dict[str, str] = {}
    for name in sorted(GOLDEN_CASES):
        outcome = solve(_scenario(name).to_bundle())
        if outcome.status == "INFEASIBLE":
            out[name] = "INFEASIBLE"
        elif outcome.status == "OPTIMAL" and outcome.plan is not None:
            out[name] = outcome.plan.content_sha256
        else:
            out[name] = f"!{outcome.status}"
    return out


def aggregate(digests: dict[str, str]) -> str:
    """按用例名排序拼接后再 sha256。**排序是必须的** —— 字典序不定就不可复现。"""
    joined = "\n".join(f"{name}={digests[name]}" for name in sorted(digests))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="黄金用例指纹（v6 §11.4 两条路径比对用）")
    parser.add_argument("--per-case", action="store_true", help="逐个用例打印 content_sha256")
    parser.add_argument("--json", type=Path, default=None, help="把结果写成 JSON")
    args = parser.parse_args(argv)

    digests = per_case_digests()
    failed = {name: value for name, value in digests.items() if value.startswith("!")}
    fingerprint = aggregate(digests)

    if args.per_case:
        for name in sorted(digests):
            print(f"{name}\t{digests[name]}")
    print(f"cases={len(digests)}")
    print(f"golden_fingerprint={fingerprint}")

    if args.json is not None:
        args.json.write_text(
            json.dumps(
                {"cases": digests, "golden_fingerprint": fingerprint, "count": len(digests)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    if failed:
        print(
            f"❌ 有用例停在不可复现的终态，指纹不可比：{sorted(failed)}"
            f"（可复现的终态只有 {REPRODUCIBLE_STATUSES}）",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口薄封装
    sys.exit(main())
