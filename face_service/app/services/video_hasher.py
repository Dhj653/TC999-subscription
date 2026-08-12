"""
视频内容级去重：基于 dHash (difference hash, 64bit 感知哈希) 的视频指纹。
- 对每个视频均匀抽取 N 个关键帧（默认 5 帧）
- 每帧计算 dHash（9x8 灰度 → 相邻差分 → 64bit → 16 字符 hex）
- 判定重复：时长差 ≤ tol、分辨率相同、≥M 个关键帧 dHash 完全一致
- 支持从磁盘文件 / 内存字节流 / 压缩包内字节（read_entry_bytes）计算指纹
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from ..config import settings
from ..utils.logger import get_logger

log = get_logger()


@dataclass
class VideoFingerprint:
    video_path: str
    duration_sec: Optional[float]
    width: Optional[int]
    height: Optional[int]
    file_size: int
    hashes: List[str]          # len = N，每元素 16 位 hex
    raw_bytes_used: bool = False  # True 表示不是从磁盘路径而是从内存字节抽帧


# ==============================================================
# dHash 实现
# ==============================================================
def dhash(frame_bgr: np.ndarray) -> str:
    """
    计算一帧 BGR 图像的 dHash，返回 16 位 hex 字符串（64bit）。
    经典实现：缩放到 9x8 → 灰度 → 每行相邻差分 → 64 bit。
    """
    try:
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    except Exception:  # noqa: BLE001
        if len(frame_bgr.shape) == 2:
            gray = frame_bgr
        else:
            return "0" * 16
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    # 每行 9 像素 → 8 个差分；8 行 × 8 = 64
    bits: List[int] = []
    for row in range(8):
        for col in range(8):
            bits.append(1 if small[row, col] > small[row, col + 1] else 0)
    # 8 bit -> 1 hex，64 bit -> 16 hex
    hex_chars = []
    for i in range(0, 64, 4):
        nib = (bits[i] << 3) | (bits[i + 1] << 2) | (bits[i + 2] << 1) | bits[i + 3]
        hex_chars.append(f"{nib:x}")
    return "".join(hex_chars)


# ==============================================================
# ffprobe 探测 时长 + 分辨率
# ==============================================================
def probe_video_info(video_path: Optional[str] = None,
                     video_bytes: Optional[bytes] = None) -> Tuple[Optional[float], Optional[int], Optional[int]]:
    """返回 (duration_sec, width, height)。失败返回 (None, None, None)。"""
    import tempfile

    tmp_path: Optional[str] = None
    try:
        if video_path:
            src = video_path
        elif video_bytes:
            fd, tmp_path = tempfile.mkstemp(suffix=".mp4", prefix="facehash_")
            with os.fdopen(fd, "wb") as f:
                f.write(video_bytes)
            src = tmp_path
        else:
            return None, None, None

        # 分辨率
        w = h = None
        try:
            pr = subprocess.run(
                [settings.ffprobe_bin, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", src],
                capture_output=True, text=True, timeout=30,
            )
            if pr.returncode == 0 and "x" in pr.stdout.strip():
                ws, hs = pr.stdout.strip().split("x")[:2]
                w, h = int(ws), int(hs)
        except Exception:  # noqa: BLE001
            pass

        # 时长
        duration: Optional[float] = None
        try:
            pr = subprocess.run(
                [settings.ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", src],
                capture_output=True, text=True, timeout=30,
            )
            if pr.returncode == 0:
                s = pr.stdout.strip()
                if s:
                    duration = float(s)
        except Exception:  # noqa: BLE001
            pass
        return duration, w, h
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ==============================================================
# 抽关键帧
# ==============================================================
def _extract_uniform_keyframes(
    *,
    video_path: Optional[str] = None,
    video_bytes: Optional[bytes] = None,
    keyframes: int,
) -> List[np.ndarray]:
    """
    在视频整个时间范围内均匀抽 keyframes 帧（从 0.1*duration 到 0.9*duration，避免片头片尾黑屏）。
    返回 BGR uint8 数组列表（长度 ≤ keyframes，失败可能更短）。
    """
    import subprocess
    import tempfile

    keyframes = max(1, int(keyframes))
    tmp_file = None
    src_arg: Optional[str] = None
    try:
        if video_path:
            src_arg = video_path
        elif video_bytes:
            fd, tmp_file = tempfile.mkstemp(suffix=".mp4", prefix="facehash_")
            with os.fdopen(fd, "wb") as f:
                f.write(video_bytes)
            src_arg = tmp_file
        else:
            return []

        # 先探测时长
        dur = None
        try:
            pr = subprocess.run(
                [settings.ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", src_arg],
                capture_output=True, text=True, timeout=30,
            )
            if pr.returncode == 0 and pr.stdout.strip():
                dur = float(pr.stdout.strip())
        except Exception:  # noqa: BLE001
            pass

        # 解析分辨率
        width = height = None
        try:
            pr = subprocess.run(
                [settings.ffprobe_bin, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", src_arg],
                capture_output=True, text=True, timeout=30,
            )
            if pr.returncode == 0 and "x" in pr.stdout.strip():
                ws, hs = pr.stdout.strip().split("x")[:2]
                width, height = int(ws), int(hs)
        except Exception:  # noqa: BLE001
            pass
        if not width or not height:
            return []

        # 采样时间点：均匀分布在 10%~90% 区间（避免片头片尾黑）
        if dur and dur > 0.5:
            t0 = dur * 0.1
            t1 = dur * 0.9
            if keyframes == 1:
                ts = [(t0 + t1) * 0.5]
            else:
                step = (t1 - t0) / (keyframes - 1)
                ts = [t0 + step * i for i in range(keyframes)]
        else:
            # 时长未知 → 按固定 fps 抽 1/4 秒间隔（短时长兜底）
            ts = [0.25 * i for i in range(1, keyframes + 1)]

        frames: List[np.ndarray] = []
        for t in ts:
            if t < 0:
                t = 0
            # ffmpeg: -ss 定位后读 1 帧
            cmd = [
                settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error",
                "-ss", f"{t:.3f}", "-i", src_arg,
                "-frames:v", "1",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=20)
                if proc.returncode != 0:
                    continue
                raw = proc.stdout
                need = width * height * 3
                if len(raw) < need:
                    continue
                frame = np.frombuffer(raw[:need], dtype=np.uint8).reshape((height, width, 3))
                frames.append(frame.copy())
            except Exception:  # noqa: BLE001
                continue
        return frames
    finally:
        if tmp_file and os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except OSError:
                pass


# ==============================================================
# 高层：计算视频指纹
# ==============================================================
def compute_fingerprint(
    *,
    video_path: Optional[str] = None,
    video_bytes: Optional[bytes] = None,
    keyframes: Optional[int] = None,
) -> VideoFingerprint:
    """
    计算单个视频的指纹（二选一入参）。
    - 若传 bytes，则 video_path 用于记录元信息（路径名），内部用 bytes 抽帧
    """
    if keyframes is None:
        keyframes = settings.dedup_keyframes
    keyframes = max(1, int(keyframes))

    file_size = 0
    if video_path and os.path.exists(video_path):
        try:
            file_size = os.path.getsize(video_path)
        except OSError:
            file_size = 0
    elif video_bytes is not None:
        file_size = len(video_bytes)

    duration, width, height = probe_video_info(video_path=video_path, video_bytes=video_bytes)

    frames = _extract_uniform_keyframes(
        video_path=video_path, video_bytes=video_bytes, keyframes=keyframes,
    )
    hashes: List[str] = []
    for frm in frames:
        hashes.append(dhash(frm))

    # 不足 keyframes 时用最后一个哈希重复补齐（方便按位严格比较）
    while len(hashes) < keyframes:
        hashes.append(hashes[-1] if hashes else "0" * 16)
    hashes = hashes[:keyframes]

    raw_bytes_used = video_bytes is not None and (video_path is None or not os.path.exists(video_path))
    return VideoFingerprint(
        video_path=video_path or "<bytes>",
        duration_sec=duration,
        width=width,
        height=height,
        file_size=file_size,
        hashes=hashes,
        raw_bytes_used=raw_bytes_used,
    )


# ==============================================================
# 指纹对比：判定是否重复
# ==============================================================
def is_duplicate(
    a: VideoFingerprint,
    b_hashes: List[str],
    b_duration: Optional[float],
    b_width: Optional[int],
    b_height: Optional[int],
    *,
    duration_tolerance: Optional[float] = None,
    min_hash_matches: Optional[int] = None,
) -> bool:
    """基于两个指纹签名判定内容是否相同（默认完全一致才通过）。"""
    if duration_tolerance is None:
        duration_tolerance = settings.dedup_duration_tolerance_sec
    if min_hash_matches is None:
        min_hash_matches = settings.dedup_min_hash_matches

    # 1. 分辨率一致（若两边都有）
    if a.width and a.height and b_width and b_height:
        if (a.width, a.height) != (b_width, b_height):
            return False

    # 2. 时长差 ≤ tol（两边都有时才比较）
    if a.duration_sec is not None and b_duration is not None:
        if abs(a.duration_sec - b_duration) > duration_tolerance:
            return False

    # 3. 关键帧 dHash 比对：至少 min_hash_matches 个完全相同
    n = min(len(a.hashes), len(b_hashes))
    matches = 0
    for i in range(n):
        if a.hashes[i] == b_hashes[i]:
            matches += 1
    if matches < min_hash_matches:
        return False
    return True


# ==============================================================
# 在 DB 候选中查找匹配的主视频
# ==============================================================
def find_matching_master(
    fp: VideoFingerprint,
    candidates: List[dict],
) -> Optional[int]:
    """
    传入 `find_duplicate_candidates` 得出的候选列表 + 当前指纹。
    逐个比对 dHash，返回命中的 fingerprint_id（第一个匹配的）或 None。
    """
    for c in candidates:
        c_hashes = c.get("hashes") or []
        if is_duplicate(
            fp,
            b_hashes=c_hashes,
            b_duration=c.get("duration_sec"),
            b_width=c.get("width"),
            b_height=c.get("height"),
        ):
            return int(c["fingerprint_id"])
    return None
