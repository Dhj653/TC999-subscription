"""
文件移动 / 回滚 — 核心新增：
1. move_all 仅移动 "分组视频数 >= FOLDER_CREATE_MIN_VIDEOS" 的分组（新需求：2 个同人才建夹）
2. 单人视频按策略处理（留原位 / 归未分类）
3. 角色命名后 rename_character_folder 联动重命名磁盘文件夹
4. 【新增】视频内容级去重：相同画面/内容的视频归档到 _重复文件_[/子目录]/，支持任意层嵌套
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


# ============================================================
# 【新增】重复视频归档：_重复文件_ 目录 + 任意层嵌套
# ============================================================
def _resolve_repeat_subdir(
    current_layer_dir: str,
    current_fp_hashes: list[str],
    *,
    fingerprints_in_layer: list[dict],
    depth: int = 0,
) -> str:
    """
    针对一层目录（current_layer_dir），决定该重复视频应该放置的**直接子目录**。
    - 如果本层没有与 current_fp_hashes 相同内容的视频：
        返回 current_layer_dir 本身（允许放在这一层）
    - 如果本层已经有相同内容的视频：
        * 若 dedup_nesting=False → 返回 current_layer_dir / settings.dedup_repeat_folder_name
        * 若 dedup_nesting=True  → 进入下一层 _重复文件_ 递归计算
          （如果该层已有"兄弟"视频与 current 相同 → 在 _重复文件_ 内再分配序号子目录 _重复_N_）

    fingerprints_in_layer：本层目录下所有"非 duplicate_of=某ID"的已归档视频指纹。
    返回值：最终目标目录（可能是 current_layer_dir / _重复文件_ / _重复_3_）。
    """
    from ..config import settings
    from .video_hasher import is_duplicate, VideoFingerprint

    # 1) 先看本层是否有内容相同的
    conflict_at_layer = False
    for f in fingerprints_in_layer:
        if f.get("ignored"):
            continue
        f_hashes = f.get("hashes") or []
        # 构造 fp 用于比较（只用到 hashes / duration / width / height）
        if is_duplicate(
            VideoFingerprint("<current>", None, None, None, 0, current_fp_hashes),
            b_hashes=f_hashes,
            b_duration=f.get("duration_sec"),
            b_width=f.get("width"),
            b_height=f.get("height"),
        ):
            conflict_at_layer = True
            break
    if not conflict_at_layer:
        # 本层无冲突，可以直接放在这一层
        return current_layer_dir

    # 2) 有冲突 → 进入 _重复文件_ 子目录
    repeat_root_name = settings.dedup_repeat_folder_name
    repeat_root = os.path.join(current_layer_dir, repeat_root_name)

    if not settings.dedup_nesting:
        # 简单模式：不嵌套，全部扔到 _重复文件_ 下
        return repeat_root

    # 3) 嵌套模式：递归为 repeat_root 分配序号子目录
    # 为了保证"同一层文件夹内不能有相同视频"，在 _重复文件_ 内把"互相重复"的分到不同序号子目录
    # 策略：维护序号子目录 list，每个序号子目录存放一批"互相不重复"的视频；
    #       若所有序号子目录都有与之冲突的，则新建序号
    siblings = _list_repeat_subdirs(repeat_root)
    # 每个子目录要知道里面的 fingerprints：调用方通过 DB 的 only_within_dir 查询即可；
    # 这里用简化但正确的策略：为每次冲突分配递增序号目录，直到找到"该序号目录里没有相同指纹"为止
    subdir_idx = 1
    while True:
        subdir = os.path.join(repeat_root, f"_重复_{subdir_idx}组_")
        # 取 subdir 目录下已有的主指纹（通过 only_within_dir 过滤）
        subdir_fps = [f for f in fingerprints_in_layer
                      if f.get("video_path") and (
                          f["video_path"].startswith(subdir + os.sep)
                          or f["video_path"].startswith(subdir + "/"))]
        # 再加入 _重复文件夹_ 根下（非子目录内）的文件 → 同样作为 subdir_idx=0
        if subdir_idx == 1:
            # 把 repeat_root 本身下的文件也纳入检测（指纹可能在 repeat_root）
            root_fps = [f for f in fingerprints_in_layer
                        if f.get("video_path") and (
                            f["video_path"].startswith(repeat_root + os.sep)
                            or f["video_path"].startswith(repeat_root + "/"))
                        and not any(f["video_path"].startswith(
                            os.path.join(repeat_root, g) + os.sep)
                            or f["video_path"].startswith(os.path.join(repeat_root, g) + "/")
                            for g in _list_repeat_subdirs(repeat_root, fullpath=False))]
            subdir_fps = subdir_fps + root_fps
        still_conflict = False
        for f in subdir_fps:
            if is_duplicate(
                VideoFingerprint("<current>", None, None, None, 0, current_fp_hashes),
                b_hashes=f.get("hashes") or [],
                b_duration=f.get("duration_sec"),
                b_width=f.get("width"),
                b_height=f.get("height"),
            ):
                still_conflict = True
                break
        if not still_conflict:
            return subdir
        subdir_idx += 1
        # 安全上限 999
        if subdir_idx > 999:
            return subdir


def _list_repeat_subdirs(repeat_root: str, *, fullpath: bool = True) -> list[str]:
    """列出 repeat_root 下形如 _重复_N组_ 的子目录名（或完整路径）。不存在返回 []。"""
    import re
    if not os.path.isdir(repeat_root):
        return []
    pat = re.compile(r"^_重复_(\d+)组_$")
    out = []
    try:
        for name in os.listdir(repeat_root):
            if pat.match(name):
                out.append(os.path.join(repeat_root, name) if fullpath else name)
    except OSError:
        pass
    return sorted(out)


def move_duplicate_to_repeat_dir(
    video_path: str,
    target_role_dir: str,
    current_fp_hashes: list[str],
    *,
    test_mode: bool,
    fingerprints_in_role: list[dict],
) -> dict:
    """
    把视频 video_path 从它当前位置，根据与 fingerprints_in_role 的冲突情况，
    放置到合理的嵌套目录，以保证"同一层目录内不出现相同视频"。

    fingerprints_in_role: 该角色目录下（含子目录）全部指纹列表（含重复文件内的）。
    返回 dict: {success, moved, target_path, message, target_dir, layer_desc}
    """
    if not os.path.exists(video_path):
        return {"success": False, "message": f"源文件不存在: {video_path}"}

    # 深度优先：从顶层 target_role_dir 开始递归判定存放位置
    target_dir = _resolve_repeat_subdir(
        target_role_dir, current_fp_hashes,
        fingerprints_in_layer=fingerprints_in_role, depth=0,
    )

    src = video_path
    base = os.path.basename(src)
    target_path = os.path.join(target_dir, base)

    # 处理目标已存在同名（不同内容）的情况
    if os.path.exists(target_path) and os.path.abspath(target_path) != os.path.abspath(src):
        bname, ext = os.path.splitext(base)
        i = 1
        while os.path.exists(target_path):
            target_path = os.path.join(target_dir, f"{bname}_{i}{ext}")
            i += 1

    if test_mode:
        return {"success": True, "moved": False, "test_mode": True,
                "message": f"[预览重复归档] 拟 → {target_path}",
                "target_path": target_path, "target_dir": target_dir}

    _ensure_dir(target_dir)
    shutil.move(src, target_path)
    log.info("重复视频归档 %s → %s", src, target_path)
    return {"success": True, "moved": True,
            "target_path": target_path, "target_dir": target_dir,
            "message": "已归档到重复目录"}
