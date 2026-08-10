"""集中配置加载：读取 .env + 萤核数据库路径，提供运行时可调参数。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# 加载 .env（若不存在则使用默认值）
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _get_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_float(key: str, default: float) -> float:
    val = os.getenv(key)
    return float(val) if val else default


def _get_int(key: str, default: int) -> int:
    val = os.getenv(key)
    return int(val) if val else default


def _get_list(key: str, default: List[str]) -> List[str]:
    val = os.getenv(key)
    if not val:
        return default
    return [p.strip() for p in val.split(",") if p.strip()]


class Settings:
    # —— 服务 ——
    service_host: str = os.getenv("SERVICE_HOST", "127.0.0.1")
    service_port: int = _get_int("SERVICE_PORT", 5002)

    # —— 萤核数据库 ——
    firefly_db_path: str = os.getenv("FIREFLY_DB_PATH", "./data.db")

    # —— 日志 ——
    log_file: str = os.getenv("LOG_FILE", "./logs/face_service.log")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    # —— InsightFace ——
    insightface_root: str = os.getenv("INSIGHTFACE_ROOT", "./models")
    insightface_ctx: int = _get_int("INSIGHTFACE_CTX", 0)
    disable_model_download: bool = _get_bool("DISABLE_MODEL_DOWNLOAD", True)

    # —— 人脸过滤 ——
    face_gender_keep: str = os.getenv("FACE_GENDER_KEEP", "female").lower()
    # InsightFace buffalo_l genderage 约定：0=女性 1=男性。
    # 若实测过滤反了，改 .env 的 FACE_GENDER_FEMALE_VALUE 即可，无需改代码。
    face_gender_female_value: int = _get_int("FACE_GENDER_FEMALE_VALUE", 0)
    face_yaw_threshold: float = _get_float("FACE_YAW_THRESHOLD", 35)
    face_blur_threshold: float = _get_float("FACE_BLUR_THRESHOLD", 80)
    face_det_score: float = _get_float("FACE_DET_SCORE", 0.5)

    # —— 聚类 ——
    face_similarity_threshold: float = _get_float("FACE_SIMILARITY_THRESHOLD", 0.55)

    # —— 视频 ——
    video_frame_interval: float = _get_float("VIDEO_FRAME_INTERVAL", 2)
    video_max_frames: int = _get_int("VIDEO_MAX_FRAMES", 60)

    # —— 输出 ——
    test_preview_mode: bool = _get_bool("TEST_PREVIEW_MODE", True)

    # —— 路径安全 ——
    blocked_path_roots: List[str] = _get_list(
        "BLOCKED_PATH_ROOTS",
        ["C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)", "C:\\ProgramData"],
    )


settings = Settings()


def set_runtime(key: str, value) -> None:
    """运行时调节参数（如前端滑块调相似度阈值），仅内存生效，不持久化。"""
    setattr(settings, key, value)


def get_runtime_similarity() -> float:
    return max(0.0, min(1.0, settings.face_similarity_threshold))
