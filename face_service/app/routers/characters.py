"""
【新增】角色库接口（character_manager 页面使用）：
  - 列表（含缩略图相对路径 / 文件夹路径 / 视频数）
  - 重命名 → 联动重命名磁盘文件夹
  - 删除角色 → 仅软删数据库，不删文件夹/视频
  - 打开/定位文件夹 → 返回 folder_path 的绝对路径，前端通过 IPC 调用系统 explorer
  - 设置工作文件夹 + 获取工作文件夹
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from .. import database as DB
from ..config import resolve_path, settings
from ..models import ApiResponse
from ..services.file_mover import rename_character_folder
from ..utils.logger import get_logger

log = get_logger()
router = APIRouter()


def _serialize_char(c: dict, with_feature: bool = False) -> dict:
    out = {
        "character_id": c["character_id"],
        "name": c["name"],
        "original_name": c["original_name"],
        "thumbnail_path": c.get("thumbnail_path"),
        "folder_path": c.get("folder_path"),
        "video_count": c.get("video_count", 0),
        "status": c.get("status", "active"),
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
    }
    if with_feature:
        out["feature"] = c.get("feature") or []
    return out


@router.get("/api/characters", response_model=ApiResponse)
async def list_characters(include_deleted: bool = False):
    """角色列表（供角色管理页面展示）。"""
    chars = DB.list_characters(include_deleted=include_deleted)
    data = [_serialize_char(c) for c in chars]
    return ApiResponse(success=True, data={"characters": data, "count": len(data)})


@router.get("/api/characters/{character_id}", response_model=ApiResponse)
async def get_character(character_id: int):
    c = DB.get_character(character_id)
    if not c:
        raise HTTPException(status_code=404, detail="角色不存在")
    return ApiResponse(success=True, data=_serialize_char(c, with_feature=True))


@router.put("/api/characters/rename", response_model=ApiResponse)
async def rename_character(character_id: int, new_name: str = Query(..., min_length=1, max_length=128)):
    """
    重命名角色：
    1. 更新数据库的 name
    2. 若 folder_path 存在 → 联动重命名磁盘文件夹
    3. 若重命名磁盘文件夹成功 → 更新 character.folder_path 和所有关联 group_name 与 mapping 中的路径
    """
    c = DB.get_character(character_id)
    if not c:
        raise HTTPException(status_code=404, detail="角色不存在")
    new_name = new_name.strip()
    old_name = c["name"]
    if old_name == new_name:
        return ApiResponse(success=True, message="名称无变化",
                           data={"character_id": character_id, "name": new_name})

    # 1. DB 更新名字（不更新 folder_path，下面联动时再更新）
    DB.update_character(character_id, name=new_name)

    # 2. 联动文件夹重命名
    old_folder = c.get("folder_path")
    rename_result: dict = {"success": False}
    new_folder_path: Optional[str] = None
    if old_folder and os.path.exists(old_folder):
        rename_result = rename_character_folder(old_folder, new_name)
        if rename_result.get("success"):
            new_folder_path = rename_result.get("new_path")
            DB.update_character(character_id, folder_path=new_folder_path)

    # 3. 更新 face_person_group 中 linked_character 且名字 == old_name 的分组名（同任务）
    try:
        from ..database import list_groups, update_group as _ug
        for g in list_groups():
            if g["status"] == "linked_character" and g["group_name"] == old_name:
                _ug(g["group_id"], group_name=new_name)
    except Exception as e:  # noqa: BLE001
        log.warning("重命名角色时同步分组名失败（忽略）: %s", e)

    msg = f"角色 #{character_id} [{old_name}] → [{new_name}]"
    if new_folder_path:
        msg += f"；磁盘文件夹已同步重命名"
    elif old_folder and not rename_result.get("success"):
        msg += f"；⚠ 磁盘文件夹未同步：{rename_result.get('message','')}"
    else:
        msg += "（文件夹尚未创建，无需同步）"

    return ApiResponse(success=True, message=msg, data={
        "character_id": character_id, "old_name": old_name, "new_name": new_name,
        "old_folder": old_folder, "new_folder": new_folder_path,
    })


@router.post("/api/characters/delete", response_model=ApiResponse)
async def delete_character(character_id: int):
    """
    删除角色（仅软删，不删磁盘文件夹 / 视频 / group / mapping）。
    符合需求："不能删除文件夹和视频，由用户来决定是否删除"。
    """
    c = DB.get_character(character_id)
    if not c:
        raise HTTPException(status_code=404, detail="角色不存在")
    DB.delete_character(character_id)
    folder = c.get("folder_path") or "（尚未创建文件夹）"
    return ApiResponse(
        success=True,
        message=(f"角色 #{character_id} [{c['name']}] 已删除。"
                 f"文件夹位置保留：{folder}，请用户自行决定是否物理删除。"),
        data={"character_id": character_id, "folder_path": c.get("folder_path")},
    )


@router.post("/api/characters/open_folder", response_model=ApiResponse)
async def open_character_folder(character_id: int):
    """
    返回该角色的 folder_path 绝对路径。
    前端通过 window.electronAPI / IPC 调用：
        Windows: explorer.exe /select,"<path>"
        macOS:   open -R "<path>"
        Linux:   xdg-open "<parent>"
    注意：后端不直接执行打开，防止服务器/客户端不在同一机器；由前端 UI 决定。
    """
    c = DB.get_character(character_id)
    if not c:
        raise HTTPException(status_code=404, detail="角色不存在")
    fp = c.get("folder_path")
    if not fp:
        return ApiResponse(success=False,
                           message="该角色尚未创建文件夹（视频数未达阈值 / 还没执行移动）。",
                           data={"character_id": character_id})
    exists = os.path.exists(fp)
    return ApiResponse(
        success=True,
        message=("文件夹存在，可打开定位" if exists else "文件夹路径已记录但磁盘不存在"),
        data={"character_id": character_id,
              "folder_path": fp,
              "exists": exists,
              "platform": sys.platform,
              "command_hint": _build_open_hint(fp, sys.platform)},
    )


def _build_open_hint(path: str, platform: str) -> str:
    """给出各平台在前端中调用命令的提示字符串（仅参考）。"""
    if platform.startswith("win"):
        return f'explorer.exe /select,"{path}"'
    if platform == "darwin":
        return f'open -R "{path}"'
    parent = os.path.dirname(path) or path
    return f'xdg-open "{parent}"'


# ================= 缩略图静态 =================
@router.get("/api/characters/thumbnail/{character_id}")
async def get_character_thumbnail(character_id: int):
    """返回角色缩略图（如果存在）。"""
    c = DB.get_character(character_id)
    if not c or not c.get("thumbnail_path"):
        raise HTTPException(status_code=404, detail="该角色暂无缩略图")
    tp = c["thumbnail_path"]
    if not os.path.isabs(tp):
        tp = resolve_path(tp)
    if not os.path.exists(tp):
        raise HTTPException(status_code=404, detail="缩略图文件不存在")
    return FileResponse(tp, media_type="image/jpeg")


# ================= 工作文件夹设置 =================
@router.get("/api/settings/work_folder", response_model=ApiResponse)
async def get_work_folder():
    """返回上次配置的工作文件夹。"""
    work = DB.get_setting("last_work_folder") or ""
    auto = DB.get_setting("work_folder_auto_scan") or "false"
    return ApiResponse(success=True, data={
        "work_folder": work,
        "auto_scan_on_start": auto.lower() in {"1", "true", "yes", "on"},
    })


@router.post("/api/settings/work_folder", response_model=ApiResponse)
async def set_work_folder(work_folder: str, auto_scan_on_start: bool = False):
    """设置工作文件夹路径（供前端"工作文件夹"输入框保存使用）。"""
    if not work_folder:
        raise HTTPException(status_code=400, detail="路径不能为空")
    if not os.path.isdir(work_folder):
        raise HTTPException(status_code=400, detail="路径不存在或不是文件夹")
    DB.set_setting("last_work_folder", work_folder)
    DB.set_setting("work_folder_auto_scan", "true" if auto_scan_on_start else "false")
    return ApiResponse(success=True, message="工作文件夹已保存",
                       data={"work_folder": work_folder,
                             "auto_scan_on_start": auto_scan_on_start})
