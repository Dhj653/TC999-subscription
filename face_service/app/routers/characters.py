"""
角色管理 API：角色CRUD / 重命名联动文件夹 / 软删除（不删磁盘文件）/ 打开文件夹 / 工作文件夹 KV / 缩略图
"""
from __future__ import annotations

import os
import platform
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel

from .. import database as DB
from ..config import settings
from ..services.file_mover import rename_character_folder
from ..utils.logger import get_logger

log = get_logger()
router = APIRouter(tags=["characters"])


class RenameReqBody(BaseModel):
    character_id: Optional[int] = None
    new_name: Optional[str] = None


class DeleteReq(BaseModel):
    character_id: int


class SetSettingReq(BaseModel):
    key: str
    value: str


# ----------------------------------------------------------------------------
@router.get("/api/characters/list")
async def list_characters(include_deleted: bool = Query(False)):
    items = DB.list_characters(include_deleted=include_deleted)
    for it in items:
        it.pop("feature_json", None)  # 不返回大体积 JSON（缩略图走专用接口）
    return {"ok": True, "items": items, "total": len(items)}


@router.get("/api/characters/get")
async def get_character(character_id: int):
    c = DB.get_character(character_id)
    if not c:
        raise HTTPException(404, "角色不存在")
    c.pop("feature_json", None)
    return {"ok": True, "character": c}


@router.put("/api/characters/rename")
async def rename_character(
    character_id: Optional[int] = Query(None),
    new_name: Optional[str] = Query(None),
):
    # 同时兼容 query 传参 和 body 传参；前端 Vue 用的是 query
    if character_id is None or not new_name:
        raise HTTPException(400, "参数缺失: character_id / new_name")
    nm = new_name.strip()
    if not nm:
        raise HTTPException(400, "new_name 不能为空")
    ch = DB.get_character(character_id)
    if not ch:
        raise HTTPException(404, "角色不存在")
    old_folder = ch.get("folder_path")
    new_folder_path = None
    extra_msg = ""
    if old_folder and os.path.isdir(old_folder):
        rr = rename_character_folder(old_folder, nm)
        if rr.get("success"):
            new_folder_path = rr["new_path"]
            extra_msg = f" 已同步重命名文件夹：{os.path.basename(old_folder)} → {os.path.basename(new_folder_path)}"
        else:
            extra_msg = f" ⚠️ 文件夹未同步：{rr.get('message')}"
    DB.update_character(character_id, name=nm, **({} if new_folder_path is None else {"folder_path": new_folder_path}))
    return {"ok": True, "message": "角色已重命名" + extra_msg,
            "folder_renamed": bool(new_folder_path and new_folder_path != old_folder)}


@router.post("/api/characters/delete")
async def delete_character(req: DeleteReq):
    ch = DB.get_character(req.character_id)
    if not ch:
        raise HTTPException(404, "角色不存在")
    DB.delete_character(req.character_id)
    return {"ok": True, "message": "角色已软删除（未删除任何磁盘文件夹或视频）",
            "folder_path": ch.get("folder_path")}


@router.get("/api/characters/thumbnail")
async def character_thumbnail(character_id: int):
    ch = DB.get_character(character_id)
    if not ch:
        raise HTTPException(404, "角色不存在")
    p = ch.get("thumbnail_path")
    if not p or not os.path.exists(p):
        # 返回占位 SVG
        svg = """<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160' viewBox='0 0 160 160'>
        <rect width='160' height='160' fill='#e5e7eb'/>
        <text x='50%' y='52%' text-anchor='middle' font-size='66' fill='#9ca3af'
              font-family='Arial, sans-serif' dominant-baseline='middle'>?</text></svg>"""
        return Response(content=svg, media_type="image/svg+xml")
    ext = os.path.splitext(p)[1].lower()
    mt = "image/jpeg" if ext in (".jpg", ".jpeg") else (
        "image/png" if ext == ".png" else "image/webp" if ext == ".webp" else "application/octet-stream")
    try:
        with open(p, "rb") as f:
            data = f.read()
    except OSError as e:
        raise HTTPException(500, f"缩略图读取失败: {e}")
    return Response(content=data, media_type=mt)


@router.get("/api/characters/open-folder")
async def open_character_folder(character_id: int):
    ch = DB.get_character(character_id)
    if not ch:
        raise HTTPException(404, "角色不存在")
    folder = ch.get("folder_path") or ""
    if not folder or not os.path.exists(folder):
        return {"ok": False, "message": "该角色尚未创建/无对应磁盘文件夹（视频不足 2 个时不建夹）",
                "folder": folder}
    cmd_win = f"explorer {folder}"
    cmd_mac = f'open "{folder}"'
    cmd_linux = f'xdg-open "{folder}"'
    auto_cmd = {"Windows": cmd_win, "Darwin": cmd_mac, "Linux": cmd_linux}.get(
        platform.system(), cmd_linux)
    return {
        "ok": True, "folder": folder, "character_id": character_id,
        "commands": {"windows": cmd_win, "darwin": cmd_mac, "linux": cmd_linux},
        "auto_command": auto_cmd,
    }


@router.get("/api/characters/get-setting")
async def get_setting(key: str):
    return {"ok": True, "key": key, "value": DB.get_setting(key)}


@router.post("/api/characters/set-setting")
async def set_setting(req: SetSettingReq):
    # 保留字段：工作文件夹 last_work_folder、是否自动扫描 work_folder_auto_scan
    allowed_prefixes = ("last_", "work_folder_", "dedup_", "ui_")
    if not any(req.key.startswith(p) for p in allowed_prefixes) and req.key.count("/") == 0 and len(req.key) < 64:
        # 合法 key（短、无路径分隔符）
        pass
    DB.set_setting(req.key, req.value)
    return {"ok": True, "message": "已保存"}
