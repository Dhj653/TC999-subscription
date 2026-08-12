"""
压缩包内存解析：支持 zip 与 7z，加密/损坏包自动跳过。
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from ..utils.logger import get_logger

log = get_logger()

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".webm"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


@dataclass
class ArchiveEntry:
    archive_path: str
    in_archive_name: str
    size: int
    is_video: bool
    is_image: bool


def _is_video(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXTS


def _is_image(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTS


def is_archive(path: str) -> bool:
    return Path(path).suffix.lower() in {".zip", ".7z"}


def list_entries(archive_path: str) -> list[ArchiveEntry]:
    """枚举压缩包内视频/图片条目（不解压内容）。"""
    entries: list[ArchiveEntry] = []
    ext = Path(archive_path).suffix.lower()
    try:
        if ext == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    is_v = _is_video(name)
                    is_i = _is_image(name)
                    if not (is_v or is_i):
                        continue
                    entries.append(ArchiveEntry(archive_path, name, info.file_size, is_v, is_i))
        elif ext == ".7z":
            try:
                import py7zr
            except ImportError:
                log.error("缺少 py7zr，无法解析 7z：pip install py7zr")
                return entries
            with py7zr.SevenZipFile(archive_path, mode="r") as sz:
                for info in sz.list():
                    name = info.filename
                    is_v = _is_video(name)
                    is_i = _is_image(name)
                    if not (is_v or is_i):
                        continue
                    entries.append(
                        ArchiveEntry(archive_path, name,
                                     getattr(info, "uncompressed", 0) or 0,
                                     is_v, is_i)
                    )
        else:
            log.warning("不支持的压缩格式: %s", archive_path)
    except (zipfile.BadZipFile, RuntimeError) as e:
        log.warning("压缩包损坏或加密，跳过: %s (%s)", archive_path, e)
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "password" in msg or "encrypted" in msg or "crypto" in msg:
            log.warning("加密压缩包，跳过并记录: %s", archive_path)
        else:
            log.warning("压缩包解析失败，跳过: %s (%s)", archive_path, e)
    return entries


def read_entry_bytes(archive_path: str, in_archive_name: str) -> Optional[bytes]:
    """读取压缩包内单个条目的字节流到内存。兼容 py7zr 0.x/1.x。"""
    ext = Path(archive_path).suffix.lower()
    try:
        if ext == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                with zf.open(in_archive_name, "r") as f:
                    return f.read()
        elif ext == ".7z":
            import py7zr
            with py7zr.SevenZipFile(archive_path, mode="r") as sz:
                if hasattr(sz, "read"):
                    data = sz.read([in_archive_name])
                    bio = data.get(in_archive_name)
                    if bio is None:
                        return None
                    return bio.read()
                tmpdir = tempfile.mkdtemp(prefix="facevid_7z_")
                try:
                    sz.extract(targets=[in_archive_name], path=tmpdir)
                    extracted = os.path.join(tmpdir, in_archive_name)
                    if not os.path.exists(extracted):
                        for root, _, files in os.walk(tmpdir):
                            if os.path.basename(in_archive_name) in files:
                                extracted = os.path.join(root, os.path.basename(in_archive_name))
                                break
                    if os.path.exists(extracted):
                        with open(extracted, "rb") as f:
                            return f.read()
                    return None
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
    except RuntimeError as e:
        msg = str(e).lower()
        if "password" in msg or "encrypted" in msg:
            log.warning("加密条目，跳过: %s :: %s", archive_path, in_archive_name)
            return None
        log.warning("读取压缩条目失败: %s :: %s (%s)", archive_path, in_archive_name, e)
    except Exception as e:  # noqa: BLE001
        msg = str(e).lower()
        if "password" in msg or "encrypted" in msg or "crypto" in msg:
            log.warning("加密条目，跳过: %s :: %s", archive_path, in_archive_name)
            return None
        log.warning("读取压缩条目异常: %s :: %s (%s)", archive_path, in_archive_name, e)
    return None
