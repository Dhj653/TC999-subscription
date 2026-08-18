"""
视频内容级去重：基于 dHash (difference hash, 64bit 感知哈希) 的视频指纹。
- 对每个视频均匀抽取 N 个关键帧（默认 5 帧）
- 每帧计算 dHash（9x8 灰度 → 相邻差分 → 64bit → 16 字符 hex）
- 判定重复：时长差 ≤ tol、分辨率相同、≥M 个关键帧 dHash 完全一致
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
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
    hashes: List[str]
    raw_bytes_used: bool = False


def dhash(frame_bgr: np.ndarray) -> str:
    """9x8 灰度 → 相邻差分 → 64bit → 16 hex 字符串。"""
    try:
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    except Exception:  # noqa: BLE001
        if len(frame_bgr.shape) == 2:
            gray = frame_bgr
        else:
            return "0" * 16
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits: List[int] = []
    for row in range(8):
        for col in range(8):
            bits.append(1 if small[row, col] > small[row, col + 1] else 0)
    hex_chars = []
    for i in range(0, 64, 4):
        nib = (bits[i] << 3) | (bits[i + 1] << 2) | (bits[i + 2] << 1) | bits[i + 3]
        hex_chars.append(f"{nib:x}")
    return "".join(hex_chars)


def probe_video_info(
    video_path: Optional[str] = None,
    video_bytes: Optional[bytes] = None,
) -> Tuple[Optional[float], Optional[int], Optional[int]]:
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

        duration: Optional[float] = None
        try:
            pr = subprocess.run(
                [settings.ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", src],
                capture_output=True, text=True, timeout=30,
            )
            if pr.returncode == 0 and pr.stdout.strip():
                duration = float(pr.stdout.strip())
        except Exception:  # noqa: BLE001
            pass
        return duration, w, h
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _extract_uniform_keyframes(
    *,
    video_path: Optional[str] = None,
    video_bytes: Optional[bytes] = None,
    keyframes: int,
) -> List[np.ndarray]:
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

        if dur and dur > 0.5:
            t0 = dur * 0.1
            t1 = dur * 0.9
            if keyframes == 1:
                ts = [(t0 + t1) * 0.5]
            else:
                step = (t1 - t0) / (keyframes - 1)
                ts = [t0 + step * i for i in range(keyframes)]
        else:
            ts = [0.25 * i for i in range(1, keyframes + 1)]

        frames: List[np.ndarray] = []
        for t in ts:
            t = max(0.0, float(t))
            cmd = [
                settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error",
                "-ss", f"{t:.3f}", "-i", src_arg,
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
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


def compute_fingerprint(
    *,
    video_path: Optional[str] = None,
    video_bytes: Optional[bytes] = None,
    keyframes: Optional[int] = None,
) -> VideoFingerprint:
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
    hashes: List[str] = [dhash(frm) for frm in frames]
    while len(hashes) < keyframes:
        hashes.append(hashes[-1] if hashes else "0" * 16)
    hashes = hashes[:keyframes]

    raw_bytes_used = bool(video_bytes is not None and (video_path is None or not os.path.exists(video_path)))
    return VideoFingerprint(
        video_path=video_path or "<bytes>",
        duration_sec=duration, width=width, height=height,
        file_size=file_size, hashes=hashes, raw_bytes_used=raw_bytes_used,
    )


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
    if duration_tolerance is None:
        duration_tolerance = settings.dedup_duration_tolerance_sec
    if min_hash_matches is None:
        min_hash_matches = settings.dedup_min_hash_matches

    if a.width and a.height and b_width and b_height:
        if (a.width, a.height) != (b_width, b_height):
            return False
    if a.duration_sec is not None and b_duration is not None:
        if abs(a.duration_sec - b_duration) > duration_tolerance:
            return False
    n = min(len(a.hashes), len(b_hashes))
    matches = 0
    for i in range(n):
        if a.hashes[i] == b_hashes[i]:
            matches += 1
    return matches >= min_hash_matches


def find_matching_master(fp: VideoFingerprint, candidates: List[dict]) -> Optional[int]:
    for c in candidates:
        if is_duplicate(fp,
                        b_hashes=c.get("hashes") or [],
                        b_duration=c.get("duration_sec"),
                        b_width=c.get("width"),
                        b_height=c.get("height")):
            return int(c["fingerprint_id"])
    return None
