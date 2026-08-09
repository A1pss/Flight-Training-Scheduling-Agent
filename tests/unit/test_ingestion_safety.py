"""安全闸单测（v6 §5.1）。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from backend.core.config import PROJECT_ROOT
from backend.core.errors import IngestionError
from backend.ingestion.safety import (
    EXTENSION_WHITELIST,
    MAX_ARCHIVE_ENTRIES,
    check_zip_bomb,
    screen_file,
    sniff_media_type,
)

ORIGIN = PROJECT_ROOT / "data" / "origin"


def test_real_pdf_passes_the_gate() -> None:
    safe = screen_file(ORIGIN / "personnel.pdf")
    assert safe.media_type == "application/pdf"
    assert safe.extension == ".pdf"
    assert len(safe.sha256) == 64
    assert safe.size_bytes > 0


def test_sniff_detects_pdf_and_png() -> None:
    assert sniff_media_type(ORIGIN / "personnel.pdf") == "application/pdf"
    assert sniff_media_type(ORIGIN / "image 1.png") == "image/png"


def test_extension_not_in_whitelist_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "payload.exe"
    target.write_bytes(b"MZ\x90\x00")
    with pytest.raises(IngestionError, match="不在白名单"):
        screen_file(target)


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(IngestionError, match="不存在"):
        screen_file(tmp_path / "nope.pdf")


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "empty.pdf"
    target.write_bytes(b"")
    with pytest.raises(IngestionError, match="为空"):
        screen_file(target)


def test_size_limit_is_enforced(tmp_path: Path) -> None:
    target = tmp_path / "big.pdf"
    target.write_bytes(b"%PDF-1.4" + b"0" * 1024)
    with pytest.raises(IngestionError, match="超过上限"):
        screen_file(target, max_bytes=64)


def test_extension_spoofing_is_caught(tmp_path: Path) -> None:
    """改后缀绕不过去 —— 一个 `.pdf` 后缀的 zip 必须被拒。"""
    target = tmp_path / "fake.pdf"
    with zipfile.ZipFile(target, "w") as zf:
        zf.writestr("a.txt", "hello")
    with pytest.raises(IngestionError, match="MIME 嗅探结果"):
        screen_file(target)


def test_csv_must_be_decodable_text(tmp_path: Path) -> None:
    good = tmp_path / "ok.csv"
    good.write_text("a,b\n1,2\n", encoding="utf-8")
    assert screen_file(good).media_type == "text/csv"

    bad = tmp_path / "bad.csv"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01")
    with pytest.raises(IngestionError, match="不是可解码文本"):
        screen_file(bad)


def test_zip_bomb_ratio_is_detected(tmp_path: Path) -> None:
    target = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", "<x/>")
        zf.writestr("payload.bin", b"\x00" * (20 * 1024 * 1024))
    with pytest.raises(IngestionError, match="压缩比"):
        check_zip_bomb(target)


def test_zip_bomb_entry_count_is_detected(tmp_path: Path) -> None:
    target = tmp_path / "many.xlsx"
    with zipfile.ZipFile(target, "w") as zf:
        for i in range(MAX_ARCHIVE_ENTRIES + 1):
            zf.writestr(f"f{i}.txt", "x")
    with pytest.raises(IngestionError, match="条目数"):
        check_zip_bomb(target)


def test_check_zip_bomb_ignores_non_zip() -> None:
    check_zip_bomb(ORIGIN / "personnel.pdf")  # 不该抛


def test_whitelist_covers_the_formats_v6_names() -> None:
    """v6 §5.1 点名的格式：PDF / XLSX / CSV / DOCX / 图片。"""
    assert {".pdf", ".xlsx", ".csv", ".docx", ".png", ".jpg"} <= set(EXTENSION_WHITELIST)
