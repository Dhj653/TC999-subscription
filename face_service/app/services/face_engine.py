"""
InsightFace 人脸引擎封装。
- 仅使用本地模型（路径在 .env 配置），禁止联网下载（受 DISABLE_MODEL_DOWNLOAD 控制）。
- 人脸过滤：仅保留女性 + 正脸（yaw/roll 在阈值内）+ 非模糊（拉普拉斯方差）+ 检测置信度达标。
- 输出归一化人脸特征向量，供 FAISS 聚类。
"""
from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np

from ..config import settings
from ..utils.logger import get_logger

log = get_logger()


@dataclass
class FaceFeature:
    embedding: np.ndarray          # (512,) float32 归一化
    det_score: float
    gender: int
    yaw_deg: float
    blur_score: float
    bbox: tuple  # (x1,y1,x2,y2)


class FaceEngine:
    _instance: Optional["FaceEngine"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._app = None
        self._ready = False

    @classmethod
    def instance(cls) -> "FaceEngine":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = FaceEngine()
        return cls._instance

    def init(self) -> None:
        if self._ready:
            return
        try:
            from insightface.app import FaceAnalysis
        except ImportError as e:
            raise RuntimeError("未安装 insightface，请 pip install -r requirements.txt") from e

        root = Path(settings.insightface_root).expanduser().resolve()
        name = os.getenv("INSIGHTFACE_MODEL_NAME", "buffalo_l")
        # 模型包路径：{root}/models/{name}（insightface 约定）
        pack_dir = root / "models" / name

        if settings.disable_model_download and not pack_dir.exists():
            raise RuntimeError(
                f"本地未找到模型包：{pack_dir}。"
                f"请手动下载 buffalo_l.zip 解压到该目录，或在 .env 设置 DISABLE_MODEL_DOWNLOAD=false 允许联网下载。"
            )

        try:
            providers = ["CPUExecutionProvider"]
            self._app = FaceAnalysis(
                name=name,
                root=str(root),
                providers=providers,
                allowed_modules=["detection", "genderage", "recognition"],
            )
            self._app.prepare(
                ctx_id=settings.insightface_ctx,
                det_size=(640, 640),
            )
        except Exception as e:
            # 模型缺失时 insightface 会尝试联网下载；这里捕获并提示
            if settings.disable_model_download:
                raise RuntimeError(
                    f"模型加载失败（且已禁止联网下载）：{e}。请检查 {pack_dir} 下 onnx 文件是否齐全。"
                ) from e
            raise

        self._ready = True
        log.info("InsightFace 引擎就绪: model=%s root=%s", name, root)

    @property
    def ready(self) -> bool:
        return self._ready

    # ---------- 几何 / 质量估计 ----------
    @staticmethod
    def _yaw_roll(kps: np.ndarray) -> tuple[float, float]:
        """由 5 关键点估计 yaw/roll（度）。"""
        # kps: [[lx,ly],[rx,ry],[nx,ny],[mlx,mly],[mrx,mry]]
        lx, ly = kps[0]
        rx, ry = kps[1]
        nx, ny = kps[2]

        eye_dx = rx - lx
        eye_dy = ry - ly
        roll = math.degrees(math.atan2(eye_dy, eye_dx + 1e-6))

        d_left = abs(nx - lx)
        d_right = abs(rx - nx)
        ratio = (d_left - d_right) / (d_left + d_right + 1e-6)
        ratio = max(-1.0, min(1.0, ratio))
        yaw = math.degrees(math.asin(ratio))
        return yaw, roll

    @staticmethod
    def _blur_score(face_crop_gray: np.ndarray) -> float:
        if face_crop_gray.size == 0:
            return 0.0
        return float(cv2.Laplacian(face_crop_gray, cv2.CV_64F).var())

    # ---------- 检测 + 过滤 ----------
    def extract_valid_faces(self, img_bgr: np.ndarray) -> List[FaceFeature]:
        """对一帧图片提取【满足过滤条件】的人脸特征。"""
        if not self._ready:
            self.init()
        assert self._app is not None
        results: List[FaceFeature] = []

        faces = self._app.get(img_bgr)
        for face in faces:
            det = float(np.asarray(face.det_score).max()) if hasattr(face, "det_score") else 0.0
            if det < settings.face_det_score:
                continue

            gender = int(getattr(face, "gender", -1))
            # 仅保留目标性别（默认女性）
            if settings.face_gender_keep == "female":
                if gender != settings.face_gender_female_value:
                    continue
            else:
                if gender == settings.face_gender_female_value:
                    continue

            kps = np.asarray(face.kps, dtype=np.float32) if face.kps is not None else None
            if kps is None or kps.shape != (5, 2):
                continue

            yaw, roll = self._yaw_roll(kps)
            if abs(yaw) > settings.face_yaw_threshold or abs(roll) > settings.face_yaw_threshold:
                continue  # 侧脸 / 大角度偏转丢弃

            emb = face.normed_embedding if hasattr(face, "normed_embedding") else None
            emb = np.asarray(emb, dtype=np.float32) if emb is not None else None
            if emb is None or emb.size == 0:
                continue

            # 模糊度
            x1, y1, x2, y2 = [int(v) for v in face.bbox]
            h, w = img_bgr.shape[:2]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            crop = img_bgr[y1:y2, x1:x2]
            blur = self._blur_score(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)) if crop.size else 0.0
            if blur < settings.face_blur_threshold:
                continue  # 模糊丢弃

            results.append(
                FaceFeature(
                    embedding=emb,
                    det_score=det,
                    gender=gender,
                    yaw_deg=yaw,
                    blur_score=blur,
                    bbox=(x1, y1, x2, y2),
                )
            )
        return results

    def free_face_objects(self, faces: List[FaceFeature]) -> None:
        """显式释放单视频人脸特征对象。"""
        for f in faces:
            f.embedding = np.empty(0, dtype=np.float32)
        faces.clear()
