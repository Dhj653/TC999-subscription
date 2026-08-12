"""
去重 API：列表/忽略重复/手动归档/打开重复目录/重算指定任务去重/开关配置。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

from .. import database as DB
from ..config import settings
from ..services.file_mover import move_duplicate_to_repeat_dir
from ..services.task_queue import enqueue_scan, is_busy
from ..utils.logger import get_logger

log = get_logger()

router = APIRouter(tags=["dedup"])


# ------------------- 响应模型 -------------------
class DedupFingerprintVO(BaseModel):
    fingerprint_id: int
    task_id: Optional[int]
    mapping_id: Optional[int]
    video_path: str
    original_video_path: str
    duration_sec: Optional[float]
    width: Optional[int]
    height: Optional[int]
    file_size: int
    hashes: List[str]
    duplicate_of: Optional[int]
    ignored: bool
    created_at: float


class DedupGroupedItem(BaseModel):
    master_fingerprint_id: int
    master_video_path: str
    duplicate_count: int
    duplicates: List[DedupFingerprintVO]


class IgnoreReq(BaseModel):
    fingerprint_id: int
    ignored: bool = True


class ArchiveReq(BaseModel):
    fingerprint_id: int
    target_role_dir: str
    test_mode: Optional[bool] = None


class ListReq(BaseModel):
    task_id: Optional[int] = None
    only_duplicates: bool = True
    only_within_dir: Optional[str] = None


# ------------------- 工具 -------------------
def _to_vo(row: dict) -> DedupFingerprintVO:
    return DedupFingerprintVO(
        fingerprint_id=int(row["fingerprint_id"]),
        task_id=row.get("task_id"),
        mapping_id=row.get("mapping_id"),
        video_path=row.get("video_path") or "",
        original_video_path=row.get("original_video_path") or "",
        duration_sec=row.get("duration_sec"),
        width=row.get("width"),
        height=row.get("height"),
        file_size=int(row.get("file_size") or 0),
        hashes=list(row.get("hashes") or []),
        duplicate_of=row.get("duplicate_of"),
        ignored=bool(row.get("ignored")),
        created_at=float(row.get("created_at") or 0),
    )


# ------------------- 端点 -------------------
@router.get("/api/dedup/list")
async def dedup_list(
    task_id: Optional[int] = Query(None, description="按任务过滤"),
    only_duplicates: bool = Query(True, description="只列出重复的（duplicate_of非空且未被忽略）"),
    only_within_dir: Optional[str] = Query(None, description="只列出位于该目录下的视频"),
):
    rows = DB.list_fingerprints(
        task_id=task_id, only_duplicates=only_duplicates, only_within_dir=only_within_dir,
    )
    return {"ok": True, "total": len(rows), "items": [_to_vo(r) for r in rows]}


@router.get("/api/dedup/grouped")
async def dedup_grouped(
    task_id: Optional[int] = Query(None, description="按任务过滤"),
    only_within_dir: Optional[str] = Query(None, description="只看该目录下"),
):
    """按"主视频 → 重复列表"聚合返回，方便前端展示。"""
    all_rows = DB.list_fingerprints(task_id=task_id, only_duplicates=False,
                                    only_within_dir=only_within_dir)
    by_id = {r["fingerprint_id"]: r for r in all_rows}

    # 构建 master→duplicates 列表
    master_map: dict[int, list] = {}
    for r in all_rows:
        mid = r.get("duplicate_of")
        if mid is None:
            continue
        if r.get("ignored"):
            continue
        lst = master_map.setdefault(int(mid), [])
        lst.append(r)

    out: List[dict] = []
    for mid, dups in master_map.items():
        master_row = by_id.get(mid)
        if master_row is None:
            master_path = dups[0].get("video_path") if dups else ""
        else:
            master_path = master_row.get("video_path") or ""
        out.append({
            "master_fingerprint_id": mid,
            "master_video_path": master_path,
            "duplicate_count": len(dups),
            "duplicates": [_to_vo(d) for d in dups],
        })
    out.sort(key=lambda g: -g["duplicate_count"])
    return {"ok": True, "groups": out, "total_groups": len(out)}


@router.post("/api/dedup/ignore")
async def dedup_ignore(req: IgnoreReq):
    """标记某个重复记录为"忽略（保留原位，不归档）"，或取消忽略。"""
    if not DB.get_fingerprint(req.fingerprint_id):
        raise HTTPException(404, "指纹ID不存在")
    DB.update_fingerprint(req.fingerprint_id, ignored=1 if req.ignored else 0)
    return {"ok": True, "message": ("已忽略" if req.ignored else "已取消忽略")}


@router.post("/api/dedup/archive-one")
async def dedup_archive_one(req: ArchiveReq):
    """
    对一个标记为 duplicate 的指纹记录手动执行归档移动到 _重复文件_/子目录。
    target_role_dir：目标角色顶层目录（角色对应的文件夹，将在此目录下创建 _重复文件_）。
    """
    fp = DB.get_fingerprint(req.fingerprint_id)
    if not fp:
        raise HTTPException(404, "指纹ID不存在")
    if not fp.get("duplicate_of"):
        raise HTTPException(400, "该指纹是主视频，无需归档；请确认 duplicate_of 非空")
    if not os.path.isdir(req.target_role_dir):
        raise HTTPException(400, f"target_role_dir 不存在或非目录: {req.target_role_dir}")

    video_path = fp.get("video_path")
    if not os.path.exists(video_path):
        video_path = fp.get("original_video_path")
    if not os.path.exists(video_path):
        raise HTTPException(404, "指纹对应的视频文件在磁盘上不存在")

    fps_in_role = DB.list_fingerprints(only_within_dir=req.target_role_dir)
    test_mode = req.test_mode if req.test_mode is not None else settings.test_preview_mode

    result = move_duplicate_to_repeat_dir(
        video_path,
        req.target_role_dir,
        fp.get("hashes") or [],
        test_mode=test_mode,
        fingerprints_in_role=fps_in_role,
    )
    if result.get("success") and result.get("moved"):
        DB.update_fingerprint(req.fingerprint_id, video_path=result["target_path"])
        # 若 mapping 存在，同步更新其 video_path
        if fp.get("mapping_id"):
            DB.update_mapping(fp["mapping_id"], video_path=result["target_path"])
    return {"ok": result.get("success", False), **result}


@router.post("/api/dedup/archive-all")
async def dedup_archive_all(
    background_tasks: BackgroundTasks,
    task_id: Optional[int] = None,
    only_within_dir: Optional[str] = None,
    test_mode: Optional[bool] = None,
):
    """对一个角色目录（或某任务）下的所有标记为重复的指纹执行归档。"""
    rows = DB.list_fingerprints(task_id=task_id, only_duplicates=True,
                                only_within_dir=only_within_dir)
    if not rows:
        return {"ok": True, "message": "没有需要归档的重复视频"}
    tm = test_mode if test_mode is not None else settings.test_preview_mode
    moved, skipped, preview = 0, 0, 0

    for fp in rows:
        if not fp.get("duplicate_of"):
            continue
        if fp.get("ignored"):
            skipped += 1
            continue
        # 尝试用 video_path 的父目录作为 role_dir
        vp = fp.get("video_path") or fp.get("original_video_path") or ""
        role_dir = os.path.dirname(vp)
        # 如果已经在 _重复文件_ 里，向上追溯父目录直到不包含重复文件夹名（只向上回跳一层）
        if os.path.basename(role_dir) == settings.dedup_repeat_folder_name:
            role_dir = os.path.dirname(role_dir)
        if not os.path.isdir(role_dir):
            skipped += 1
            continue
        if not os.path.exists(vp):
            skipped += 1
            continue
        fps_in_role = DB.list_fingerprints(only_within_dir=role_dir)
        result = move_duplicate_to_repeat_dir(
            vp, role_dir, fp.get("hashes") or [],
            test_mode=tm, fingerprints_in_role=fps_in_role,
        )
        if not result.get("success"):
            skipped += 1
            continue
        if result.get("moved"):
            moved += 1
            DB.update_fingerprint(fp["fingerprint_id"], video_path=result["target_path"])
            if fp.get("mapping_id"):
                DB.update_mapping(fp["mapping_id"], video_path=result["target_path"])
        elif result.get("test_mode"):
            preview += 1

    return {
        "ok": True,
        "test_mode": tm,
        "moved": moved,
        "preview": preview,
        "skipped": skipped,
        "message": f"执行完成：移动 {moved}，预览 {preview}，跳过 {skipped}",
    }


@router.get("/api/dedup/open-folder")
async def dedup_open_folder(fingerprint_id: int, mode: str = "video"):
    """
    返回重复文件所在目录（或主视频所在目录）和资源管理器打开命令，
    由前端 / Electron 调用打开系统文件管理器定位。
    mode: 'video'=重复视频所在目录；'repeat-root'=该角色目录下的 _重复文件_ 根目录
    """
    fp = DB.get_fingerprint(fingerprint_id)
    if not fp:
        raise HTTPException(404, "指纹ID不存在")

    if mode == "repeat-root":
        # 通过角色目录推导 _重复文件_
        vp = fp.get("video_path") or fp.get("original_video_path") or ""
        dir_up = os.path.dirname(vp)
        while dir_up:
            parent = os.path.dirname(dir_up)
            if not parent or parent == dir_up:
                break
            if os.path.basename(dir_up) == settings.dedup_repeat_folder_name:
                target_dir = dir_up
                break
            dir_up = parent
        else:
            # 没找到 repeat root，用 video 所在目录
            target_dir = os.path.dirname(vp) or ""
    else:
        vp = fp.get("video_path") or fp.get("original_video_path") or ""
        target_dir = os.path.dirname(vp) or ""

    if not target_dir or not os.path.exists(target_dir):
        return {"ok": False, "message": "目录不存在或路径为空", "folder": target_dir}

    # 返回各平台打开命令，前端根据当前系统选择
    import platform
    cmd_win = f'explorer /select,"{vp}"' if mode == "video" and os.path.exists(vp) else f"explorer {target_dir}"
    cmd_mac = f'open -R "{vp}"' if mode == "video" and os.path.exists(vp) else f'open "{target_dir}"'
    cmd_linux = f'xdg-open "{target_dir}"'

    return {
        "ok": True,
        "folder": target_dir,
        "video": (vp if mode == "video" else None),
        "commands": {
            "windows": cmd_win,
            "darwin": cmd_mac,
            "linux": cmd_linux,
        },
        "auto_command": {
            "Windows": cmd_win, "Darwin": cmd_mac, "Linux": cmd_linux,
        }.get(platform.system(), cmd_linux),
    }


@router.get("/api/dedup/config")
async def dedup_config():
    """返回当前去重配置（只读）。"""
    return {
        "ok": True,
        "dedup_enabled": settings.dedup_enabled,
        "dedup_keyframes": settings.dedup_keyframes,
        "dedup_duration_tolerance_sec": settings.dedup_duration_tolerance_sec,
        "dedup_min_hash_matches": settings.dedup_min_hash_matches,
        "dedup_repeat_folder_name": settings.dedup_repeat_folder_name,
        "dedup_nesting": settings.dedup_nesting,
    }


@router.post("/api/dedup/recheck-duplicate")
async def dedup_recheck(fingerprint_id: int):
    """对某个指纹重新在候选中比对 duplicate_of 并更新。"""
    fp = DB.get_fingerprint(fingerprint_id)
    if not fp:
        raise HTTPException(404, "指纹ID不存在")
    cands = DB.find_duplicate_candidates(
        fp.get("duration_sec"), fp.get("width"), fp.get("height"),
        int(fp.get("file_size") or 0),
        duration_tolerance=settings.dedup_duration_tolerance_sec,
    )
    # 过滤自己
    cands = [c for c in cands if int(c["fingerprint_id"]) != int(fp["fingerprint_id"])]
    from ..services.video_hasher import VideoFingerprint, find_matching_master
    vf = VideoFingerprint(
        fp.get("video_path") or "", fp.get("duration_sec"),
        fp.get("width"), fp.get("height"),
        int(fp.get("file_size") or 0), list(fp.get("hashes") or []),
    )
    mid = find_matching_master(vf, cands)
    DB.update_fingerprint(fingerprint_id, duplicate_of=mid)
    return {"ok": True, "duplicate_of": mid,
            "message": ("重复" if mid else "未发现重复")}
