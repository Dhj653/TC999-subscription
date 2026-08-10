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
)
from ..models import ApiResponse, MergeGroupRequest

router = APIRouter()

# 分组状态 → 前端标签
STATUS_LABELS = {
    "auto_numbered": "自动编号",
    "renamed": "已命名",
    "name_conflict": "多名称冲突",
    "multi_person": "多人",
}


@router.get("/api/person_groups", response_model=ApiResponse)
async def person_groups(task_id: int | None = None):
    """获取全部人物分组与关联视频列表，附带分组状态标签。"""
    groups = list_groups(task_id)
    out = []
    for g in groups:
        mappings = list_mappings(group_id=g["group_id"])
        out.append({
            "group_id": g["group_id"],
            "task_id": g["task_id"],
            "group_name": g["group_name"],
            "original_group_name": g["original_group_name"],
            "status": g["status"],
            "status_label": STATUS_LABELS.get(g["status"], g["status"]),
            "video_count": g["video_count"],
            "extracted_names": json.loads(g["extracted_names"]) if g["extracted_names"] else [],
            "videos": [
                {
                    "mapping_id": m["id"],
                    "video_path": m["video_path"],
                    "original_video_path": m["original_video_path"],
                    "moved": bool(m["moved"]),
                    "source": m["source"],
                    "archive_path": m["archive_path"],
                    "in_archive_name": m["in_archive_name"],
                }
                for m in mappings
            ],
        })
    return ApiResponse(success=True, data={"groups": out})


@router.get("/api/group_extract_names", response_model=ApiResponse)
async def group_extract_names(group_id: int):
    """获取该分组下所有视频文件名解析出来的全部人名列表。"""
    g = get_group(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    names: List[str] = json.loads(g["extracted_names"]) if g["extracted_names"] else []
    return ApiResponse(
        success=True,
        data={"group_id": group_id, "extracted_names": names, "has_conflict": len(set(names)) >= 2},
    )


@router.put("/api/group_rename", response_model=ApiResponse)
async def group_rename(group_id: int, new_name: str):
    """手动修改分组名称（group_id / new_name 走 query 参数）。"""
    if not new_name or not new_name.strip():
        raise HTTPException(status_code=400, detail="新名称不能为空")
    new_name = new_name.strip()[:128]
    g = get_group(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    old = g["group_name"]
    update_group(group_id, group_name=new_name, status="renamed")
    return ApiResponse(
        success=True,
        message=f"分组 {group_id} 名称已由 [{old}] 改为 [{new_name}]",
        data={"group_id": group_id, "old_name": old, "new_name": new_name},
    )


@router.post("/api/merge_group", response_model=ApiResponse)
async def merge_group(req: MergeGroupRequest):
    """合并两个人物分组：将 source 下全部视频迁移到 target，删除 source 分组。"""
    src = get_group(req.source_group_id)
    tgt = get_group(req.target_group_id)
    if not src or not tgt:
        raise HTTPException(status_code=404, detail="源或目标分组不存在")
    if src["group_id"] == tgt["group_id"]:
        raise HTTPException(status_code=400, detail="不能与自身合并")

    mappings = list_mappings(group_id=req.source_group_id)
    moved = 0
    for m in mappings:
        update_group_mapping_group(m["id"], req.target_group_id, tgt["group_name"])
        moved += 1
    # 更新目标分组视频计数与提取人名
    tgt_mappings = list_mappings(group_id=req.target_group_id)
    update_group(req.target_group_id, video_count=len(tgt_mappings))
    # 合并提取人名
    import json as _json
    src_names = set(_json.loads(src["extracted_names"]) if src["extracted_names"] else [])
    tgt_names = set(_json.loads(tgt["extracted_names"]) if tgt["extracted_names"] else [])
    merged = list((src_names | tgt_names))
    # 人名冲突判定
    new_status = "name_conflict" if len(merged) >= 2 else (
        "renamed" if len(merged) == 1 else tgt["status"]
    )
    update_group(
        req.target_group_id,
        extracted_names=_json.dumps(merged, ensure_ascii=False),
        status=new_status,
    )
    # 删除源分组（级联由外键处理；此处手动删映射）
    from ..database import update_group as _ug
    # 标记源分组为已合并（保留记录），不物理删除以免丢回滚信息
    _ug(req.source_group_id, status="merged", video_count=0)
    return ApiResponse(
        success=True,
        message=f"已将 {moved} 个视频从 [{src['group_name']}] 合并到 [{tgt['group_name']}]",
        data={"moved": moved, "merged_names": merged, "new_status": new_status},
    )


@router.post("/api/delete_group", response_model=ApiResponse)
async def delete_group(group_id: int):
    """删除错误分组（仅删除分组与映射记录，不删磁盘文件）。"""
    from ..database import get_group, list_mappings, update_group, update_mapping

    g = get_group(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="分组不存在")
    # 删除映射记录
    mappings = list_mappings(group_id=group_id)
    for m in mappings:
        update_mapping(m["id"], group_id=-1)  # 标记无效；此处简化为保留
    update_group(group_id, status="deleted", video_count=0)
    return ApiResponse(
        success=True,
        message=f"分组 {group_id} 已标记删除（{len(mappings)} 条映射保留用于审计）",
    )


def update_group_mapping_group(mapping_id: int, new_group_id: int, new_group_name: str) -> None:
    """把某条映射的 group_id 切换到新分组。"""
    from ..database import update_mapping

    update_mapping(
        mapping_id,
        group_id=new_group_id,
        original_group_name=new_group_name,
    )
