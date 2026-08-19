"""数据集命令行：校验 / 刷新清单 / 看分布。

```bash
python -m backend.datasets.cli verify              # 全部已登记的数据集
python -m backend.datasets.cli verify nl_360
python -m backend.datasets.cli refresh nl_360      # 重算 sha256 / 条数 / 分层
python -m backend.datasets.cli stats nl_360
python -m backend.datasets.cli approve nl_360 --by Alps --at 2026-08-19
```

`approve` 把 `stage` 置为 `approved` 并记下确认人与日期。**它是唯一的批准入口** ——
手改 `manifest.json` 也能达到同样效果，但那样就没人知道是谁在什么时候批的。
数据一旦重新生成（`sha256` 变了），`write_datasets.py` 会自动把 stage 打回 `draft`，
批准不会被无声地继承下去。

`refresh` 是**手工编辑数据文件之后唯一正确的收尾动作** —— 改完 `items.jsonl`
不刷新清单，下一次 `load_eval_dataset` 就会因为哈希不符而拒绝加载（这正是设计意图）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from backend.datasets.card import render_card
from backend.datasets.loader import REGISTRY, dataset_dir, load_eval_dataset, stratum_field
from backend.datasets.manifest import (
    DatasetIntegrityError,
    load_manifest,
    sha256_of,
    write_manifest,
)


def _verify(names: list[str], version: str) -> int:
    failures = 0
    for name in names:
        try:
            manifest, items = load_eval_dataset(name, version=version)
        except (DatasetIntegrityError, FileNotFoundError, KeyError) as exc:
            print(f"❌ {name}/{version}: {exc}")
            failures += 1
            continue
        print(
            f"✅ {name}/{version}: {len(items)} 条 · stage={manifest.stage} · {manifest.sha256[:16]}…"
        )
    return failures


def _refresh(name: str, version: str) -> int:
    """重算清单里的 `sha256` / `item_count` / `strata`，其余字段原样保留。

    刷新前会**先逐条过一遍 schema** —— 一份不合契约的数据不该拿到一个漂亮的
    新哈希，那等于给坏数据发了合格证。
    """
    directory = dataset_dir(name, version)
    manifest = load_manifest(directory)
    items_path = directory / manifest.items_file
    model = REGISTRY[name]
    rows = [
        json.loads(line)
        for line in items_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    items = [model.model_validate(row) for row in rows]

    manifest.sha256 = sha256_of(items_path)
    manifest.item_count = len(items)
    field = stratum_field(items)
    if field is not None:
        manifest.strata = dict(sorted(Counter(str(getattr(i, field)) for i in items).items()))
    write_manifest(directory, manifest)

    # 刷完立刻走一次正常加载路径，确认刷新结果自洽（分层求和、唯一编号…）
    load_eval_dataset(name, version=version)
    print(f"✅ 已刷新 {name}/{version}: {manifest.item_count} 条 · {manifest.sha256[:16]}…")
    return 0


def _approve(name: str, version: str, *, by: str, at: str) -> int:
    """记业务方确认。**先完整加载一遍**——没通过校验的数据不该被批准。"""
    directory = dataset_dir(name, version)
    load_eval_dataset(name, version=version)
    manifest = load_manifest(directory)
    manifest.stage = "approved"
    manifest.approved_by = by
    manifest.approved_at = at
    write_manifest(directory, manifest)
    (directory / "card.md").write_text(render_card(manifest), encoding="utf-8", newline="\n")
    load_eval_dataset(name, version=version, require_approved=True)
    print(f"✅ {name}/{version} 已确认：{by} · {at}")
    return 0


def _stats(name: str, version: str) -> int:
    manifest, items = load_eval_dataset(name, version=version)
    print(f"{name}/{version} · stage={manifest.stage} · {manifest.item_count} 条")
    field = stratum_field(items)
    if field is not None:
        for key, count in sorted(Counter(str(getattr(i, field)) for i in items).items()):
            print(f"  {key:<24} {count}")
    for extra in ("expected_intent", "expected_action", "adversarial_kind"):
        if items and hasattr(items[0], extra):
            print(f"  ── {extra} ──")
            values = Counter(str(getattr(i, extra)) for i in items)
            for key, count in sorted(values.items()):
                print(f"    {key:<22} {count}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backend.datasets.cli")
    parser.add_argument("command", choices=("verify", "refresh", "stats", "approve"))
    parser.add_argument("names", nargs="*", help="数据集名；verify 留空表示全部")
    parser.add_argument("--version", default="v1")
    parser.add_argument("--by", default="", help="approve：确认人")
    parser.add_argument("--at", default="", help="approve：确认日期 YYYY-MM-DD")
    args = parser.parse_args(argv)

    if args.command == "verify":
        return _verify(args.names or sorted(REGISTRY), args.version)
    if not args.names:
        parser.error(f"{args.command} 需要指定数据集名")
    if args.command == "refresh":
        return _refresh(args.names[0], args.version)
    if args.command == "approve":
        if not (args.by and args.at):
            parser.error("approve 必须同时给 --by 与 --at")
        return _approve(args.names[0], args.version, by=args.by, at=args.at)
    return _stats(args.names[0], args.version)


if __name__ == "__main__":  # pragma: no cover —— 入口
    sys.exit(main())
