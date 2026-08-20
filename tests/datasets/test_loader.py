"""加载器的四道校验：哈希、条数、分层、stage 闸。

每一条都用**故意弄坏一份数据**的方式验证 —— 只测「好数据能加载」等于没测，
这些校验存在的意义全在坏数据上。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.datasets.loader import DATASETS_DIR, load_eval_dataset
from backend.datasets.manifest import (
    DatasetIntegrityError,
    load_manifest,
    write_jsonl,
    write_manifest,
)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """把真的 nl_360 复制一份到 tmp，随便怎么弄坏都不碰仓库。"""
    source = DATASETS_DIR / "nl_360" / "v1"
    target = tmp_path / "nl_360" / "v1"
    target.mkdir(parents=True)
    for name in ("items.jsonl", "manifest.json"):
        (target / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _rows(directory: Path) -> list[dict[str, object]]:
    text = (directory / "items.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_loads_clean_copy(sandbox: Path) -> None:
    manifest, items = load_eval_dataset("nl_360", root=sandbox)
    assert len(items) == manifest.item_count == 360


def test_tampered_content_is_rejected(sandbox: Path) -> None:
    """改一个字就要断 —— 这是数据集能跨窗口复用的全部依据。"""
    directory = sandbox / "nl_360" / "v1"
    rows = _rows(directory)
    rows[0]["utterance"] = str(rows[0]["utterance"]) + "！"
    write_jsonl(directory / "items.jsonl", rows)
    with pytest.raises(DatasetIntegrityError, match="SHA256 与清单不符"):
        load_eval_dataset("nl_360", root=sandbox)


def test_count_mismatch_is_rejected(sandbox: Path) -> None:
    directory = sandbox / "nl_360" / "v1"
    rows = _rows(directory)[:-1]
    manifest = load_manifest(directory)
    manifest.sha256 = write_jsonl(directory / "items.jsonl", rows)
    write_manifest(directory, manifest)
    with pytest.raises(DatasetIntegrityError, match="实际 359 条"):
        load_eval_dataset("nl_360", root=sandbox)


def test_strata_mismatch_is_rejected(sandbox: Path) -> None:
    """分层分布是卡片上最容易与内容脱节的一栏，所以做成加载期断言。"""
    directory = sandbox / "nl_360" / "v1"
    manifest = load_manifest(directory)
    manifest.strata = dict(manifest.strata) | {"ambiguous": 59, "adversarial": 61}
    write_manifest(directory, manifest)
    with pytest.raises(DatasetIntegrityError, match="分层分布与清单不符"):
        load_eval_dataset("nl_360", root=sandbox)


def test_duplicate_item_id_is_rejected(sandbox: Path) -> None:
    directory = sandbox / "nl_360" / "v1"
    rows = _rows(directory)
    rows[1]["item_id"] = rows[0]["item_id"]
    manifest = load_manifest(directory)
    manifest.sha256 = write_jsonl(directory / "items.jsonl", rows)
    write_manifest(directory, manifest)
    with pytest.raises(DatasetIntegrityError, match="条目编号重复"):
        load_eval_dataset("nl_360", root=sandbox)


def test_bad_row_reports_line_number(sandbox: Path) -> None:
    """schema 不合时要指出**哪一行**，否则 360 行里找一处笔误没法查。"""
    directory = sandbox / "nl_360" / "v1"
    rows = _rows(directory)
    slots = dict(rows[4]["expected_slots"])  # type: ignore[arg-type]
    slots["persons"] = ["P99"]
    rows[4]["expected_slots"] = slots
    manifest = load_manifest(directory)
    manifest.sha256 = write_jsonl(directory / "items.jsonl", rows)
    write_manifest(directory, manifest)
    with pytest.raises(DatasetIntegrityError, match="第 5 行"):
        load_eval_dataset("nl_360", root=sandbox)


def test_require_approved_blocks_unapproved(sandbox: Path) -> None:
    """未经业务方确认的数据集不许进实验 —— 本窗口的头号规则。"""
    directory = sandbox / "nl_360" / "v1"
    manifest = load_manifest(directory)
    manifest.stage = "draft"
    manifest.approved_by = None
    manifest.approved_at = None
    write_manifest(directory, manifest)
    with pytest.raises(DatasetIntegrityError, match="尚未经业务方确认"):
        load_eval_dataset("nl_360", root=sandbox, require_approved=True)


def test_approved_dataset_passes_the_gate(sandbox: Path) -> None:
    """反面：已确认的数据集能过 `require_approved` 这道闸。

    ★ 在沙箱副本里**显式**置为 approved，不依赖仓库里那份此刻是什么状态 ——
    数据一改，批准就按设计自动失效，拿真实状态当前提的测试会跟着红。
    """
    directory = sandbox / "nl_360" / "v1"
    manifest = load_manifest(directory)
    manifest.stage = "approved"
    manifest.approved_by = "Alps"
    manifest.approved_at = "2026-08-20"
    write_manifest(directory, manifest)

    loaded, items = load_eval_dataset("nl_360", root=sandbox, require_approved=True)
    assert loaded.stage == "approved"
    assert loaded.approved_by == "Alps"
    assert len(items) == 360


def test_approved_without_approver_is_rejected(sandbox: Path) -> None:
    """stage 写成 approved 却没留确认人 —— 那不叫确认过。"""
    directory = sandbox / "nl_360" / "v1"
    manifest = load_manifest(directory)
    manifest.stage = "approved"
    manifest.approved_by = None
    manifest.approved_at = None
    write_manifest(directory, manifest)
    with pytest.raises(DatasetIntegrityError, match="没有确认人"):
        load_eval_dataset("nl_360", root=sandbox)


def test_unknown_dataset_name(sandbox: Path) -> None:
    with pytest.raises(KeyError, match="未登记的数据集"):
        load_eval_dataset("nope", root=sandbox)
