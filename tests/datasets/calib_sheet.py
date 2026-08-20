"""生成**人工标注表**（`judge_calib_50` 的交付形态之一）。

## 为什么不让人直接改 jsonl

50 条 × 平均 3.6 条断言 + 上下文利用率那部分，直接编辑 JSON 意味着一边数括号
一边判断 —— 判断本身够费神了，不该再叠一层格式负担。所以交付两份：

| 文件 | 给谁 | 干什么 |
|---|---|---|
| `items.jsonl` | 机器 | 契约、校验、W13 取数 |
| `annotation_sheet.csv` | 人 | **只填两列**，其余都是只读上下文 |

标完之后由 `merge_annotations()` 合回 jsonl —— 合并时会逐条核对
`item_id` / `claim_id` 对得上、取值在枚举内，**对不上就抛**，不静默丢。

## 表里有两种行

`kind` 列区分：

- `claim` —— 一条断言，填 `verdict`（`SUPPORTED` / `PARTIAL` / `NOT_SUPPORTED`）
- `context` —— 一条**进了 Top-5 的 gold 召回条目**，填 `used`（`Y` / `N`）

两者是**两个指标**：前者是 Faithfulness，后者是上下文利用率。§12.4.1 把它们
分开定义，判定粒度也不同（断言 vs 召回条目），所以表里也分开。

**非陈述片段（`is_assertive=false`）不进表** —— 「检索到以下相关内容：」
没有「被不被支撑」可言，把它们塞进去只会让一致率虚高。
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

HEADER: Final[tuple[str, ...]] = (
    "kind",
    "item_id",
    "ref_id",
    "stratum",
    "memory_type",
    "query",
    "target",
    "answer",
    "contexts",
    "verifier_hint",
    "verdict",
    "used",
)

VERDICTS: Final[frozenset[str]] = frozenset({"SUPPORTED", "PARTIAL", "NOT_SUPPORTED"})
USED_YES: Final[frozenset[str]] = frozenset({"Y", "y", "是", "TRUE", "true", "1"})
USED_NO: Final[frozenset[str]] = frozenset({"N", "n", "否", "FALSE", "false", "0"})


def _contexts_blob(item: dict[str, Any]) -> str:
    return "\n".join(
        f"[{n}] {c['doc_id']} :: {c['snippet']}"
        for n, c in enumerate(item["retrieved_contexts"], start=1)
    )


def write_annotation_sheet(path: Path, items: Sequence[dict[str, Any]]) -> int:
    """写 CSV，返回待填行数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        for item in items:
            blob = _contexts_blob(item)
            for claim in item["claims"]:
                if not claim.get("is_assertive", True):
                    continue
                hint = claim.get("verifier_supported")
                writer.writerow(
                    [
                        "claim",
                        item["item_id"],
                        claim["claim_id"],
                        item["stratum"],
                        item["memory_type"],
                        item["query"],
                        claim["text"],
                        item["answer"],
                        blob,
                        "" if hint is None else ("有出处" if hint else "无出处"),
                        "",
                        "",
                    ]
                )
                written += 1
            for entry in item["context_usage"]:
                writer.writerow(
                    [
                        "context",
                        item["item_id"],
                        entry["doc_id"],
                        item["stratum"],
                        item["memory_type"],
                        item["query"],
                        entry["snippet"],
                        item["answer"],
                        blob,
                        "",
                        "",
                        "",
                    ]
                )
                written += 1
    return written


def annotated_label_count(items_path: Path) -> tuple[int, int]:
    """数一份 `items.jsonl` 里已经填了多少标签，返回 `(断言, 召回条目)`。

    ★ **给「别把标注覆盖掉」那道闸用的。** `judge_calib_50` 的标注是人工的、
    不可复现的劳动（204 行），而重新生成这一集会产出一批空标签的新条目 ——
    一次 `write_datasets.py`（不带参数 = 全部）就能把它抹掉，且不留任何痕迹。
    """
    if not items_path.exists():
        return (0, 0)
    claims = contexts = 0
    for line in items_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        claims += sum(1 for c in row.get("claims", []) if c.get("verdict") is not None)
        contexts += sum(1 for e in row.get("context_usage", []) if e.get("used") is not None)
    return (claims, contexts)


def merge_annotations(sheet: Path, items_path: Path) -> tuple[int, int]:
    """把填好的表合回 `items.jsonl`。返回 `(断言数, 召回条目数)`。

    **对不上就抛**：`item_id` / `ref_id` 找不到、取值不在枚举内，一律报错而不是
    静默丢 —— 一条被悄悄丢掉的标注，会让分母对不上而没人发现。
    """
    rows = [
        json.loads(line)
        for line in items_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_id = {row["item_id"]: row for row in rows}
    claims = 0
    contexts = 0
    with sheet.open("r", encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            item = by_id.get(record["item_id"])
            if item is None:
                raise KeyError(f"标注表里的 {record['item_id']} 不在数据集里")
            if record["kind"] == "claim":
                verdict = record["verdict"].strip()
                if not verdict:
                    continue
                if verdict not in VERDICTS:
                    raise ValueError(
                        f"{record['item_id']}/{record['ref_id']}：verdict={verdict!r} "
                        f"不在 {sorted(VERDICTS)} 里"
                    )
                target = next(
                    (c for c in item["claims"] if c["claim_id"] == record["ref_id"]), None
                )
                if target is None:
                    raise KeyError(f"{record['item_id']} 没有断言 {record['ref_id']}")
                target["verdict"] = verdict
                claims += 1
            elif record["kind"] == "context":
                used = record["used"].strip()
                if not used:
                    continue
                if used not in USED_YES and used not in USED_NO:
                    raise ValueError(
                        f"{record['item_id']}/{record['ref_id']}：used={used!r} 只能是 Y 或 N"
                    )
                entry = next(
                    (c for c in item["context_usage"] if c["doc_id"] == record["ref_id"]), None
                )
                if entry is None:
                    raise KeyError(f"{record['item_id']} 没有召回条目 {record['ref_id']}")
                entry["used"] = used in USED_YES
                contexts += 1
    with items_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return claims, contexts


__all__ = [
    "HEADER",
    "annotated_label_count",
    "merge_annotations",
    "write_annotation_sheet",
]
