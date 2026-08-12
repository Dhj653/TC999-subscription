"""
路径安全校验：禁止输出到系统目录、禁止跳出源目录（防止 ../../etc/passwd 攻击）。
新增：group_output_dir 在 output_root 下按角色名建子目录。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from ..config import settings
from ..utils.logger import get_logger

log = get_logger()


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _normalize_root(p: str) -> str:
    return os.path.abspath(p).replace("\\", "/").rstrip("/") + "/"


def validate_scan_folder(path: Optional[str]) -> bool:
    if not path:
        return False
    try:
        pp = Path(path).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return False
    return pp.is_dir()


def is_safe_output_path(output_dir: str, scan_folder: str) -> bool:
    """判断输出目录是否安全：不在黑名单根下，且路径合法。"""
    if not output_dir:
        return False
    try:
        out = Path(output_dir).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return False

    out_str = _normalize_root(str(out))
    for blocked in settings.blocked_path_roots:
        try:
            bp = Path(blocked).expanduser().resolve()
        except Exception:  # noqa: BLE001
            continue
        if out_str.startswith(_normalize_root(str(bp))):
            log.warning("输出目录在黑名单下: %s (命中 %s)", output_dir, blocked)
            return False
    # 不能输出到根目录（/ 或 X:\\）
    if out.parent == out:
        return False
    return True


def sanitize_group_name(name: str, fallback: str = "unknown") -> str:
    """
    把任意角色名清洗为合法的文件夹名片段：
    - 移除非法字符，截断 80 字，空则 fallback
    """
    if not name:
        return fallback
    cleaned = _ILLEGAL.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"_{2,}", "_", cleaned)[:80]
    return cleaned or fallback


def group_output_dir(output_root: str, group_name: str) -> str:
    """拼接分组目录 = 输出根 + 清洗后的分组名。"""
    safe_name = sanitize_group_name(group_name)
    return os.path.join(output_root, safe_name)
