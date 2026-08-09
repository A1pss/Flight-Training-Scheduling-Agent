"""安全闸（v6 §5.1 第一段）：扩展名白名单 · MIME 嗅探 · 50MB 上限 · 压缩炸弹检测。

**MIME 嗅探自己做，不引 `python-magic`**：那个包要拖 libmagic 的 C 库，
在「全离线内网裸装」这个部署形态下多一个系统级依赖就多一个装不上的理由。
我们只需要判别六种格式，读前 8 字节的魔数就够，且判别逻辑本身可测。

**扩展名不可信**：白名单只是第一道；真正决定后续走哪个适配器的是嗅探结果。
两者不一致直接拒收 —— 一个 `.csv` 后缀的 zip 是典型的投递面。
"""

from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from backend.core.errors import IngestionError

#: 允许上传的扩展名 → 期望的媒体类型
EXTENSION_WHITELIST: Final[dict[str, str]] = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

#: 魔数 → 媒体类型。zip 容器（xlsx/docx）再按内部条目细分。
_MAGIC: Final[tuple[tuple[bytes, str], ...]] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"PK\x03\x04", "application/zip"),
)

#: 50MB 上限（v6 §5.1）
MAX_UPLOAD_BYTES: Final[int] = 50 * 1024 * 1024
#: 压缩炸弹判定：解压后总字节上限
MAX_UNCOMPRESSED_BYTES: Final[int] = 500 * 1024 * 1024
#: 压缩炸弹判定：解压比上限
MAX_COMPRESSION_RATIO: Final[float] = 200.0
#: 压缩炸弹判定：条目数上限
MAX_ARCHIVE_ENTRIES: Final[int] = 5_000


@dataclass(frozen=True)
class SafeFile:
    """通过安全闸的文件。`media_type` 是**嗅探结果**，不是按后缀猜的。"""

    path: Path
    size_bytes: int
    sha256: str
    media_type: str
    extension: str


def sniff_media_type(path: Path) -> str:
    """按魔数嗅探媒体类型。zip 容器进一步区分 xlsx / docx / 普通 zip。"""
    with path.open("rb") as fh:
        head = fh.read(8)
    for magic, media_type in _MAGIC:
        if head.startswith(magic):
            if media_type != "application/zip":
                return media_type
            return _sniff_zip_container(path)
    # 没有魔数的文本类：只要能按 UTF-8 解出来就当 CSV/纯文本
    try:
        path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return "application/octet-stream"
    return "text/csv"


def _sniff_zip_container(path: Path) -> str:
    """OOXML 容器靠内部目录结构区分。"""
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return "application/zip"
    if "xl/workbook.xml" in names:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if "word/document.xml" in names:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return "application/zip"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_zip_bomb(path: Path) -> None:
    """压缩炸弹检测。三条独立判据，任一超限即拒收。"""
    if not zipfile.is_zipfile(path):
        return
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise IngestionError(
                f"压缩包条目数 {len(infos)} 超过上限 {MAX_ARCHIVE_ENTRIES}，疑似压缩炸弹",
                details={"entries": len(infos), "limit": MAX_ARCHIVE_ENTRIES},
            )
        total_uncompressed = sum(i.file_size for i in infos)
        total_compressed = sum(i.compress_size for i in infos)
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            raise IngestionError(
                f"解压后体积 {total_uncompressed} 字节超过上限 "
                f"{MAX_UNCOMPRESSED_BYTES}，疑似压缩炸弹",
                details={"uncompressed": total_uncompressed, "limit": MAX_UNCOMPRESSED_BYTES},
            )
        if total_compressed > 0:
            ratio = total_uncompressed / total_compressed
            if ratio > MAX_COMPRESSION_RATIO:
                raise IngestionError(
                    f"压缩比 {ratio:.1f}:1 超过上限 {MAX_COMPRESSION_RATIO:.0f}:1，疑似压缩炸弹",
                    details={"ratio": round(ratio, 2), "limit": MAX_COMPRESSION_RATIO},
                )


def screen_file(path: Path, *, max_bytes: int = MAX_UPLOAD_BYTES) -> SafeFile:
    """安全闸主入口。任一条不过就抛 `IngestionError`（FTS-1003）阻断。"""
    if not path.is_file():
        raise IngestionError(f"文件不存在或不是普通文件：{path}", details={"path": str(path)})

    extension = path.suffix.lower()
    if extension not in EXTENSION_WHITELIST:
        raise IngestionError(
            f"扩展名 {extension or '(无)'} 不在白名单内",
            details={"extension": extension, "whitelist": sorted(EXTENSION_WHITELIST)},
            suggestions=[f"支持的格式：{'、'.join(sorted(EXTENSION_WHITELIST))}"],
        )

    size_bytes = path.stat().st_size
    if size_bytes > max_bytes:
        raise IngestionError(
            f"文件体积 {size_bytes} 字节超过上限 {max_bytes} 字节",
            details={"size_bytes": size_bytes, "limit": max_bytes},
        )
    if size_bytes == 0:
        raise IngestionError("文件为空", details={"path": str(path)})

    media_type = sniff_media_type(path)
    expected = EXTENSION_WHITELIST[extension]
    # CSV 没有魔数，嗅探只能确认「是可解码文本」；其余格式必须与后缀一致。
    if expected != "text/csv" and media_type != expected:
        raise IngestionError(
            f"MIME 嗅探结果 {media_type} 与扩展名 {extension} 期望的 {expected} 不一致",
            details={"sniffed": media_type, "extension": extension, "expected": expected},
            suggestions=["改扩展名不能绕过本检查；请确认文件本身格式正确"],
        )
    if expected == "text/csv" and media_type != "text/csv":
        raise IngestionError(
            f".csv 文件的嗅探结果为 {media_type}，不是可解码文本",
            details={"sniffed": media_type},
        )

    check_zip_bomb(path)

    return SafeFile(
        path=path,
        size_bytes=size_bytes,
        sha256=_sha256(path),
        media_type=media_type,
        extension=extension,
    )


__all__ = [
    "EXTENSION_WHITELIST",
    "MAX_ARCHIVE_ENTRIES",
    "MAX_COMPRESSION_RATIO",
    "MAX_UNCOMPRESSED_BYTES",
    "MAX_UPLOAD_BYTES",
    "SafeFile",
    "check_zip_bomb",
    "screen_file",
    "sniff_media_type",
]
