"""命名规范与版本分配（v6 §10.6）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from backend.report.naming import (
    NAME_RE,
    PlanName,
    allocate_name,
    existing_versions,
    next_version,
    parse_name,
    read_ledger,
    week_dir,
)
from tests.fixtures.report_bundle import GENERATED_AT, sample_bundle

EXAMPLE = "FTP_NAU_WEEKLY_2026W02_20260105-20260111_v3_APPROVED_7f3a9c21.xlsx"


def test_v6_example_filename_parses() -> None:
    """v6 §10.6 的两个示例文件名必须被本实现认得。"""
    name = parse_name(EXAMPLE)
    assert (name.org, name.plan_type, name.iso_week) == ("NAU", "WEEKLY", "2026W02")
    assert (name.week_start, name.week_end) == ("20260105", "20260111")
    assert (name.version, name.status, name.hash8) == (3, "APPROVED", "7f3a9c21")
    assert name.xlsx == EXAMPLE
    other = parse_name("FTP_NAU_RESCHED_2026W02_20260105-20260111_v4_PENDING_b12d55e0.xlsx")
    assert (other.plan_type, other.version, other.status) == ("RESCHED", 4, "PENDING")


def test_sibling_file_names_follow_the_archive_layout() -> None:
    name = parse_name(EXAMPLE)
    stem = "FTP_NAU_WEEKLY_2026W02_20260105-20260111_v3_APPROVED_7f3a9c21"
    assert name.json == f"{stem}.json"
    assert name.manifest == f"{stem}.manifest.yaml"
    assert name.validation_report == "validation_report_v3.json"
    assert name.solver_log == "solver_log_v3.txt"


@pytest.mark.parametrize(
    "bad",
    [
        "FTP_NAU_WEEKLY_2026W02_20260105-20260111_v3_APPROVED.xlsx",  # 缺指纹
        "FTP_NAU_WEEKLY_2026W2_20260105-20260111_v3_APPROVED_7f3a9c21.xlsx",  # 周号 1 位
        "FTP_NAU_UNKNOWN_2026W02_20260105-20260111_v3_APPROVED_7f3a9c21.xlsx",  # 类型非法
        "FTP_NAU_WEEKLY_2026W02_20260105-20260111_v3_DONE_7f3a9c21.xlsx",  # 状态非法
        "计划_2026W02.xlsx",
    ],
)
def test_illegal_names_are_rejected(bad: str) -> None:
    assert not NAME_RE.match(bad.removesuffix(".xlsx"))
    with pytest.raises(ValueError, match="命名规范"):
        parse_name(bad)


def test_week_dir_follows_iso_week(tmp_path: Path) -> None:
    assert week_dir("2026W02", root=tmp_path) == tmp_path / "2026" / "W02"
    with pytest.raises(ValueError, match="ISO 周"):
        week_dir("2026-W2", root=tmp_path)


def test_versions_increase_within_a_week(tmp_path: Path) -> None:
    bundle = sample_bundle()
    got = [allocate_name(bundle, root=tmp_path, now=GENERATED_AT).version for _ in range(3)]
    assert got == [1, 2, 3]


def test_version_is_never_reused_even_after_the_file_is_deleted(tmp_path: Path) -> None:
    """台账只增不改 —— 删掉 v1 的文件也不会把 v1 这个号放回池子里。"""
    bundle = sample_bundle()
    first = allocate_name(bundle, root=tmp_path, now=GENERATED_AT)
    directory = week_dir("2026W02", root=tmp_path)
    (directory / first.xlsx).write_bytes(b"stub")
    assert existing_versions(directory) == (1,)

    (directory / first.xlsx).unlink()
    assert existing_versions(directory) == ()  # 目录里已经没有它了
    assert next_version(directory) == 2  # 但台账记得

    second = allocate_name(bundle, root=tmp_path, now=GENERATED_AT)
    assert second.version == 2
    assert [e.version for e in read_ledger(directory)] == [1, 2]


def test_ledger_records_plan_id_and_fingerprint(tmp_path: Path) -> None:
    bundle = sample_bundle()
    name = allocate_name(bundle, root=tmp_path, now=GENERATED_AT)
    entry = read_ledger(week_dir("2026W02", root=tmp_path))[0]
    assert entry.plan_id == bundle.plan.plan_id
    assert entry.content_sha256 == bundle.plan.content_sha256
    assert entry.filename == name.xlsx
    assert entry.allocated_at == GENERATED_AT.isoformat()


def test_stray_files_do_not_break_version_allocation(tmp_path: Path) -> None:
    directory = week_dir("2026W02", root=tmp_path)
    directory.mkdir(parents=True)
    (directory / "笔记.xlsx").write_bytes(b"stub")
    (directory / "FTP_NAU_WEEKLY_2026W02_20260105-20260111_v7_DRAFT_deadbeef.xlsx").write_bytes(
        b"stub"
    )
    assert next_version(directory) == 8


def test_hash8_comes_from_the_content_fingerprint() -> None:
    bundle = sample_bundle()
    assert bundle.content_fingerprint == bundle.plan.content_sha256[:8]
    name = PlanName(
        org="NAU",
        plan_type="WEEKLY",
        iso_week=bundle.plan.iso_week,
        week_start=bundle.plan.week_start.strftime("%Y%m%d"),
        week_end=bundle.plan.week_end.strftime("%Y%m%d"),
        version=1,
        status="DRAFT",
        hash8=bundle.content_fingerprint,
    )
    assert parse_name(name.xlsx).hash8 == bundle.plan.content_sha256[:8]


def test_allocation_timestamp_is_the_one_passed_in(tmp_path: Path) -> None:
    """时间戳来自调用方，不是 `datetime.now()` —— 报告要逐字节可复现（铁律 9）。"""
    stamp = datetime.fromisoformat("2026-01-02T10:22:41+08:00")
    allocate_name(sample_bundle(), root=tmp_path, now=stamp)
    assert read_ledger(week_dir("2026W02", root=tmp_path))[0].allocated_at == stamp.isoformat()
