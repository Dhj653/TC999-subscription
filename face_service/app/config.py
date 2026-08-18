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
    # 口罩兼容：疑似遮挡时放宽角度与模糊阈值
    face_mask_tolerant: bool = _get_bool("FACE_MASK_TOLERANT", True)
    face_yaw_mask_threshold: float = _get_float("FACE_YAW_MASK_THRESHOLD", 55)
    face_blur_mask_threshold: float = _get_float("FACE_BLUR_MASK_THRESHOLD", 40)

    # —— 聚类 ——
    face_similarity_threshold: float = _get_float("FACE_SIMILARITY_THRESHOLD", 0.55)

    # —— 视频 ——
    video_frame_interval: float = _get_float("VIDEO_FRAME_INTERVAL", 2)
    video_max_frames: int = _get_int("VIDEO_MAX_FRAMES", 60)
    ffmpeg_bin: str = os.getenv("FFMPEG_BIN", "ffmpeg")
    ffprobe_bin: str = os.getenv("FFPROBE_BIN", "ffprobe")

    # —— 输出 ——
    test_preview_mode: bool = _get_bool("TEST_PREVIEW_MODE", True)
    thumbnail_dir: str = os.getenv("THUMBNAIL_DIR", "./thumbnails")
    # 【要求】≥N 个相同女性视频才建夹并移动
    folder_create_min_videos: int = _get_int("FOLDER_CREATE_MIN_VIDEOS", 2)
    single_video_policy: str = os.getenv("SINGLE_VIDEO_POLICY", "leave_in_place").lower()

    # —— 路径安全 ——
    blocked_path_roots: List[str] = _get_list(
        "BLOCKED_PATH_ROOTS",
        ["C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)", "C:\\ProgramData"],
    )

    # —— 去重（视频内容级，画面完全相同才算重复）——
    dedup_enabled: bool = _get_bool("DEDUP_ENABLED", True)
    dedup_keyframes: int = _get_int("DEDUP_KEYFRAMES", 5)
    dedup_duration_tolerance_sec: float = _get_float("DEDUP_DURATION_TOLERANCE_SEC", 2.0)
    dedup_min_hash_matches: int = _get_int("DEDUP_MIN_HASH_MATCHES", 5)
    dedup_repeat_folder_name: str = os.getenv("DEDUP_REPEAT_FOLDER_NAME", "_重复文件_")
    dedup_nesting: bool = _get_bool("DEDUP_NESTING", True)

    # —— 运行时覆盖（set_runtime 可调）——
    _runtime: dict = {}

    def get(self, key: str, default=None):
        if key in self._runtime:
            return self._runtime[key]
        return getattr(self, key, default)


settings = Settings()


def set_runtime(key: str, value) -> None:
    """运行时调节参数（如前端滑块调相似度阈值），仅内存生效，不持久化。"""
    setattr(settings, key, value)


def get_runtime_similarity() -> float:
    return max(0.0, min(1.0, settings.face_similarity_threshold))


def resolve_path(p: str) -> str:
    """把相对路径解析为相对于项目根 (face_service/) 的绝对路径；绝对路径原样返回。"""
    if not p:
        return p
    import os as _os
    if _os.path.isabs(p):
        return p
    base = Path(__file__).resolve().parent.parent  # face_service/
    return str((base / p).resolve())
