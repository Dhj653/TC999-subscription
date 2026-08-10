#!/usr/bin/env python3
"""
幂等补丁：在萤核 sidebar.js 顶部追加 import，并在其默认导出的菜单数组中合并本扩展路由。
- 自动备份 sidebar.js -> sidebar.js.bak
- 已打过补丁则跳过（幂等）
- 采用正则定位 export default 的数组，安全合并；若结构无法识别，打印手动指引。
用法：python patch_sidebar.py <sidebar.js 路径> <face_video_sidebar.js 路径>
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

MARK = "# face_video_ext injected"
IMPORT_LINE_TEMPLATE = "import {{ faceVideoMenu }} from '{rel}'"


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: python patch_sidebar.py <sidebar.js> <face_video_sidebar.js>")
        return 2

    sidebar_path = Path(sys.argv[1]).resolve()
    ext_module = Path(sys.argv[2]).resolve()

    if not sidebar_path.exists():
        print(f"[错误] 找不到 sidebar.js: {sidebar_path}")
        return 1

    text = sidebar_path.read_text(encoding="utf-8")

    if MARK in text:
        print("[跳过] sidebar.js 已打过补丁（幂等）。")
        return 0

    # 计算相对导入路径
    rel = os.path.relpath(str(ext_module), str(sidebar_path.parent)).replace("\\", "/")
    if not rel.startswith("."):
        rel = "./" + rel
    import_line = IMPORT_LINE_TEMPLATE.format(rel=rel)

    # 备份
    bak = sidebar_path.with_suffix(sidebar_path.suffix + ".bak")
    shutil.copy2(sidebar_path, bak)
    print(f"[备份] {sidebar_path} -> {bak}")

    # 1) 顶部插入 import
    lines = text.splitlines(keepends=True)
    # 找最后一条 import 行，插到其后
    last_import = -1
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("import ") or ln.lstrip().startswith("import{"):
            last_import = i
    inject_import = f"// {MARK}\n{import_line}\n"
    if last_import >= 0:
        lines.insert(last_import + 1, inject_import)
    else:
        lines.insert(0, inject_import)

    new_text = "".join(lines)

    # 2) 尝试在 export default [...] 数组内合并 faceVideoMenu
    #    匹配 export default [  形式（数组字面量）
    pattern = re.compile(r"export\s+default\s*\[\s*", re.S)
    merged = False
    m = pattern.search(new_text)
    if m:
        insert_at = m.end()
        new_text = new_text[:insert_at] + "\n  ...faceVideoMenu,\n" + new_text[insert_at:]
        merged = True
    else:
        # 备选：在文件末尾追加 push 语句，尝试常见菜单变量名
        names = ["menus", "menuList", "routes", "sidebarMenus", "menuData"]
        tries = " || ".join(
            f"(typeof {n}!=='undefined'&&Array.isArray({n})?{n}:null)" for n in names
        )
        new_text += (
            f"\n// {MARK} (fallback)\n"
            f"(function(){{ var _m = {tries}; if(_m){{ _m.push.apply(_m, faceVideoMenu); }} "
            f"else {{ console.warn('[face_video_ext] 未识别菜单数组，请手动加入 ...faceVideoMenu'); }} }})();\n"
        )

    sidebar_path.write_text(new_text, encoding="utf-8")
    print(f"[完成] 已注入 import: {import_line}")
    if merged:
        print("[完成] 已在 export default 数组中合并 ...faceVideoMenu")
    else:
        print("[提示] 未能自动识别菜单数组结构，已追加 fallback。")
        print("       请手动在 sidebar.js 的菜单数组中加入一项：...faceVideoMenu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
