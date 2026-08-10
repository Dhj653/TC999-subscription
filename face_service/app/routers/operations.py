"""文件操作接口：移动、回滚、重新处理单个视频。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..database import get_mapping, get_task
from ..models import ApiResponse, MoveRequest, ReprocessSingleRequest
from ..services import task_queue
from ..services.file_mover import move_all, move_mapping, revert_all

router = APIRouter()


@router.post("/api/move_files", response_model=ApiResponse)
async def move_files(req: MoveRequest):
    """执行文件移动，受测试模式开关控制。"""
    status = task_queue.get_status()
    # 若未指定 task_id，取最近一个任务
    task_id = req.task_id
    if not task_id:
        latest = status.get("latest")
        if not latest:
            raise HTTPException(status_code=404, detail="未找到任何任务")
        task_id = latest["id"]

    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    results = move_all(task_id, req.test_mode)
    moved_count = sum(1 for r in results if r.get("moved"))
    preview_count = sum(1 for r in results if r.get("test_mode"))
    msg = (
        f"已移动 {moved_count} 个文件" if not req.test_mode
        else f"[预览] 拟移动 {preview_count} 个文件（未实际写入磁盘）"
    )
    return ApiResponse(success=True, message=msg, data={"results": results, "task_id": task_id})


@router.post("/api/revert_all_files", response_model=ApiResponse)
async def revert_all_files(task_id: int):
    """一键回滚所有已移动文件、恢复分组原始名称。"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    res = revert_all(task_id)
    return ApiResponse(
        success=True,
        message=f"已回滚 {res['reverted_files']} 个文件并恢复分组原名",
        data=res,
    )


@router.post("/api/reprocess_single", response_model=ApiResponse)
async def reprocess_single(req: ReprocessSingleRequest):
    """重新处理单个视频。"""
    m = get_mapping(req.mapping_id)
    if not m:
        raise HTTPException(status_code=404, detail="映射不存在")
    res = await task_queue.reprocess_single(m["task_id"], req.mapping_id)
    if not res.get("success"):
        raise HTTPException(status_code=500, detail=res.get("message", "重新处理失败"))
    return ApiResponse(success=True, message=res["message"], data={"mapping_id": req.mapping_id})


@router.post("/api/move_single", response_model=ApiResponse)
async def move_single(mapping_id: int, test_mode: bool = True):
    """移动单个视频（受测试模式管控）。"""
    m = get_mapping(mapping_id)
    if not m:
        raise HTTPException(status_code=404, detail="映射不存在")
    task = get_task(m["task_id"])
    output_root = task["output_dir"] if task else ""
    res = move_mapping(mapping_id, output_root, test_mode)
    return ApiResponse(success=res.get("success", False), message=res.get("message", ""), data=res)
