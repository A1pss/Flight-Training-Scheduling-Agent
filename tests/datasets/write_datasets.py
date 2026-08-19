"""把构造代码的产物落成 `datasets/<name>/<version>/` 下的数据文件。

```bash
PYTHONPATH=. python tests/datasets/write_datasets.py nl_360
```

**不是测试**，是生成器入口。`tests/datasets/test_nl_360.py` 会断言仓库里的
`items.jsonl` 与构造代码的输出**逐字节相同** —— 于是「手改了数据但忘了改代码」
和「改了代码但忘了重生成」两种漂移都会在 CI 上变成红。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from backend.datasets.card import render_card
from backend.datasets.loader import dataset_dir, load_eval_dataset
from backend.datasets.manifest import DatasetManifest, load_manifest, write_jsonl, write_manifest
from tests.datasets import nl_catalog


def write_nl_360() -> None:
    rows = nl_catalog.build()
    directory = dataset_dir("nl_360")
    sha = write_jsonl(directory / "items.jsonl", rows)
    strata: dict[str, int] = {}
    for row in rows:
        layer = str(row["layer"])
        strata[layer] = strata.get(layer, 0) + 1

    previous: DatasetManifest | None = None
    try:
        previous = load_manifest(directory)
    except FileNotFoundError:
        previous = None

    keep = previous is not None and previous.sha256 == sha
    manifest = DatasetManifest(
        name="nl_360",
        version="v1",
        stage=previous.stage if (keep and previous is not None) else "draft",
        item_count=len(rows),
        strata=dict(sorted(strata.items())),
        sha256=sha,
        generated_at=(
            previous.generated_at
            if keep and previous
            else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        method=(
            "Claude Code 逐条构造（重复度高的层用程序化组合保证覆盖齐全）→ Alps 逐批人工复核。"
            "实体一律取自 v6 §1.3 基准实体表；构造代码见 tests/datasets/nl_catalog.py。"
        ),
        spec_refs=[
            "v6 §12.2",
            "v6 §1.3",
            "SPEC_DECISIONS §D",
            "v6 §7.2.1",
            "v6 §5.4",
            "v6 §12.5.3",
        ],
        known_limitations=[
            "六类意图中 ingest / export 只有 5 条样本 —— §12.2 的六层分布没有给这两类"
            "留独立分层，它们只出现在歧义层与对抗层的多意图样本里。意图分类准确率要按类"
            "分别报，这两类的置信区间会很宽。",
            "相对周表述（本周/下周）的判读依赖 context.eval_today=2026-01-05，换参照日会"
            "改变期望槽位。",
            "标注口径按 SPEC_DECISIONS §D：Claude Code 初稿 + Alps 人工复核，"
            "**不计算也不报告双人标注的 Cohen's Kappa**（v6 §12.7 必述项 2）。",
            "「约束修饰」槽位里 kind=OTHER 的条目共 6 条，它们表达的是冻结档位或目标权重"
            "偏好（R3），DSL 中没有对应的 IncrementalConstraint —— 修订翻译准确率统计时"
            "要把它们单列，不能算作翻译失败。",
        ],
        context={
            "eval_today": "2026-01-05",
            "baseline_week": "2026W02",
            "week_format": "YYYYWww（与 backend.schemas.intent 的 iso_week 正则一致）",
            "ruling_typo": "唯一候选就执行；候选不唯一则反问（业务方 2026-08-19）",
            "ruling_multi_intent": "取主意图执行；副意图的周次不进槽位（业务方 2026-08-19）",
            "ruling_missing_week": "缺周次一律归歧义层，期望动作 ask_clarify（业务方 2026-08-19）",
        },
        approved_by=previous.approved_by if keep and previous else None,
        approved_at=previous.approved_at if keep and previous else None,
    )
    write_manifest(directory, manifest)
    (directory / "card.md").write_text(render_card(manifest), encoding="utf-8", newline="\n")
    loaded_manifest, items = load_eval_dataset("nl_360")
    print(f"✅ nl_360 {len(items)} 条 · {loaded_manifest.sha256[:16]}… · {loaded_manifest.strata}")


WRITERS = {"nl_360": write_nl_360}


def main(argv: list[str]) -> int:
    names = argv[1:] or sorted(WRITERS)
    for name in names:
        WRITERS[name]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
