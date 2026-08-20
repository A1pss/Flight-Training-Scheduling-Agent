"""数据集清单：版本、条数、分层分布、SHA256、生成时间、已知局限。

## 为什么 SHA256 记在清单里而不是算完就丢

数据集要跨窗口用（W11 造、W12 合成、W13 验收），中间隔着几十次 commit。
`items.jsonl` 被手工改过一行而没人注意，是这类项目里最常见的静默偏差 ——
验收报告上的 92% 与半年后复现出来的 89% 差在哪，事后没有任何办法查。
清单里钉住哈希，加载时复核，差一个字节就抛。

## `stage` 这一位

`CLAUDE.md` 给本窗口的规则是「每集产出后**先停下来交用户审核**」。所以清单区分
`sample`（送审样例）/ `draft`（全量已生成、待复核）/ `approved`（业务方已确认）。
**只有 `approved` 的数据集允许进实验**，`load_eval_dataset(..., require_approved=True)`
就是那道闸。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Stage = Literal["sample", "draft", "approved"]


class DatasetManifest(BaseModel):
    """一份数据集卡片的机器可读部分（`card.md` 是同一份内容的人读版）。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="数据集名，与目录名一致")
    version: str = Field(pattern=r"^v\d+$")
    stage: Stage
    items_file: str = Field(default="items.jsonl")
    item_count: int = Field(ge=0)
    #: 分层名 → 条数。求和必须等于 `item_count`（`verify_manifest` 会查）
    strata: dict[str, int] = Field(default_factory=dict)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: str = Field(description="ISO-8601，生成时间")
    #: 构造方法，一两句话说清「这些条目是怎么来的」
    method: str = Field(min_length=1)
    #: v6 / SPEC_DECISIONS 的依据章节
    spec_refs: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    #: 判读这份标注需要的固定上下文。最要紧的一项是 `eval_today` ——
    #: 「下周」「本周」这类相对周表述只有钉死一个参照日才有唯一答案，
    #: 否则同一条标注今天判对、下周判错（铁律 9）。
    context: dict[str, str] = Field(default_factory=dict)
    #: 业务方确认记录（`stage == "approved"` 时必填）
    approved_by: str | None = None
    approved_at: str | None = None


def sha256_of(path: Path) -> str:
    """文件的 SHA256。逐块读，数据集再大也不吃内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> str:
    """写 JSONL 并返回 SHA256。

    **三条固定写法都是为了铁律 9**：`ensure_ascii=False`（中文原样，diff 可读）、
    `sort_keys=True`（键序固定）、行尾统一 `\\n`。少任何一条，同样的内容重生成
    一次就会得到不同的哈希。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
    return sha256_of(path)


def load_manifest(directory: Path) -> DatasetManifest:
    """读 `manifest.json`。"""
    raw = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    return DatasetManifest.model_validate(raw)


def write_manifest(directory: Path, manifest: DatasetManifest) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


class DatasetIntegrityError(ValueError):
    """清单与数据文件对不上。**不降级、不放行**（铁律 7 的同一条精神）。"""


def verify_manifest(directory: Path, manifest: DatasetManifest, line_count: int) -> None:
    """三项复核：哈希、条数、分层求和。任何一项不符即抛。"""
    items = directory / manifest.items_file
    actual_sha = sha256_of(items)
    if actual_sha != manifest.sha256:
        raise DatasetIntegrityError(
            f"{manifest.name}/{manifest.version}：{manifest.items_file} 的 SHA256 与清单不符\n"
            f"  清单 = {manifest.sha256}\n  实际 = {actual_sha}\n"
            "  数据被改过而清单没跟着更新；要么还原文件，要么重跑生成脚本。"
        )
    if line_count != manifest.item_count:
        raise DatasetIntegrityError(
            f"{manifest.name}/{manifest.version}：实际 {line_count} 条，清单写 {manifest.item_count} 条"
        )
    if manifest.strata:
        total = sum(manifest.strata.values())
        if total != manifest.item_count:
            raise DatasetIntegrityError(
                f"{manifest.name}/{manifest.version}：分层求和 {total} ≠ 条数 {manifest.item_count}"
            )
    if manifest.stage == "approved" and not (manifest.approved_by and manifest.approved_at):
        raise DatasetIntegrityError(
            f"{manifest.name}/{manifest.version}：stage=approved 但没有确认人/确认时间"
        )


__all__ = [
    "DatasetIntegrityError",
    "DatasetManifest",
    "Stage",
    "load_manifest",
    "sha256_of",
    "verify_manifest",
    "write_jsonl",
    "write_manifest",
]
