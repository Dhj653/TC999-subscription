"""
配置加载：支持 .env 文件 + 运行时可调参数。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _get_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on", "t", "y"}


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class _Settings:
    # 服务
    service_port: int
    service_host: str
    firefly_db_path: str

    # 日志
    log_file: str
    log_level: str

    # 模型
    insightface_root: str
    insightface_ctx: int
    disable_model_download: bool

    # 人脸过滤（基础阈值）
    face_gender_keep: str
    face_gender_female_value: int
    face_yaw_threshold: float
    face_yaw_mask_threshold: float         # 口罩放宽后的阈值
    face_blur_threshold: float
    face_blur_mask_threshold: float        # 口罩放宽后的阈值
    face_det_score: float
    face_mask_tolerant: bool               # 口罩兼容模式

    # 聚类
    face_similarity_threshold: float

    # 建文件夹阈值（新需求）
    folder_create_min_videos: int
    single_video_policy: str
    uncategorized_dir_name: str

    # 视频
    video_frame_interval: float
    video_max_frames: int
    ffmpeg_bin: str
    ffprobe_bin: str

    # 输出
    test_preview_mode: bool
    thumbnail_dir: str

    # 路径安全
    blocked_path_roots: list[str]

    # ===== 运行时可调（运行时 set_runtime 覆盖 .env 默认值）=====
    _runtime: dict

    def _r(self, key: str, default: Any) -> Any:
        if key in self._runtime:
            return self._runtime[key]
        return default

    # 运行时可覆盖的属性（前端发来的值优先）
    @property
    def similarity(self) -> float:
        return self._r("face_similarity_threshold", self.face_similarity_threshold)

    @property
    def test_mode(self) -> bool:
        return self._r("test_preview_mode", self.test_preview_mode)


def _init_settings() -> _Settings:
    roots_raw = os.getenv("BLOCKED_PATH_ROOTS", "")
    blocked = [r.strip() for r in roots_raw.split(",") if r.strip()]

    return _Settings(
        service_port=_get_int("SERVICE_PORT", 5002),
        service_host=os.getenv("SERVICE_HOST", "127.0.0.1"),
        firefly_db_path=os.getenv("FIREFLY_DB_PATH", "./data/face_service.db"),
        log_file=os.getenv("LOG_FILE", "./logs/face_service.log"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        insightface_root=os.getenv("INSIGHTFACE_ROOT", "./models"),
        insightface_ctx=_get_int("INSIGHTFACE_CTX", 0),
        disable_model_download=_get_bool("DISABLE_MODEL_DOWNLOAD", False),
        face_gender_keep=os.getenv("FACE_GENDER_KEEP", "female"),
        face_gender_female_value=_get_int("FACE_GENDER_FEMALE_VALUE", 0),
        face_yaw_threshold=_get_float("FACE_YAW_THRESHOLD", 35.0),
        face_yaw_mask_threshold=_get_float("FACE_YAW_MASK_THRESHOLD", 55.0),
        face_blur_threshold=_get_float("FACE_BLUR_THRESHOLD", 80.0),
        face_blur_mask_threshold=_get_float("FACE_BLUR_MASK_THRESHOLD", 40.0),
        face_det_score=_get_float("FACE_DET_SCORE", 0.5),
        face_mask_tolerant=_get_bool("FACE_MASK_TOLERANT", True),
        face_similarity_threshold=_get_float("FACE_SIMILARITY_THRESHOLD", 0.55),
        folder_create_min_videos=_get_int("FOLDER_CREATE_MIN_VIDEOS", 2),
        single_video_policy=os.getenv("SINGLE_VIDEO_POLICY", "keep_in_place"),
        uncategorized_dir_name=os.getenv("UNCATEGORIZED_DIR_NAME", "_未分类_待补充"),
        video_frame_interval=_get_float("VIDEO_FRAME_INTERVAL", 2.0),
        video_max_frames=_get_int("VIDEO_MAX_FRAMES", 60),
        ffmpeg_bin=os.getenv("FFMPEG_BIN", "ffmpeg"),
        ffprobe_bin=os.getenv("FFPROBE_BIN", "ffprobe"),
        test_preview_mode=_get_bool("TEST_PREVIEW_MODE", True),
        thumbnail_dir=os.getenv("THUMBNAIL_DIR", "./thumbnails"),
        blocked_path_roots=blocked,
        _runtime={},
    )


settings = _init_settings()


def set_runtime(key: str, value: Any) -> None:
    """运行时设置（如前端传来的相似度、测试模式）。"""
    settings._runtime[key] = value  # noqa: SLF001


def resolve_path(p: str) -> str:
    """把相对路径解析为绝对路径（以项目根为基准）。"""
    pp = Path(p)
    if pp.is_absolute():
        return str(pp.resolve())
    root = Path(__file__).resolve().parent.parent
    return str((root / pp).resolve())
