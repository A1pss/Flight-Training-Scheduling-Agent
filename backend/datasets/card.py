"""Dataset card 的渲染：**从 `manifest.json` 生成，不手写**。

手写的卡片一定会与数据脱节 —— 加两条样本、改一次分层，卡片上的数字就成了历史。
所以这里把卡片定义成清单的一个**投影**：`card.md` 由 `manifest.json` 渲染而来，
`tests/datasets/test_cards.py` 断言仓库里的卡片与渲染结果逐字节相同。

要改卡片的措辞就改本模块；要改卡片上的数字就改数据然后重新生成。
"""

from __future__ import annotations

from backend.datasets.manifest import DatasetManifest

_STAGE_TEXT = {
    "sample": "送审样例 —— 仅供业务方复核口径，**不得用于实验**",
    "draft": "全量已生成、待业务方复核 —— **不得用于实验**",
    "approved": "业务方已确认，可用于实验",
}


def render_card(manifest: DatasetManifest) -> str:
    lines: list[str] = [
        f"# Dataset Card · `{manifest.name}` {manifest.version}",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 版本 | `{manifest.version}` |",
        f"| 状态 | `{manifest.stage}` —— {_STAGE_TEXT[manifest.stage]} |",
        f"| 条数 | **{manifest.item_count}** |",
        f"| 数据文件 | `{manifest.items_file}` |",
        f"| SHA256 | `{manifest.sha256}` |",
        f"| 生成时间 | {manifest.generated_at} |",
    ]
    if manifest.approved_by:
        lines.append(f"| 业务方确认 | {manifest.approved_by} · {manifest.approved_at} |")
    lines += ["", "## 分层分布", "", "| 分层 | 条数 |", "|---|---|"]
    for key, count in sorted(manifest.strata.items()):
        lines.append(f"| `{key}` | {count} |")
    lines.append(f"| **合计** | **{manifest.item_count}** |")

    lines += ["", "## 构造方法", "", manifest.method]
    if manifest.context:
        lines += ["", "## 判读上下文", "", "| 键 | 值 |", "|---|---|"]
        for key, value in sorted(manifest.context.items()):
            lines.append(f"| `{key}` | {value} |")
    if manifest.spec_refs:
        lines += ["", "## 规格依据", ""]
        lines += [f"- {ref}" for ref in manifest.spec_refs]
    if manifest.known_limitations:
        lines += ["", "## 已知局限", ""]
        lines += [f"{i}. {text}" for i, text in enumerate(manifest.known_limitations, start=1)]
    lines += [
        "",
        "## 怎么用",
        "",
        "```python",
        "from backend.datasets.loader import load_eval_dataset",
        "",
        f'manifest, items = load_eval_dataset("{manifest.name}", require_approved=True)',
        "```",
        "",
        "加载路径会复核 SHA256、条数、分层分布与逐条 schema；任何一项不符即抛 "
        "`DatasetIntegrityError`。手工改过数据文件之后必须跑 "
        f"`python -m backend.datasets.cli refresh {manifest.name}`。",
        "",
    ]
    return "\n".join(lines)


__all__ = ["render_card"]
