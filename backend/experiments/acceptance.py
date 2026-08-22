"""验收报告的**装配**（v6 §12.7）：`python -m backend.experiments.acceptance`。

## 为什么报告要生成而不是手写

铁律 6 说「不报告未实际计算的指标」。手抄一遍数字是**最容易违反它的方式** ——
抄错一位没人看得出来，跑了一半的实验被当成跑完的也没人看得出来。所以这里
从各实验的结果文件**读**，缺哪个就在报告里写「未跑 + 原因」，而不是留空
或填一个看起来合理的数。

## 三类口径必须分表（§12.7）

| 表 | 内容 | 判定 |
|---|---|---|
| **A 不可调指标** | 2 条 100% 类 | 任何一条不为 100% 即整体不通过，无协商余地 |
| **B 验收主指标** | 2 条 ≥92% | 达标即绿、未达标即整体不通过 |
| **C 工程指标** | 其余 | 允许个别项以「已定位原因 + 改进项」带条件通过 |

**三类混排是验收会上最容易吵起来的地方** —— 尤其不能让「某个工程指标差 2 个点」
看起来和「格式校验没到 100%」一样严重。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

M9B = Path("reports/m9b")

#: 缺数据时统一的占位 —— **不是 0、不是「约」**，是一句说明。
NOT_RUN = "未跑"


@dataclass(frozen=True)
class Row:
    """报告里的一行指标。"""

    name: str
    target: str
    measured: str
    verdict: str
    note: str = ""


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def pct(value: float | None, digits: int = 2) -> str:
    return NOT_RUN if value is None else f"{value * 100:.{digits}f}%"


def interval_str(interval: Mapping[str, Any] | None, digits: int = 2) -> str:
    """点估计 + Wilson 区间。**区间不是可选项** —— 只报点估计的比例会让
    「200 条里全对」看起来和「20000 条里全对」一样确定。"""
    if not interval:
        return NOT_RUN
    point = float(interval["point"])
    low, high = float(interval["low"]), float(interval["high"])
    if point != point:  # NaN
        return "不适用"
    return f"{point * 100:.{digits}f}% [{low * 100:.{digits}f}, {high * 100:.{digits}f}]"


def verdict_of(measured: float | None, target: float, *, higher_is_better: bool = True) -> str:
    if measured is None:
        return "⬜ 未跑"
    ok = measured >= target if higher_is_better else measured <= target
    return "✅ 达标" if ok else "❌ 未达标"


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        return "（取不到）"


def dataset_fingerprints() -> list[tuple[str, str, Any, str]]:
    """九集的 SHA256 与 stage —— 环境指纹的一部分（§12.7）。"""
    out: list[tuple[str, str, Any, str]] = []
    root = Path("datasets")
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        manifest = read_json(d / "v1" / "manifest.json")
        if manifest:
            out.append(
                (
                    d.name,
                    str(manifest.get("sha256", ""))[:16] + "…",
                    manifest.get("item_count"),
                    str(manifest.get("stage", "")),
                )
            )
    return out


def semantics_snapshot() -> list[tuple[str, str, str]]:
    """S-01~S-13 全部取值（§12.7 必述项 3）。

    同一份数据在不同解读下会排出不同的班，**不写清开关取值的验收结论是不可
    复现的**。
    """
    from backend.core.ruleset import get_semantics

    rows: list[tuple[str, str, str]] = []
    switches = getattr(get_semantics(), "switches", {}) or {}
    for key in sorted(switches):
        item = switches[key]
        rows.append((key, str(item.get("topic", "")), str(item.get("value", ""))))
    return rows


def render_table(rows: list[Row], *, with_note: bool = True) -> str:
    head = "| 指标 | 目标 | 实测 | 判定 |" + (" 说明 |" if with_note else "")
    sep = "|---|---|---|---|" + ("---|" if with_note else "")
    lines = [head, sep]
    for r in rows:
        line = f"| {r.name} | {r.target} | {r.measured} | {r.verdict} |"
        if with_note:
            line += f" {r.note} |"
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "M9B",
    "NOT_RUN",
    "Row",
    "dataset_fingerprints",
    "git_commit",
    "interval_str",
    "pct",
    "read_json",
    "render_table",
    "semantics_snapshot",
    "verdict_of",
]
