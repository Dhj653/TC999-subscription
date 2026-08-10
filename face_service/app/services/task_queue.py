"""
串行任务编排器。
- 全局单锁：同一时间只处理 1 个视频（强制串行）。
- 单视频流程：扫描发现 → ffmpeg 抽帧 → InsightFace 提取女性正脸特征
  → FAISS 聚类 → 人名解析与冲突判定 → 写入 face_video_mapping
  → （非测试模式）自动移动 → 清空该视频人脸/帧缓存 → 下一视频。
- 进度持久化到 video_task，支持断点续跑（重启后未完成任务可继续）。
"""
from __future__ import annotations

import asyncio
import gc
import os
from pathlib import Path
from typing import Dict, List, Optional

from ..config import settings, set_runtime
from ..database import (
    append_log,
    create_mapping,
    create_task,
    get_task,
    latest_task,
    list_mappings,
    list_tasks,
    update_task,
)
from ..utils.logger import get_logger
from .archive import is_archive, list_entries, read_entry_bytes
from .file_mover import move_mapping
from .name_parser import extract_names
from .path_safety import group_output_dir, join_output_root, validate_scan_folder

# 重依赖（numpy/cv2/faiss/insightface）懒加载：保证服务在未安装模型时也能启动并服务非 ML 接口。
def _load_ml():
    from .clustering import ClusterStore
    from .face_engine import FaceEngine
    from .video_processor import extract_frames
    return ClusterStore, FaceEngine, extract_frames

log = get_logger()

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".webm"}

# 全局串行锁
_global_lock = asyncio.Lock()
# 运行中任务标记（task_id -> 停止标志）
_stop_flags: Dict[int, bool] = {}
_running_task_id: Optional[int] = None

UNRECOGNIZED_GROUP = "未识别"


def _discover_videos(scan_folder: str) -> List[Dict]:
    """递归发现视频文件与压缩包条目。返回统一结构列表。"""
    items: List[Dict] = []
    root = Path(scan_folder)
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        ext = p.suffix.lower()
        if ext in VIDEO_EXTS:
            items.append({
                "source": "file",
                "video_path": str(p),
                "archive_path": None,
                "in_archive_name": None,
                "display_name": p.name,
            })
        elif is_archive(str(p)):
            for entry in list_entries(str(p)):
                if entry.is_video:
                    items.append({
                        "source": "archive",
                        "video_path": None,
                        "archive_path": str(p),
                        "in_archive_name": entry.in_archive_name,
                        "display_name": f"{p.name}::{entry.in_archive_name}",
                    })
    return items


def _item_identity(item: Dict) -> str:
    """与 face_video_mapping.original_video_path 一致的标识，用于断点续跑去重。"""
    return item["video_path"] or item["display_name"]


def _process_task_sync(task_id: int) -> None:
    """同步处理一个完整任务（在 executor 线程中执行）。"""
    global _running_task_id
    task = get_task(task_id)
    if not task:
        return
    _running_task_id = task_id
    scan_folder = task["scan_folder"]
    output_root = task["output_dir"]
    test_mode = bool(task["test_mode"])
    similarity = float(task["similarity"])
    set_runtime("face_similarity_threshold", similarity)

    try:
        if not validate_scan_folder(scan_folder):
            update_task(task_id, status="failed", error="扫描目录无效或不存在")
            append_log(task_id, f"[错误] 扫描目录无效: {scan_folder}")
            return

        ClusterStore, FaceEngine, _ = _load_ml()
        engine = FaceEngine.instance()
        engine.init()

        append_log(task_id, f"开始扫描: {scan_folder}")
        append_log(task_id, f"输出根: {output_root} | 测试模式: {test_mode} | 相似度: {similarity}")

        items = _discover_videos(scan_folder)
        # 断点续跑：跳过已存在映射记录的视频（identity 命中即视为已处理）
        existing = {
            m["original_video_path"]
            for m in list_mappings(task_id=task_id)
        }
        if existing:
            before = len(items)
            items = [it for it in items if _item_identity(it) not in existing]
            append_log(task_id, f"断点续跑：跳过已处理 {before - len(items)} 个")
        remaining = len(items)
        grand_total = remaining + len(existing)
        update_task(
            task_id,
            total_videos=grand_total,
            processed_videos=len(existing),
            status="running",
        )
        append_log(task_id, f"待处理 {remaining} 个（总计 {grand_total}）")

        store = ClusterStore(task_id)
        # 创建未识别分组（懒创建）
        unrecognized_gid: Optional[int] = None

        def get_unrecognized_gid() -> int:
            nonlocal unrecognized_gid
            if unrecognized_gid is None:
                from ..database import create_group
                unrecognized_gid = create_group(task_id, UNRECOGNIZED_GROUP, status="auto_numbered")
            return unrecognized_gid

        processed = len(existing)
        for idx, item in enumerate(items):
            if _stop_flags.get(task_id):
                append_log(task_id, "收到停止信号，任务已暂停（可断点续跑）")
                update_task(task_id, status="cancelled")
                return

            display = item["display_name"]
            update_task(task_id, current_video=display)
            append_log(task_id, f"[{idx + 1}/{remaining}] 处理: {display}")

            try:
                _process_single_video(task_id, store, item, engine, get_unrecognized_gid, test_mode, output_root)
            except Exception as e:  # noqa: BLE001
                append_log(task_id, f"[警告] 处理失败跳过: {display} ({e})")
                log.warning("处理视频失败: %s", e, exc_info=True)
            finally:
                # ★ 强制清空该视频人脸/帧缓存，释放内存后再处理下一个
                gc.collect()

            processed += 1
            progress = round(processed / grand_total * 100, 1) if grand_total else 100.0
            update_task(
                task_id,
                processed_videos=processed,
                progress=progress,
                current_video="",
            )

        update_task(task_id, progress=100.0, status="completed")
        append_log(task_id, "任务完成")

    except Exception as e:  # noqa: BLE001
        update_task(task_id, status="failed", error=str(e))
        append_log(task_id, f"[错误] 任务失败: {e}")
        log.exception("任务失败")
    finally:
        _stop_flags.pop(task_id, None)
        if _running_task_id == task_id:
            _running_task_id = None


def _process_single_video(
    task_id: int,
    store: ClusterStore,
    item: Dict,
    engine: FaceEngine,
    get_unrecognized_gid,
    test_mode: bool,
    output_root: str,
) -> None:
    """处理单个视频：抽帧→人脸→聚类→命名→映射→移动→清缓存。"""
    _, _, extract_frames = _load_ml()
    video_faces = []
    distinct_group_ids = set()

    if item["source"] == "file":
        video_path = item["video_path"]
        frames = extract_frames(video_path=video_path)
    else:
        # 压缩包内视频：读字节到内存
        bio = read_entry_bytes(item["archive_path"], item["in_archive_name"])
        if not bio:
            append_log(task_id, f"  跳过（无法读取压缩条目）: {item['display_name']}")
            return
        frames = extract_frames(video_bytes=bio)
        del bio

    frame_count = 0
    for frame in frames:
        frame_count += 1
        try:
            faces = engine.extract_valid_faces(frame)
        except Exception:  # noqa: BLE001
            faces = []
        for f in faces:
            cluster = store.get_or_create(f.embedding)
            if cluster is not None:
                distinct_group_ids.add(cluster.group_id)
            video_faces.append(f)
        # 释放该帧
        del frame
    del frames

    append_log(task_id, f"  抽帧 {frame_count} 帧, 有效人脸 {len(video_faces)} 个, 不同人物 {len(distinct_group_ids)} 个")

    # 决定归属分组
    if len(distinct_group_ids) >= 2:
        # 多人
        gid = store.ensure_multi_person_group()
        group_name = store.MULTI_PERSON_GROUP_NAME
        append_log(task_id, f"  → 归入【多人】分组")
    elif len(distinct_group_ids) == 1:
        cluster = store.cluster_by_id(next(iter(distinct_group_ids)))
        # 单人：解析文件名人名并应用
        names = extract_names(item["display_name"])
        for nm in names:
            store.apply_extracted_name(cluster, nm)
        gid = cluster.group_id
        group_name = cluster.group_name
        append_log(task_id, f"  → 归入分组 {group_name}（解析人名: {names or '无'}）")
    else:
        gid = get_unrecognized_gid()
        group_name = UNRECOGNIZED_GROUP
        append_log(task_id, f"  → 无有效女性正脸，归入【未识别】")

    # 记录映射（原始路径用于回滚）
    original_video_path = item["video_path"] or item["display_name"]
    mapping_id = create_mapping(
        group_id=gid,
        task_id=task_id,
        video_path=original_video_path,
        original_video_path=original_video_path,
        original_group_name=group_name,
        source=item["source"],
        archive_path=item["archive_path"],
        in_archive_name=item["in_archive_name"],
    )

    # 自动移动（仅非测试模式；受 1.6 管控）
    if not test_mode and item["source"] == "file":
        try:
            res = move_mapping(mapping_id, output_root, test_mode=False)
            if res.get("moved"):
                append_log(task_id, f"  已移动 → {res['target']}")
        except Exception as e:  # noqa: BLE001
            append_log(task_id, f"  [警告] 移动失败: {e}")

    # ★ 清空该视频人脸缓存（质心保留）
    store.free_video_cache(video_faces)
    engine.free_face_objects(video_faces)
    del video_faces
    # 持久化本视频导致变更的质心（供 reprocess_single 匹配）
    store.flush_dirty()


# ===================== 对外入口 =====================
async def enqueue_scan(
    scan_folder: str,
    output_dir: Optional[str],
    test_mode: bool,
    similarity: float,
) -> int:
    """创建任务并调度后台串行处理。"""
    root = join_output_root(scan_folder, output_dir)
    task_id = create_task(scan_folder, root, test_mode, similarity)
    append_log(task_id, "任务已入队，等待串行处理")
    asyncio.create_task(_run_task(task_id))
    return task_id


async def _run_task(task_id: int) -> None:
    async with _global_lock:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _process_task_sync, task_id)


def stop_task(task_id: int) -> bool:
    _stop_flags[task_id] = True
    return True


def get_status() -> Dict:
    """获取任务进度、队列、运行日志。"""
    tasks = list_tasks(limit=20)
    queued = [t for t in tasks if t["status"] == "queued"]
    latest = latest_task()
    return {
        "running_task_id": _running_task_id,
        "queued_count": len(queued),
        "latest": latest,
        "recent_tasks": tasks,
    }


def resume_pending() -> int:
    """服务启动时恢复未完成（queued/running/cancelled）任务，重新入队。返回恢复数量。"""
    count = 0
    for t in list_tasks(limit=100):
        if t["status"] in ("queued", "running", "cancelled"):
            # 已完成的视频跳过：基于 face_video_mapping 已记录的进行断点续跑
            # 这里简单地把状态置回 queued 重新调度（重处理未完成部分由 processed_videos 区分）
            update_task(t["id"], status="queued")
            asyncio.create_task(_run_task(t["id"]))
            count += 1
    return count


def _reprocess_single_sync(task_id: int, mapping_id: int) -> Dict:
    """重新处理单个视频（同步，需在外部全局锁内调用）。"""
    from ..database import delete_mapping, get_mapping, get_task

    m = get_mapping(mapping_id)
    if not m:
        return {"success": False, "message": "映射不存在"}
    task = get_task(task_id)
    if not task:
        return {"success": False, "message": "任务不存在"}

    # 删除旧映射（释放该视频的旧归属）
    delete_mapping(mapping_id)

    # 重建既有聚类存储
    ClusterStore, FaceEngine, _ = _load_ml()
    store = ClusterStore.load_existing(task_id)
    engine = FaceEngine.instance()
    engine.init()

    # 构造 item：优先用当前磁盘路径
    current_path = m["video_path"] if m["moved"] else m["original_video_path"]
    if m["source"] == "file":
        if not os.path.exists(current_path):
            # 文件可能被移动到分组目录，回退到 original
            current_path = m["original_video_path"]
        item = {
            "source": "file",
            "video_path": current_path,
            "archive_path": None,
            "in_archive_name": None,
            "display_name": os.path.basename(current_path),
        }
    else:
        item = {
            "source": "archive",
            "video_path": None,
            "archive_path": m["archive_path"],
            "in_archive_name": m["in_archive_name"],
            "display_name": m["in_archive_name"] or "archive_video",
        }

    test_mode = bool(task["test_mode"])
    output_root = task["output_dir"]

    # 未识别分组懒创建
    unrecognized_gid: Optional[int] = None

    def get_unrecognized_gid() -> int:
        nonlocal unrecognized_gid
        if unrecognized_gid is None:
            from ..database import list_groups
            for g in list_groups(task_id):
                if g["group_name"] == UNRECOGNIZED_GROUP:
                    unrecognized_gid = g["group_id"]
                    break
            if unrecognized_gid is None:
                from ..database import create_group
                unrecognized_gid = create_group(task_id, UNRECOGNIZED_GROUP, status="auto_numbered")
        return unrecognized_gid

    append_log(task_id, f"重新处理单个视频: {item['display_name']}")
    try:
        _process_single_video(task_id, store, item, engine, get_unrecognized_gid, test_mode, output_root)
        gc.collect()
        return {"success": True, "message": f"已重新处理: {item['display_name']}"}
    except Exception as e:  # noqa: BLE001
        append_log(task_id, f"[错误] 重新处理失败: {e}")
        return {"success": False, "message": str(e)}


async def reprocess_single(task_id: int, mapping_id: int) -> Dict:
    """重新处理单个视频（串行，受全局锁约束）。"""
    async with _global_lock:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _reprocess_single_sync, task_id, mapping_id)
