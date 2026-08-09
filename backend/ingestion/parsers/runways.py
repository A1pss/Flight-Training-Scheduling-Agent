"""跑道 → :class:`IngestedRunway`。

**跑道不来自任何 PDF。** 四份原始资料里没有跑道表 —— `rules.pdf` 约束9 只提到
「同一跑道」，没说有几条、各服务什么机型。双跑道模型是业务方在 S-05 里裁定
的，权威落点是 `rules/semantics.yaml`。所以这个 parser 读 YAML，不读 PDF。

⚠️ **映射不是「RWY-1=JL-8、RWY-2=JL-9」**（v6 §1.3.5）：

- `RWY-1` 服务 **JL-8 与 JL-9** → 全 8 架
- `RWY-2` **只服务 JL-8** → 六架

JL-9 架次因此固定使用 RWY-1；JL-8 架次的跑道是求解决策变量（§3.3）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from backend.core.config import get_settings
from backend.core.errors import IngestionError
from backend.ingestion.schema import IngestedRunway

#: 跑道显示名。`semantics.yaml` 只给机型映射，不给名字。
RUNWAY_NAMES = {"RWY-1": "跑道 1", "RWY-2": "跑道 2"}


def load_semantics(path: Path | None = None) -> dict[str, Any]:
    """读 `rules/semantics.yaml`。"""
    target = path or get_settings().SEMANTICS_PATH
    try:
        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise IngestionError(
            f"读取语义开关文件失败：{target}", details={"path": str(target), "error": str(exc)}
        ) from exc
    if not isinstance(loaded, dict):
        raise IngestionError(f"语义开关文件不是映射结构：{target}", details={"path": str(target)})
    return loaded


def parse_runways_from_semantics(path: Path | None = None) -> tuple[IngestedRunway, ...]:
    """从 `semantics.yaml` 的 S-05 开关读出跑道定义。"""
    doc = load_semantics(path)
    switches = doc.get("switches", {})
    s05 = switches.get("S-05", {}) if isinstance(switches, dict) else {}
    runways = s05.get("runways") if isinstance(s05, dict) else None

    if not isinstance(runways, dict) or not runways:
        raise IngestionError(
            "semantics.yaml 的 S-05 未定义 runways",
            details={"path": str(path or get_settings().SEMANTICS_PATH)},
            suggestions=["S-05.runways 应形如 {RWY-1: {aircraft_types: [JL-8, JL-9]}}"],
        )

    parsed: list[IngestedRunway] = []
    for runway_id, spec in runways.items():
        types = spec.get("aircraft_types") if isinstance(spec, dict) else None
        if not isinstance(types, list) or not types:
            raise IngestionError(
                f"跑道 {runway_id} 未定义 aircraft_types",
                details={"runway_id": str(runway_id), "spec": spec},
            )
        parsed.append(
            IngestedRunway(
                runway_id=str(runway_id),
                name=RUNWAY_NAMES.get(str(runway_id), str(runway_id)),
                aircraft_types=tuple(str(t) for t in types),
            )
        )
    return tuple(sorted(parsed, key=lambda r: r.runway_id))


__all__ = ["RUNWAY_NAMES", "load_semantics", "parse_runways_from_semantics"]
