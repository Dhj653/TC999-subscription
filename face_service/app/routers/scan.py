"""扫描与任务状态接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import settings, set_runtime
from ..models import ApiResponse, ScanRequest
from ..services import task_queue
from ..services.path_safety import is_safe_output_path, validate_scan_folder

router = APIRouter()


@router.post("/api/scan_folder", response_model=ApiResponse)
async def scan_folder(req: ScanRequest):
    """启动人脸分析任务（支持压缩包内视频）。"""
    if not validate_scan_folder(req.scan_folder):
        raise HTTPException(status_code=400, detail="扫描目录无效或不存在")

    if req.output_dir and not is_safe_output_path(req.output_dir, req.scan_folder):
        raise HTTPException(status_code=400, detail="输出路径不安全或非法")

    # 运行时同步相似度与测试模式
    set_runtime("face_similarity_threshold", req.similarity)
    set_runtime("test_preview_mode", req.test_mode)

    task_id = await task_queue.enqueue_scan(
        scan_folder=req.scan_folder,
        output_dir=req.output_dir,
        test_mode=req.test_mode,
        similarity=req.similarity,
    )
    return ApiResponse(
        success=True,
        message="任务已入队，开始串行处理",
        data={"task_id": task_id, "test_mode": req.test_mode},
    )


@router.get("/api/task_status", response_model=ApiResponse)
async def task_status():
    """获取任务进度、队列、运行日志。"""
    status = task_queue.get_status()
    return ApiResponse(success=True, data=status)


@router.post("/api/stop_task", response_model=ApiResponse)
async def stop_task():
    """停止当前任务（优雅停止，可断点续跑）。"""
    status = task_queue.get_status()
    tid = status.get("running_task_id")
    if tid:
        task_queue.stop_task(tid)
        return ApiResponse(success=True, message=f"已发送停止信号到任务 {tid}")
    return ApiResponse(success=True, message="当前无运行中任务")


@router.get("/api/config", response_model=ApiResponse)
async def get_config():
    """返回当前配置，供前端初始化滑块/开关。"""
    return ApiResponse(
        success=True,
        data={
            "similarity": settings.face_similarity_threshold,
            "test_preview_mode": settings.test_preview_mode,
            "face_yaw_threshold": settings.face_yaw_threshold,
            "face_blur_threshold": settings.face_blur_threshold,
            "face_det_score": settings.face_det_score,
            "video_frame_interval": settings.video_frame_interval,
            "video_max_frames": settings.video_max_frames,
        },
    )
