"""
侧边栏注入脚本（自动、幂等）：
  - 读取萤核 sidebar.js（由命令行参数传入路径）
  - 读取 frontend_extension/face_video_sidebar.js 的 menu 定义
  - 追加新增菜单项到 sidebar 数组中（避免重复）
用法：
  python scripts/patch_sidebar.py /path/to/yonuc/src/renderer/layout/sidebar.js
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


NEW_ITEMS = [
    {
        "import": "FaceVideoClassify",
        "from_str": "'../../../../frontend_extension/views/FaceVideoClassify.vue'",
        "route_path": "/face-video",
        "title": "人脸视频分类",
        "icon": "VideoIcon",
        "key_suffix": "face-video",
    },
    {
        "import": "CharacterManager",
        "from_str": "'../../../../frontend_extension/views/CharacterManager.vue'",
        "route_path": "/character-manager",
        "title": "角色管理",
        "icon": "UsersIcon",
        "key_suffix": "character-manager",
    },
]


def patch(sidebar_path: Path) -> None:
    src = sidebar_path.read_text(encoding="utf-8")
    orig = src

    # 1. 顶部追加 import
    needed_imports = ""
    for it in NEW_ITEMS:
        pat = rf"import\s+{re.escape(it['import'])}\s+from"
        if not re.search(pat, src):
            needed_imports += f"import {it['import']} from {it['from_str']}\n"

    if needed_imports:
        # 找到第一行 import 的位置前插入
        m = re.search(r"^import\s", src, re.MULTILINE)
        if m:
            src = src[: m.start()] + needed_imports + src[m.start() :]
        else:
            src = needed_imports + "\n" + src

    # 2. 追加路由（简单：在 default export 的数组的最后一个元素前插入）
    # 这里采用更保守的方法：在文件末尾若有 sidebarExtensions 就追加，
    # 否则在 routes 数组尾部追加新的 { path, name, component, meta } 片段。
    for it in NEW_ITEMS:
        key_check = f"path: '{it['route_path']}'"
        if key_check in src or f'path: "{it["route_path"]}"' in src:
            continue  # 已注入

        # 匹配 routes 数组的最后一个成员（如果能）
        # 简化：在 `]` 前（数组闭括号）插入新条目
        insertion = (
            "\n  {\n"
            f"    path: '{it['route_path']}',\n"
            f"    name: '{it['key_suffix']}',\n"
            f"    component: {it['import']},\n"
            "    meta: {\n"
            f"      title: '{it['title']}',\n"
            f"      icon: '{it['icon']}',\n"
            "      hideInMenu: false,\n"
            "    },\n"
            "  },\n"
        )

        # 找 default export 里最外层的 routes: [ 或 export default [
        # 简化做法：在最后一个 "]" 前（假设最后的 ] 就是 routes 数组闭）
        idx = src.rfind("]")
        if idx == -1:
            print("[patch_sidebar] 无法定位 routes 数组结尾，跳过追加 routes",
                  file=sys.stderr)
            continue
        src = src[:idx] + insertion + src[idx:]

    if src != orig:
        backup = sidebar_path.with_suffix(sidebar_path.suffix + ".bak")
        if not backup.exists():
            backup.write_text(orig, encoding="utf-8")
        sidebar_path.write_text(src, encoding="utf-8")
        print("[patch_sidebar] 已注入新增菜单项；备份：", backup)
    else:
        print("[patch_sidebar] 无需修改（新增项已存在）。")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sidebar", help="萤核 sidebar.js / routes.js 文件绝对路径")
    args = ap.parse_args()
    p = Path(args.sidebar)
    if not p.exists():
        print("文件不存在:", p, file=sys.stderr)
        sys.exit(2)
    patch(p)


if __name__ == "__main__":
    main()
