"""扫描与任务状态接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import set_runtime, settings
from ..database import get_setting, set_setting
from ..models import ApiResponse, ScanRequest
from ..services import task_queue
from ..services.path_safety import is_safe_output_path, validate_scan_folder

router = APIRouter()


@router.post("/api/scan_folder", response_model=ApiResponse)
async def scan_folder(req: ScanRequest):
    """启动人脸分析任务（支持压缩包内视频 + 角色库先验匹配）。"""
    if not validate_scan_folder(req.scan_folder):
        raise HTTPException(status_code=400, detail="扫描目录无效或不存在")
    out = req.output_dir or req.scan_folder
    if not is_safe_output_path(out, req.scan_folder):
        raise HTTPException(status_code=400, detail="输出路径不安全或非法")
    set_runtime("face_similarity_threshold", req.similarity)
    set_runtime("test_preview_mode", req.test_mode)

    try:
        task_id = await task_queue.enqueue_scan(
            scan_folder=req.scan_folder,
            output_dir=req.output_dir,
            test_mode=req.test_mode,
            similarity=req.similarity,
            use_character_library=req.use_character_library,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # 若是工作文件夹，记住该路径
    try:
        set_setting("last_work_folder", req.scan_folder)
    except Exception:  # noqa: BLE001
        pass

    return ApiResponse(
        success=True, message="任务已入队，开始串行处理",
        data={"task_id": task_id, "test_mode": req.test_mode},
    )


@router.get("/api/task_status", response_model=ApiResponse)
async def task_status():
    return ApiResponse(success=True, data=task_queue.get_status())


@router.post("/api/stop_task", response_model=ApiResponse)
async def stop_task():
    st = task_queue.get_status()
    tid = st.get("running_task_id")
    if tid:
        task_queue.stop_task(tid)
        return ApiResponse(success=True, message=f"已发送停止信号到任务 {tid}")
    return ApiResponse(success=True, message="当前无运行中任务")


@router.get("/api/config", response_model=ApiResponse)
async def get_config():
    """返回默认配置（供前端初始化滑块/开关），工作文件夹相关配置从DB读取。"""
    work_folder = get_setting("last_work_folder") or ""
    return ApiResponse(
        success=True,
        data={
            "similarity": settings.face_similarity_threshold,
            "test_preview_mode": settings.test_preview_mode,
            "face_yaw_threshold": settings.face_yaw_threshold,
            "face_yaw_mask_threshold": settings.face_yaw_mask_threshold,
            "face_blur_threshold": settings.face_blur_threshold,
            "face_blur_mask_threshold": settings.face_blur_mask_threshold,
            "face_det_score": settings.face_det_score,
            "face_mask_tolerant": settings.face_mask_tolerant,
            "video_frame_interval": settings.video_frame_interval,
            "video_max_frames": settings.video_max_frames,
            "folder_create_min_videos": settings.folder_create_min_videos,
            "single_video_policy": settings.single_video_policy,
            "uncategorized_dir_name": settings.uncategorized_dir_name,
            "work_folder": work_folder,
            "thumbnail_dir": settings.thumbnail_dir,
        },
    )
