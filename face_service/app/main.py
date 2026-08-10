"""FastAPI 应用入口。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import init_db
from .routers import groups, operations, scan
from .services import task_queue
from .utils.logger import get_logger

log = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动
    init_db()
    try:
        restored = task_queue.resume_pending()
        if restored:
            log.info("已恢复 %d 个未完成任务（断点续跑）", restored)
    except Exception as e:  # noqa: BLE001
        log.warning("恢复任务失败（忽略，不影响启动）: %s", e)
    log.info("face_service 启动: http://%s:%d", settings.service_host, settings.service_port)
    yield
    # 关闭
    log.info("face_service 关闭")


app = FastAPI(
    title="萤核-人脸视频分类外挂服务",
    version="1.0.0",
    description="基于 InsightFace + FAISS 的人脸视频聚类外挂服务，端口 5002",
    lifespan=lifespan,
)

# CORS：允许萤核前端（任意端口）访问
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


@app.get("/", tags=["health"])
async def health():
    return {"status": "ok", "service": "face_service", "port": settings.service_port}


@app.get("/api/health", tags=["health"])
async def api_health():
    return {"success": True, "message": "face_service 运行中"}
