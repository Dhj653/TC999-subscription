"""
文件移动 / 回滚 — 核心新增：
1. move_all 仅移动 "分组视频数 >= FOLDER_CREATE_MIN_VIDEOS" 的分组（新需求：2 个同人才建夹）
2. 单人视频按策略处理（留原位 / 归未分类）
3. 角色命名后 rename_character_folder 联动重命名磁盘文件夹
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings
from ..database import (
    get_group,
    get_task,
    list_groups,
    list_mappings,
    update_group,
    update_mapping,
)
from ..utils.logger import get_logger
from .path_safety import group_output_dir, sanitize_group_name

log = get_logger()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


# ============================================================
# 单视频移动 / 单任务批量移动
# ============================================================
def move_mapping(
    mapping_id: int,
    output_root: str,
    test_mode: bool,
    *,
    force_group_name: Optional[str] = None,
) -> Dict:
    """移动单个视频到所属分组目录。返回操作结果。"""
    from ..database import get_mapping

    m = get_mapping(mapping_id)
    if not m:
        return {"success": False, "message": "映射不存在"}
    if m["source"] == "archive":
        return {"success": True, "moved": False,
                "message": "压缩包内视频不落盘移动（保持归档分析）", "target": None}
    group = get_group(m["group_id"])
    if not group:
        return {"success": False, "message": "分组不存在"}

    gname = force_group_name or group["group_name"]
    target_dir = group_output_dir(output_root, gname)
    src = m["original_video_path"]
    if not os.path.exists(src):
        return {"success": False, "message": f"源文件不存在: {src}"}

    target_path = os.path.join(target_dir, os.path.basename(src))
    if os.path.exists(target_path) and os.path.abspath(target_path) != os.path.abspath(src):
        base, ext = os.path.splitext(os.path.basename(src))
        i = 1
        while os.path.exists(target_path):
            target_path = os.path.join(target_dir, f"{base}_{i}{ext}")
            i += 1

    if test_mode:
        return {"success": True, "moved": False, "test_mode": True,
                "message": f"[预览] 拟移动 → {target_path}", "target": target_path}

    _ensure_dir(target_dir)
    shutil.move(src, target_path)
    update_mapping(mapping_id, video_path=target_path, moved=1)
    log.info("已移动 %s → %s", src, target_path)
    return {"success": True, "moved": True, "target": target_path, "source": src}


def move_all(task_id: int, test_mode: bool) -> List[Dict]:
    """
    批量移动某任务下全部视频 —— 仅满足 folder_create_min_videos 阈值的分组才建夹移动。
    单人视频按 SINGLE_VIDEO_POLICY 处理。
    """
    from ..database import get_task

    task = get_task(task_id)
    if not task:
        return [{"success": False, "message": "任务不存在"}]
    output_root = task["output_dir"] or ""
    min_n = max(1, settings.folder_create_min_videos)
    policy = settings.single_video_policy

    groups = list_groups(task_id)
    # 先筛选符合阈值的分组
    eligible_groups: dict[int, str] = {}
    for g in groups:
        cnt = g["video_count"]
        if cnt >= min_n:
            eligible_groups[g["group_id"]] = g["group_name"]

    results: List[Dict] = []
    mappings = list_mappings(task_id=task_id)
    uncategorized_dir = os.path.join(output_root, settings.uncategorized_dir_name)

    for m in mappings:
        gid = m["group_id"]
        if gid in eligible_groups:
            # 正常：移动到对应分组目录
            results.append({"mapping_id": m["id"],
                            **move_mapping(m["id"], output_root, test_mode)})
        else:
            # 单人：按策略处理
            if m["source"] == "archive":
                results.append({"mapping_id": m["id"],
                                "success": True, "moved": False,
                                "message": "压缩包内视频不落盘（单人不建夹）"})
                continue
            if policy == "to_uncategorized":
                src = m["original_video_path"]
                if not os.path.exists(src):
                    results.append({"mapping_id": m["id"],
                                    "success": False, "message": "源文件不存在"})
                    continue
                target_path = os.path.join(uncategorized_dir, os.path.basename(src))
                if test_mode:
                    results.append({"mapping_id": m["id"], "success": True, "moved": False,
                                    "test_mode": True,
                                    "message": f"[预览单人] 拟移动 → {target_path}"})
                else:
                    _ensure_dir(uncategorized_dir)
                    try:
                        shutil.move(src, target_path)
                        update_mapping(m["id"], video_path=target_path, moved=1)
                        results.append({"mapping_id": m["id"], "success": True, "moved": True,
                                        "target": target_path, "policy": "uncategorized"})
                    except Exception as e:  # noqa: BLE001
                        results.append({"mapping_id": m["id"],
                                        "success": False, "message": str(e)})
            else:
                results.append({"mapping_id": m["id"], "success": True, "moved": False,
                                "message": f"单人视频留原位（本分组不足 {min_n} 个视频）"})
    return results


def revert_all(task_id: int) -> Dict:
    """一键回滚：还原文件 + 恢复分组原始名称。"""
    mappings = list_mappings(task_id=task_id)
    reverted_files = 0
    failed: List[Dict] = []
    for m in mappings:
        if not m["moved"]:
            continue
        src = m["video_path"]
        dst = m["original_video_path"]
        try:
            if os.path.exists(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                reverted_files += 1
            else:
                failed.append({"mapping_id": m["id"], "reason": "源不存在", "src": src})
            update_mapping(m["id"], video_path=dst, moved=0)
        except Exception as e:  # noqa: BLE001
            failed.append({"mapping_id": m["id"], "reason": str(e), "src": src})
    for g in list_groups(task_id):
        update_group(g["group_id"], group_name=g["original_group_name"],
                     status="auto_numbered")
    log.info("回滚完成: 还原 %d 个文件, 失败 %d", reverted_files, len(failed))
    return {"reverted_files": reverted_files, "failed": failed}


# ============================================================
# 【新增】角色命名联动重命名磁盘文件夹
# ============================================================
def rename_character_folder(
    old_folder_path: str,
    new_name: str,
) -> Dict:
    """
    用户在角色管理中改名 → 把磁盘上的旧目录重命名为新名（同目录父路径下）。
    返回 {success, old_path, new_path, message}
    """
    if not old_folder_path or not os.path.exists(old_folder_path):
        return {"success": False, "message": "旧文件夹不存在，跳过重命名（可能还没移动文件）",
                "old_path": old_folder_path}

    safe_new = sanitize_group_name(new_name)
    if not safe_new:
        return {"success": False, "message": "新名称清洗后为空"}

    parent = os.path.dirname(os.path.abspath(old_folder_path))
    new_path = os.path.join(parent, safe_new)

    if os.path.abspath(old_folder_path) == os.path.abspath(new_path):
        return {"success": True, "message": "名称无变化（清洗后相同）",
                "old_path": old_folder_path, "new_path": new_path}

    if os.path.exists(new_path):
        # 新目录已存在 → 不合并避免误伤，返回提示
        return {"success": False,
                "message": f"目标目录已存在：{new_path}，请先处理后再改名",
                "old_path": old_folder_path, "new_path": new_path}

    try:
        os.rename(old_folder_path, new_path)
        log.info("角色目录重命名: %s → %s", old_folder_path, new_path)
        return {"success": True, "old_path": old_folder_path, "new_path": new_path,
                "message": "目录已重命名"}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "message": f"重命名失败：{e}",
                "old_path": old_folder_path}
