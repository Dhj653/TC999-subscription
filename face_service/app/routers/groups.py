"""人物分组相关接口。"""
from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, HTTPException

from ..database import (
    get_group,
    list_groups,
    list_mappings,
    update_group,
    update_mapping,
)
from ..models import ApiResponse, MergeGroupRequest

router = APIRouter()

STATUS_LABELS = {
    "auto_numbered": "自动编号",
    "renamed": "已命名",
    "name_conflict": "多名称冲突",
    "multi_person": "多人",
    "deleted": "已删除",
    "merged": "已合并",
    "linked_character": "已关联角色库",
}


@router.get("/api/person_groups", response_model=ApiResponse)
async def person_groups(task_id: int | None = None):
    groups = list_groups(task_id)
    out = []
    for g in groups:
        mappings = list_mappings(group_id=g["group_id"])
        out.append({
            "group_id": g["group_id"], "task_id": g["task_id"],
            "group_name": g["group_name"],
            "original_group_name": g["original_group_name"],
            "status": g["status"],
            "status_label": STATUS_LABELS.get(g["status"], g["status"]),
            "video_count": g["video_count"],
            "extracted_names": json.loads(g["extracted_names"]) if g["extracted_names"] else [],
            "videos": [
                {"mapping_id": m["id"], "video_path": m["video_path"],
                 "original_video_path": m["original_video_path"],
                 "moved": bool(m["moved"]), "source": m["source"],
                 "archive_path": m["archive_path"], "in_archive_name": m["in_archive_name"]}
                for m in mappings
            ],
        })
    return ApiResponse(success=True, data={"groups": out})


@router.get("/api/group_extract_names", response_model=ApiResponse)
async def group_extract_names(group_id: int):
    g = get_group(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    names: List[str] = json.loads(g["extracted_names"]) if g["extracted_names"] else []
    return ApiResponse(
        success=True,
        data={"group_id": group_id, "extracted_names": names,
              "has_conflict": len(set(names)) >= 2},
    )


@router.put("/api/group_rename", response_model=ApiResponse)
async def group_rename(group_id: int, new_name: str):
    if not new_name or not new_name.strip():
        raise HTTPException(status_code=400, detail="新名称不能为空")
    new_name = new_name.strip()[:128]
    g = get_group(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    old = g["group_name"]
    update_group(group_id, group_name=new_name, status="renamed")
    return ApiResponse(
        success=True, message=f"分组 {group_id} 名称已由 [{old}] 改为 [{new_name}]",
        data={"group_id": group_id, "old_name": old, "new_name": new_name},
    )


@router.post("/api/merge_group", response_model=ApiResponse)
async def merge_group(req: MergeGroupRequest):
    src = get_group(req.source_group_id)
    tgt = get_group(req.target_group_id)
    if not src or not tgt:
        raise HTTPException(status_code=404, detail="源或目标分组不存在")
    if src["group_id"] == tgt["group_id"]:
        raise HTTPException(status_code=400, detail="不能与自身合并")
    mappings = list_mappings(group_id=req.source_group_id)
    moved = 0
    for m in mappings:
        update_mapping(m["id"], group_id=req.target_group_id)
        moved += 1
    tgt_mappings = list_mappings(group_id=req.target_group_id)
    update_group(req.target_group_id, video_count=len(tgt_mappings))
    src_names = set(json.loads(src["extracted_names"]) if src["extracted_names"] else [])
    tgt_names = set(json.loads(tgt["extracted_names"]) if tgt["extracted_names"] else [])
    merged = list(src_names | tgt_names)
    new_status = "name_conflict" if len(merged) >= 2 else (
        "renamed" if len(merged) == 1 else tgt["status"]
    )
    update_group(req.target_group_id,
                 extracted_names=json.dumps(merged, ensure_ascii=False),
                 status=new_status)
    update_group(req.source_group_id, status="merged", video_count=0)
    return ApiResponse(
        success=True,
        message=f"已将 {moved} 个视频从 [{src['group_name']}] 合并到 [{tgt['group_name']}]",
        data={"moved": moved, "merged_names": merged, "new_status": new_status},
    )


@router.post("/api/delete_group", response_model=ApiResponse)
async def delete_group(group_id: int):
    """仅标记分组为已删除（映射保留用于审计，不删磁盘文件）。"""
    g = get_group(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    mappings = list_mappings(group_id=group_id)
    update_group(group_id, status="deleted", video_count=0)
    return ApiResponse(
        success=True,
        message=f"分组 {group_id} 已标记删除（{len(mappings)} 条映射保留用于审计）",
    )
