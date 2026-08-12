"""
InsightFace 人脸引擎封装 — 新增：口罩兼容（关键点质量差时自动放宽阈值）。
- 仅使用本地模型（路径在 .env 配置）
- 人脸过滤：性别(仅女性) + 正脸 + 非模糊 + 检测置信度
- 关键点质量评估：若关键点质量较差（疑似口罩遮挡）自动启用口罩兼容阈值
- 输出归一化人脸特征向量，供 FAISS 聚类 / 角色库匹配
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
    bbox: tuple                   # (x1,y1,x2,y2)
    mask_tolerant_applied: bool   # True = 命中口罩兼容（阈值放宽）


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
        pack_dir = root / "models" / name

        if settings.disable_model_download and not pack_dir.exists():
            raise RuntimeError(
                f"本地未找到模型包：{pack_dir}。"
                f"请手动下载 buffalo_l.zip 解压到该目录，或设置 DISABLE_MODEL_DOWNLOAD=false。"
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
            if settings.disable_model_download:
                raise RuntimeError(
                    f"模型加载失败（禁止联网下载）：{e}。请检查 {pack_dir} 下 onnx 文件是否齐全。"
                ) from e
            raise

        self._ready = True
        log.info("InsightFace 引擎就绪: model=%s root=%s 口罩兼容=%s",
                 name, root, settings.face_mask_tolerant)

    @property
    def ready(self) -> bool:
        return self._ready

    # ----- 几何 / 质量估计 -----
    @staticmethod
    def _yaw_roll(kps: np.ndarray) -> tuple[float, float]:
        """由 5 关键点估计 yaw/roll（度）。"""
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

    @staticmethod
    def _keypoint_quality(kps: np.ndarray, bbox) -> tuple[bool, str]:
        """
        评估 5 关键点质量，判断是否疑似口罩遮挡。
        返回 (是否高质量, 原因说明)。
        5 点顺序: [左眼, 右眼, 鼻尖, 左嘴角, 右嘴角]
        """
        x1, y1, x2, y2 = bbox
        w_face = max(1e-3, x2 - x1)
        h_face = max(1e-3, y2 - y1)
        try:
            lx, ly = kps[0]  # 左眼角
            rx, ry = kps[1]  # 右眼角
            nx, ny = kps[2]  # 鼻尖
            mlx, mly = kps[3]  # 左嘴角
            mrx, mry = kps[4]  # 右嘴角
        except Exception:  # noqa: BLE001
            return False, "关键点不可读"

        # 1. 眼睛间距 vs 脸宽：正常 0.4~0.7，口罩不影响眼睛可以跳过
        eye_dist = math.hypot(rx - lx, ry - ly)
        eye_ratio = eye_dist / w_face
        if eye_ratio < 0.2 or eye_ratio > 1.1:
            return False, f"眼距异常(ratio={eye_ratio:.2f})"

        # 2. 嘴/鼻尖在脸框底部以上（口罩：嘴角缺失/靠上/靠近鼻尖）
        mouth_y_avg = (mly + mry) / 2
        nose_to_mouth = mouth_y_avg - ny
        eye_y_avg = (ly + ry) / 2
        eye_to_nose = ny - eye_y_avg
        if eye_to_nose <= 0:
            return False, "眼鼻Y反转"
        ratio_nm_en = nose_to_mouth / max(0.1, eye_to_nose)
        # 正常 ratio ~= 1；戴口罩时嘴角被遮挡，比值会很小 (<0.5)
        if ratio_nm_en < 0.3:
            return False, f"疑似口罩遮挡(nose-mouth/eye-nose={ratio_nm_en:.2f})"

        # 3. 嘴角超出脸框底（检测错位）
        if mouth_y_avg > y2 + 2:
            return False, "嘴角超出脸框"

        return True, "OK"

    # ----- 检测 + 过滤 -----
    def extract_valid_faces(self, img_bgr: np.ndarray) -> List[FaceFeature]:
        """对一帧图片提取【满足过滤条件】的人脸特征（含口罩兼容逻辑）。"""
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
            if settings.face_gender_keep == "female":
                if gender != settings.face_gender_female_value:
                    continue
            else:
                if gender == settings.face_gender_female_value:
                    continue

            kps = np.asarray(face.kps, dtype=np.float32) if face.kps is not None else None
            if kps is None or kps.shape != (5, 2):
                continue

            emb = face.normed_embedding if hasattr(face, "normed_embedding") else None
            emb = np.asarray(emb, dtype=np.float32) if emb is not None else None
            if emb is None or emb.size == 0:
                continue

            x1, y1, x2, y2 = [int(v) for v in face.bbox]
            h, w = img_bgr.shape[:2]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            crop = img_bgr[y1:y2, x1:x2]
            blur = self._blur_score(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)) if crop.size else 0.0

            # ===== 关键点质量评估：是否疑似口罩 =====
            kp_ok, kp_reason = self._keypoint_quality(kps, (x1, y1, x2, y2))
            use_mask_tolerant = settings.face_mask_tolerant and not kp_ok

            if use_mask_tolerant:
                # 口罩兼容：姿态和模糊阈值放宽
                yaw, roll = self._yaw_roll(kps)
                yaw_thr = settings.face_yaw_mask_threshold
                blur_thr = settings.face_blur_mask_threshold
                if abs(yaw) > yaw_thr or abs(roll) > yaw_thr:
                    continue
                if blur < blur_thr:
                    continue
                results.append(FaceFeature(
                    embedding=emb, det_score=det, gender=gender,
                    yaw_deg=yaw, blur_score=blur, bbox=(x1, y1, x2, y2),
                    mask_tolerant_applied=True,
                ))
            else:
                # 正常：严格阈值
                yaw, roll = self._yaw_roll(kps)
                if abs(yaw) > settings.face_yaw_threshold or abs(roll) > settings.face_yaw_threshold:
                    continue
                if blur < settings.face_blur_threshold:
                    continue
                results.append(FaceFeature(
                    embedding=emb, det_score=det, gender=gender,
                    yaw_deg=yaw, blur_score=blur, bbox=(x1, y1, x2, y2),
                    mask_tolerant_applied=False,
                ))

        return results

    def free_face_objects(self, faces: List[FaceFeature]) -> None:
        """显式释放单视频人脸特征对象。"""
        for f in faces:
            f.embedding = np.empty(0, dtype=np.float32)
        faces.clear()
