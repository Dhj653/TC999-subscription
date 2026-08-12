"""FastAPI 应用入口。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import database as DB
from .config import resolve_path, settings
from .routers import characters, dedup, groups, operations, scan
from .services import task_queue
from .utils.logger import get_logger

log = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：幂等建表 + 列迁移 + 断点续跑 + 缩略图目录确保
    DB.init_db()
    thumb_dir = Path(resolve_path(settings.thumbnail_dir))
    thumb_dir.mkdir(parents=True, exist_ok=True)
    try:
        restored = task_queue.resume_pending()
        if restored:
            log.info("已恢复 %d 个未完成任务（标记 cancelled，用户可手动重跑）", restored)
    except Exception as e:  # noqa: BLE001
        log.warning("恢复任务失败（忽略，不影响启动）: %s", e)
    log.info("face_service 启动: http://%s:%d  口罩兼容=%s  建夹阈值>=%d",
             settings.service_host, settings.service_port,
             settings.face_mask_tolerant, settings.folder_create_min_videos)
    yield
    log.info("face_service 关闭")


app = FastAPI(
    title="萤核-人脸视频分类外挂服务 v2",
    version="2.0.0",
    description="基于 InsightFace + 角色库的人脸视频聚类：支持口罩识别、>=2视频建夹、角色命名与文件夹联动重命名",
    lifespan=lifespan,
)

# CORS：允许萤核前端（任意端口 / 任意 Origin）访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(scan.router)
app.include_router(groups.router)
app.include_router(operations.router)
app.include_router(characters.router)
app.include_router(dedup.router)


# ===== 健康检查 =====
@app.get("/", tags=["health"])
async def health_root():
    return {"status": "ok", "service": "face_service", "version": "2.0.0",
            "port": settings.service_port}


@app.get("/api/health", tags=["health"])
async def api_health():
    return {"success": True, "message": "face_service 运行中",
            "version": "2.0.0"}


# ===== 缩略图静态目录（/thumbnails/ch_xxx.jpg）=====
try:
    tp = resolve_path(settings.thumbnail_dir)
    Path(tp).mkdir(parents=True, exist_ok=True)
    app.mount("/thumbnails", StaticFiles(directory=tp), name="thumbnails")
except Exception as e:  # noqa: BLE001
    log.warning("挂载缩略图静态目录失败（忽略）: %s", e)
