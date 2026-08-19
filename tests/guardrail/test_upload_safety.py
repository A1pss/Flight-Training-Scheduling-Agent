"""文件上传安全闸（v6 §11.5「文件上传」）。

> 扩展名白名单 + MIME 嗅探双验、大小上限 50MB、压缩炸弹检测、上传目录不可执行

四道闸各有一组用例。**每一道都要有正反两面**：只测「坏的被拒」而不测「好的能过」，
一个「什么都拒绝」的实现也能全绿；反过来同理。

## 为什么扩展名与嗅探要**双验**且必须一致

扩展名是调用方说的，嗅探是我们自己看的。只信前者 → 一个 `.csv` 后缀的 zip 就能
把 zip 处理器骗进来；只信后者 → 白名单形同虚设（谁都能传任意二进制，反正会被嗅探
成 octet-stream）。**两者不一致直接拒收**，这是投递面最窄的做法。
"""

from __future__ import annotations

import io
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.routers.ingest import DIR_MODE, FILE_MODE, harden_upload_dir
from backend.core.errors import ErrorCode, IngestionError
from backend.ingestion.safety import (
    EXTENSION_WHITELIST,
    MAX_ARCHIVE_ENTRIES,
    MAX_COMPRESSION_RATIO,
    MAX_UPLOAD_BYTES,
    check_zip_bomb,
    screen_file,
    sniff_media_type,
)
from tests.fixtures.api_fixtures import (
    SCHEDULER,
    RecordingRunner,
    RecordingSessionFactory,
    build_test_app,
    make_settings,
)

pytestmark = pytest.mark.guardrail

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


# ═════════════════════════════════════════════════════════════════════
# ① 扩展名白名单
# ═════════════════════════════════════════════════════════════════════
def test_whitelist_is_a_closed_set() -> None:
    """白名单是**封闭集合**，不是「除了这些都不行」的黑名单。"""
    assert set(EXTENSION_WHITELIST) == {".pdf", ".xlsx", ".docx", ".csv", ".png", ".jpg", ".jpeg"}


@pytest.mark.parametrize("suffix", [".exe", ".sh", ".py", ".zip", ".tar", ".svg", ""])
def test_extension_outside_the_whitelist_is_rejected(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"payload{suffix}"
    path.write_bytes(PDF_BYTES)
    with pytest.raises(IngestionError) as excinfo:
        screen_file(path)
    assert excinfo.value.code == ErrorCode.PDF_REPAIR_ASSERTION_FAILED
    assert "白名单" in str(excinfo.value)


def test_whitelisted_pdf_passes(tmp_path: Path) -> None:
    path = tmp_path / "personnel.pdf"
    path.write_bytes(PDF_BYTES)
    safe = screen_file(path)
    assert safe.media_type == "application/pdf"
    assert safe.extension == ".pdf"
    assert safe.sha256


# ═════════════════════════════════════════════════════════════════════
# ② MIME 嗅探双验
# ═════════════════════════════════════════════════════════════════════
def test_sniffing_ignores_the_declared_extension(tmp_path: Path) -> None:
    """★ 典型投递面：`.csv` 后缀的 zip。扩展名过了白名单，嗅探把它拆穿。"""
    path = tmp_path / "roster.csv"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("payload.sh", "#!/bin/sh\necho pwned\n")
    path.write_bytes(buffer.getvalue())

    assert sniff_media_type(path) == "application/zip"
    with pytest.raises(IngestionError):
        screen_file(path)


def test_png_bytes_named_pdf_are_rejected(tmp_path: Path) -> None:
    """反过来也一样：两个都在白名单里，但**不一致**就是拒收。"""
    path = tmp_path / "aircraft.pdf"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    assert sniff_media_type(path) == "image/png"
    with pytest.raises(IngestionError):
        screen_file(path)


def test_sniffer_recognises_ooxml_containers(tmp_path: Path) -> None:
    """xlsx/docx 都是 zip，靠内部目录结构区分 —— 不能只看 `PK\\x03\\x04`。"""
    path = tmp_path / "plan.xlsx"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("xl/workbook.xml", "<workbook/>")
    path.write_bytes(buffer.getvalue())
    assert sniff_media_type(path).endswith("spreadsheetml.sheet")
    assert screen_file(path).extension == ".xlsx"


# ═════════════════════════════════════════════════════════════════════
# ③ 大小上限 50MB
# ═════════════════════════════════════════════════════════════════════
def test_limit_is_fifty_megabytes() -> None:
    assert MAX_UPLOAD_BYTES == 50 * 1024 * 1024


def test_oversized_file_is_rejected(tmp_path: Path) -> None:
    """用一个很小的 `max_bytes` 触发同一条判据 —— 不必真写 50MB 到盘上。"""
    path = tmp_path / "big.pdf"
    path.write_bytes(PDF_BYTES * 10)
    with pytest.raises(IngestionError) as excinfo:
        screen_file(path, max_bytes=16)
    assert "上限" in str(excinfo.value) or "超过" in str(excinfo.value)


def test_endpoint_enforces_the_configured_limit() -> None:
    """端点层也有一道（在存盘之前），配置项是 `UPLOAD_MAX_BYTES`。"""
    app, _ = build_test_app(
        settings=make_settings(UPLOAD_MAX_BYTES=32),
        runner=RecordingRunner(),
        session_factory=RecordingSessionFactory(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/ingest",
        headers=SCHEDULER,
        files={"files": ("big.pdf", PDF_BYTES * 4, "application/pdf")},
    )
    assert response.status_code == 400
    assert response.json()["code"] == ErrorCode.PDF_REPAIR_ASSERTION_FAILED.value


def test_empty_file_is_rejected_not_silently_skipped() -> None:
    """空文件**阻断**，不「跳过这一份继续处理其它的」（铁律 7）。"""
    app, _ = build_test_app(
        settings=make_settings(),
        runner=RecordingRunner(),
        session_factory=RecordingSessionFactory(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/ingest",
        headers=SCHEDULER,
        files={"files": ("empty.pdf", b"", "application/pdf")},
    )
    assert response.status_code == 400
    assert "空文件" in response.json()["message"]


# ═════════════════════════════════════════════════════════════════════
# ④ 压缩炸弹
# ═════════════════════════════════════════════════════════════════════
def test_high_compression_ratio_is_rejected(tmp_path: Path) -> None:
    """经典 zip bomb：一个高度可压缩的大文件。"""
    path = tmp_path / "bomb.xlsx"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", "0" * (4 * 1024 * 1024))
    path.write_bytes(buffer.getvalue())
    with pytest.raises(IngestionError) as excinfo:
        check_zip_bomb(path)
    assert "压缩比" in str(excinfo.value)


def test_too_many_entries_is_rejected(tmp_path: Path) -> None:
    """条目数炸弹：解压比正常，但几万个小文件同样能拖垮处理端。"""
    path = tmp_path / "many.xlsx"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for i in range(MAX_ARCHIVE_ENTRIES + 1):
            zf.writestr(f"e{i}.txt", "x")
    path.write_bytes(buffer.getvalue())
    with pytest.raises(IngestionError) as excinfo:
        check_zip_bomb(path)
    assert "条目数" in str(excinfo.value)


def test_a_normal_workbook_is_not_flagged(tmp_path: Path) -> None:
    """正常 xlsx 不许被误判 —— 否则大家会开始想办法绕过这道闸。"""
    path = tmp_path / "normal.xlsx"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", "<workbook>" + "abc" * 100 + "</workbook>")
    path.write_bytes(buffer.getvalue())
    check_zip_bomb(path)


def test_non_archive_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "plain.pdf"
    path.write_bytes(PDF_BYTES)
    check_zip_bomb(path)


def test_ratio_limit_is_documented_as_a_number() -> None:
    assert MAX_COMPRESSION_RATIO == 200.0


# ═════════════════════════════════════════════════════════════════════
# ⑤ 上传目录不可执行
# ═════════════════════════════════════════════════════════════════════
def test_hardened_directory_has_no_group_or_other_bits(tmp_path: Path) -> None:
    directory = tmp_path / "uploads" / "ing_x"
    directory.mkdir(parents=True)
    harden_upload_dir(directory)
    mode = stat.S_IMODE(directory.stat().st_mode)
    assert mode == DIR_MODE == 0o700
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO), "组与其他人不该有任何位"


def test_stored_upload_has_no_execute_bit(tmp_path: Path) -> None:
    """★ 出口标准那一条：落盘的上传件**任何人都不可执行**。"""
    app, _ = build_test_app(
        settings=make_settings(PLANS_DIR=tmp_path / "plans"),
        runner=RecordingRunner(),
        session_factory=RecordingSessionFactory(),
    )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/v1/ingest",
        headers=SCHEDULER,
        files={"files": ("personnel.pdf", PDF_BYTES, "application/pdf")},
    )
    assert response.status_code == 202, response.text

    stored = sorted((tmp_path / "uploads").rglob("*.pdf"))
    assert stored, "上传件没落盘"
    for item in stored:
        mode = stat.S_IMODE(item.stat().st_mode)
        assert mode == FILE_MODE == 0o600
        assert not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH), (
            f"{item} 带执行位 —— 上传的是数据不是程序"
        )
    for directory in {item.parent for item in stored}:
        assert stat.S_IMODE(directory.stat().st_mode) == DIR_MODE
