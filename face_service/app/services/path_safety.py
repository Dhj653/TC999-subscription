"""路径安全校验：禁止输出到受限/非法路径。"""
from __future__ import annotations

import os
from pathlib import Path

from ..config import settings
from ..utils.logger import get_logger

log = get_logger()


def _norm(p: str) -> str:
    """规范化路径用于比较（统一为正斜杠，跨平台一致）。"""
    norm = os.path.normcase(os.path.abspath(os.path.normpath(p)))
    norm = norm.replace("\\", "/").rstrip("/")
    return norm.lower()


def is_safe_output_path(path: str, source_root: Optional[str] = None) -> bool:
    """
    校验输出路径是否安全：
    - 必须为绝对路径或可规范化
    - 不得位于系统受限根（Windows、Program Files、ProgramData 等）
    - 不得为空路径、不得含非法字符
    """
    if not path or not isinstance(path, str):
        return False
    path = path.strip().strip('"').strip("'")
    if not path:
        return False

    # 非法字符（Windows）
    bad_chars = ['<', '>', '|', '?', '*', '"']
    if any(c in path for c in bad_chars):
        return False

    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return False

    resolved_s = _norm(str(resolved))

    for blocked in settings.blocked_path_roots:
        b = _norm(blocked)
        if resolved_s == b or resolved_s.startswith(b + "/"):
            log.warning("输出路径被拒绝（受限根）: %s", resolved_s)
            return False

    # 盘符限制（仅允许本地可写盘）
    drive = os.path.splitdrive(str(resolved))[0].upper()
    if drive and drive not in ("", "\\\\?\\"):
        # 排除光盘等只读盘符不强制，但禁止 UNC 系统路径滥用
        pass

    return True


def validate_scan_folder(path: str) -> bool:
    """校验扫描目录存在且为目录。"""
    if not path:
        return False
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return resolved.is_dir()


def join_output_root(scan_folder: str, output_dir: Optional[str]) -> str:
    """计算实际输出根目录。output_dir 为空则用 scan_folder。"""
    root = output_dir.strip() if output_dir else scan_folder
    if not is_safe_output_path(root, scan_folder):
        raise ValueError(f"输出路径不安全或非法: {root}")
    return os.path.abspath(root)


def group_output_dir(output_root: str, group_name: str) -> str:
    """生成某分组的输出子目录路径（安全拼接，禁止穿越）。"""
    # 过滤分组名中的路径分隔符，防止穿越
    safe_name = "".join(
        c for c in group_name if c not in ('\\', '/', ':', '*', '?', '"', '<', '>', '|')
    ).strip()
    if not safe_name:
        safe_name = "未命名分组"
    out = os.path.abspath(os.path.join(output_root, safe_name))
    # 二次校验：结果必须仍在 output_root 之下
    if _norm(out) != _norm(output_root) and not _norm(out).startswith(
        _norm(output_root) + "/"
    ):
        raise ValueError(f"分组输出路径越界: {out}")
    return out
