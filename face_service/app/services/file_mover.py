"""
文件移动 / 回滚。
- 所有移动受 TEST_PREVIEW_MODE 管控：测试模式只记录"拟移动"，不碰磁盘。
- 移动前原始路径已由 face_video_mapping 记录，支持一键回滚。
- 压缩包内视频（source='archive'）不落盘移动，仅归档记录。
"""
from __future__ import annotations

import os
import shutil
from typing import Dict, List

from ..config import settings
from ..database import (
    get_group,
    list_groups,
    list_mappings,
    update_group,
    update_mapping,
)
from ..utils.logger import get_logger
from .path_safety import group_output_dir

log = get_logger()


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def move_mapping(mapping_id: int, output_root: str, test_mode: bool) -> Dict:
    """移动单个视频到所属分组目录。返回操作结果。"""
    from ..database import get_mapping

    m = get_mapping(mapping_id)
    if not m:
        return {"success": False, "message": "映射不存在"}

    if m["source"] == "archive":
        return {
            "success": True,
            "moved": False,
            "message": "压缩包内视频不落盘移动（保持归档分析）",
            "target": None,
        }

    group = get_group(m["group_id"])
    if not group:
        return {"success": False, "message": "分组不存在"}

    target_dir = group_output_dir(output_root, group["group_name"])
    src = m["original_video_path"]
    if not os.path.exists(src):
        return {"success": False, "message": f"源文件不存在: {src}"}

    target_path = os.path.join(target_dir, os.path.basename(src))
    # 同名冲突处理
    if os.path.exists(target_path) and os.path.abspath(target_path) != os.path.abspath(src):
        base, ext = os.path.splitext(os.path.basename(src))
        i = 1
        while os.path.exists(target_path):
            target_path = os.path.join(target_dir, f"{base}_{i}{ext}")
            i += 1

    if test_mode:
        return {
            "success": True,
            "moved": False,
            "test_mode": True,
            "message": f"[预览] 拟移动 → {target_path}",
            "target": target_path,
        }

    _ensure_dir(target_dir)
    shutil.move(src, target_path)
    update_mapping(mapping_id, video_path=target_path, moved=1)
    log.info("已移动 %s → %s", src, target_path)
    return {"success": True, "moved": True, "target": target_path, "source": src}


def move_all(task_id: int, test_mode: bool) -> List[Dict]:
    """批量移动某任务下全部视频。"""
    mappings = list_mappings(task_id=task_id)
    task = None
    from ..database import get_task

    task = get_task(task_id)
    output_root = task["output_dir"] if task else ""
    results: List[Dict] = []
    for m in mappings:
        results.append(
            {"mapping_id": m["id"], **move_mapping(m["id"], output_root, test_mode)}
        )
    return results


def revert_all(task_id: int) -> Dict:
    """
    一键回滚：
    - 把已移动文件移回原始路径
    - 恢复分组原始名称与状态
    """
    mappings = list_mappings(task_id=task_id)
    reverted_files = 0
    failed = []
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

    # 恢复分组原始名
    for g in list_groups(task_id):
        update_group(
            g["group_id"],
            group_name=g["original_group_name"],
            status="auto_numbered",
        )

    log.info("回滚完成: 还原 %d 个文件, 失败 %d", reverted_files, len(failed))
    return {"reverted_files": reverted_files, "failed": failed}
