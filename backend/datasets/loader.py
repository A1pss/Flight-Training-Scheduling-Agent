"""数据集加载器：**校验通过才返回条目**。

`CLAUDE.md` 对本窗口的统一要求是「每集都要有 loader 与 schema 校验，加载时
校验通过才允许用」。所以这里没有 `strict=False` 之类的旁路开关 —— 一份坏掉的
数据集应该在加载处炸掉，而不是带着两条脏标注一路跑到验收报告里。

三道校验，顺序固定：

1. **清单存在且自洽**（分层求和 = 条数；`approved` 必须有确认人）；
2. **文件哈希与清单一致**（数据被手改过就在这里断）；
3. **逐条过 Pydantic**（字段、枚举、实体编号形态）。

`require_approved=True` 是第四道：实验代码取数时带上它，就不可能误用一份
还没经业务方确认的样例集。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from backend.datasets.manifest import (
    DatasetIntegrityError,
    DatasetManifest,
    load_manifest,
    verify_manifest,
)
from backend.datasets.schemas import (
    DatasetItem,
    GoldenCaseItem,
    MemoryItem,
    NLItem,
    OodItem,
    PlanScenarioItem,
    ToolCallItem,
    TrajectoryItem,
)

#: 仓库根。本文件位于 `<root>/backend/datasets/loader.py`。
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
DATASETS_DIR: Final[Path] = REPO_ROOT / "datasets"

#: 数据集名 → 条目模型。九集逐个接入时在这里登记，登记之后
#: `load_eval_dataset(name)` 才认它 —— 目录里凭空多出一份没登记的数据不会被静默使用。
REGISTRY: Final[dict[str, type[DatasetItem]]] = {
    "nl_360": NLItem,
    "memory_320": MemoryItem,
    "trajectory_100": TrajectoryItem,
    "tool_calls_200": ToolCallItem,
    "plan_scenarios": PlanScenarioItem,
    "golden_40": GoldenCaseItem,
    "ood_200": OodItem,
}


def dataset_dir(name: str, version: str = "v1") -> Path:
    return DATASETS_DIR / name / version


def _iter_items(path: Path) -> Iterator[tuple[int, dict[str, object]]]:
    """逐条读条目。支持两种载体：

    - `.jsonl` —— 一行一条（新造的数据集都用它，diff 可读）
    - `.json` —— 一个 JSON 数组（`plan_scenarios` 是 W4 就落成这个形态的，
      **不为了统一格式去复制一份 138KB 的数据**；行号按数组下标算）
    """
    if path.suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise DatasetIntegrityError(f"{path.name} 不是 JSON 数组")
        yield from enumerate(payload, start=1)
        return
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            yield lineno, json.loads(text)


def load_eval_dataset(
    name: str,
    *,
    version: str = "v1",
    require_approved: bool = False,
    root: Path | None = None,
) -> tuple[DatasetManifest, list[DatasetItem]]:
    """加载一份数据集，返回 `(清单, 条目列表)`。

    > 名字里的 `eval_` 不是修饰，是**为了避开 bandit B615**：该规则按函数名识别
    > HuggingFace 的 `load_dataset`，重名就会被判成「未固定 revision 的模型下载」。
    > 改名比在每个调用点写 `# nosec` 好 —— 后者会把真正的 B615 一起静音。
    """
    if name not in REGISTRY:
        raise KeyError(f"未登记的数据集 {name!r}；已登记：{sorted(REGISTRY)}")
    model = REGISTRY[name]
    directory = (root or DATASETS_DIR) / name / version
    manifest = load_manifest(directory)
    if manifest.name != name or manifest.version != version:
        raise DatasetIntegrityError(
            f"清单自称 {manifest.name}/{manifest.version}，实际位于 {name}/{version}"
        )
    rows = list(_iter_items(directory / manifest.items_file))
    verify_manifest(directory, manifest, line_count=len(rows))
    if require_approved and manifest.stage != "approved":
        raise DatasetIntegrityError(
            f"{name}/{version} 当前 stage={manifest.stage}，尚未经业务方确认，不得用于实验"
        )

    items: list[DatasetItem] = []
    for lineno, raw in rows:
        try:
            items.append(model.model_validate(raw))
        except ValidationError as exc:
            raise DatasetIntegrityError(
                f"{name}/{version} 第 {lineno} 行不合契约：\n{exc}"
            ) from exc
    _assert_unique_ids(name, items)
    _assert_strata(name, manifest, items)
    return manifest, items


def _assert_unique_ids(name: str, items: list[DatasetItem]) -> None:
    seen: set[str] = set()
    for item in items:
        item_id = item.item_id
        if item_id in seen:
            raise DatasetIntegrityError(f"{name}：条目编号重复 {item_id}")
        seen.add(item_id)


def _assert_strata(name: str, manifest: DatasetManifest, items: list[DatasetItem]) -> None:
    """清单里的分层分布必须与实际条目对得上。

    分层分布是数据集卡片上最容易与内容脱节的一栏 —— 加两条忘了改数字，
    验收报告里那张分布表就是错的。这里直接把它变成加载期断言。
    """
    if not manifest.strata:
        return
    field = stratum_field(items)
    if field is None:
        return
    actual: dict[str, int] = {}
    for item in items:
        key = str(getattr(item, field))
        actual[key] = actual.get(key, 0) + 1
    if actual != manifest.strata:
        raise DatasetIntegrityError(
            f"{name}：分层分布与清单不符\n  清单 = {manifest.strata}\n  实际 = {actual}"
        )


def stratum_field(items: list[DatasetItem]) -> str | None:
    """各集的分层字段名不同（nl 用 `layer`，后续几集用 `stratum` / `flow`）。"""
    if not items:
        return None
    for candidate in ("layer", "memory_type", "flow", "stratum", "category", "status"):
        if hasattr(items[0], candidate):
            return candidate
    return None


__all__ = [
    "DATASETS_DIR",
    "REGISTRY",
    "REPO_ROOT",
    "dataset_dir",
    "load_eval_dataset",
    "stratum_field",
]
