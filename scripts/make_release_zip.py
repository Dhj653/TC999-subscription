"""
打包脚本：输出单一 zip 交付到本地电脑。
使用 Python 标准库 zipfile，无系统依赖（不依赖 zip/tar 命令）。
默认排除：__pycache__、.pyc、数据/*.db、日志/*.log、.git、模型缓存、临时测试文件。
"""
from __future__ import annotations

import fnmatch
import os
import sys
import time
import zipfile
from pathlib import Path

# 打包脚本在 scripts/ 下，ROOT 应取项目根（scripts 的父目录）
ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "dist"
TIMESTAMP = time.strftime("%Y%m%d_%H%M%S")
ZIP_NAME = f"face_classify_project_{TIMESTAMP}.zip"
ZIP_PATH = OUT_DIR / ZIP_NAME

# 项目内真正需要打包的子目录/文件（白名单方式，避免误带大文件）
INCLUDE_ITEMS: list[str] = [
    "face_service/app",
    "face_service/models/.gitkeep",
    "face_service/.env.example",
    "face_service/requirements.txt",
    "face_service/run.py",
    "frontend_extension",
    "scripts",
    "docs",
    "LICENSE",
    "README.md",
    "README_扩展.md",
    "folders_description.yaml",
]

# 目录/文件/扩展名排除（即便在白名单中也排除）
EXCLUDE_DIR_NAMES = {
    "__pycache__", ".git", ".venv", "venv", "node_modules",
    "data", "logs", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "__MACOSX",
}
EXCLUDE_EXT = {
    ".pyc", ".pyo", ".log", ".db", ".db-wal", ".db-shm",
    ".tmp", ".pyd", ".so", ".dll", ".dylib", ".class", ".o",
    ".zip", ".tar", ".gz", ".rar", ".7z",  # 避免把旧包嵌套打进去
}
EXCLUDE_GLOBS: list[str] = [
    "*.mp4", "*.mkv", "*.avi", "*.mov",   # 绝不打包任何视频
    "data/**", "logs/**",
    # 注意：fnmatch 顺序敏感，负向规则（!开头）必须放在对应正向规则之前
    "!face_service/models/.gitkeep",
    "face_service/models/*",  # 仅保留 .gitkeep，模型文件不放（在本地下载即可）
    ".DS_Store", "Thumbs.db",
]


def _is_excluded_by_ext(name: str) -> bool:
    ext = os.path.splitext(name)[1].lower()
    return ext in EXCLUDE_EXT


def _is_excluded_by_dirparts(abs_path: Path) -> bool:
    # 任何一级目录名命中 EXCLUDE_DIR_NAMES 都排除
    for part in abs_path.parts:
        if part in EXCLUDE_DIR_NAMES:
            return True
    return False


def _match_glob(rel_posix: str) -> bool:
    for pat in EXCLUDE_GLOBS:
        neg = False
        if pat.startswith("!"):
            neg = True
            pat = pat[1:]
        hit = fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(os.path.basename(rel_posix), pat)
        if neg and hit:
            return False  # 负向 glob：匹配就不排除
        if hit and not neg:
            return True
    return False


def should_include(abs_path: Path, root: Path) -> bool:
    if abs_path.is_file():
        if _is_excluded_by_ext(abs_path.name):
            return False
    rel_parts = abs_path.relative_to(root).parts
    if set(rel_parts) & EXCLUDE_DIR_NAMES:
        return False
    rel_posix = abs_path.relative_to(root).as_posix()
    if _match_glob(rel_posix):
        return False
    # 排除目录本身时，其下文件已被 rel_parts 检查拦截
    return True


def collect_files(root: Path) -> list[Path]:
    collected: list[Path] = []
    for item in INCLUDE_ITEMS:
        p = root / item
        if not p.exists():
            print(f"[WARNING] 白名单不存在，跳过: {item}")
            continue
        if p.is_file():
            if should_include(p.resolve(), root):
                collected.append(p.resolve())
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            # 先过滤 dirnames（in-place 移除，避免 os.walk 深入）
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_NAMES]
            for fn in filenames:
                fp = Path(dirpath) / fn
                abs_fp = fp.resolve()
                if should_include(abs_fp, root):
                    collected.append(abs_fp)
    # 去重 & 排序
    return sorted(set(collected))


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = collect_files(ROOT)
    print(f"将打包 {len(files)} 个文件到 {ZIP_PATH}")
    total_bytes = 0
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for fp in files:
            rel = fp.relative_to(ROOT)
            zf.write(fp, arcname=str(rel))
            try:
                total_bytes += fp.stat().st_size
            except OSError:
                pass
    size = ZIP_PATH.stat().st_size
    size_mb = size / (1024 * 1024)
    orig_mb = total_bytes / (1024 * 1024)
    print("=" * 60)
    print(f"✅ 打包完成: {ZIP_PATH}")
    print(f"   源文件合计 : {orig_mb:.2f} MB ({len(files)} 个文件)")
    print(f"   压缩后大小 : {size_mb:.2f} MB  (压缩率 {100*size/max(1,total_bytes):.1f}%)")
    print(f"   产物时间戳 : {TIMESTAMP}")
    print("=" * 60)
    # 输出复制路径（方便用户在文件面板里直接下载）
    print()
    print("📦 下载到本地电脑的方法：")
    print(f"   在左侧文件面板里找到并右键该文件 → 下载(Download)：")
    print(f"   {ZIP_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
