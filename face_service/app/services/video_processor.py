"""
视频抽帧：ffmpeg 按时间间隔抽取关键帧，返回 numpy BGR 数组。
- 支持磁盘文件路径
- 支持内存字节流（压缩包内视频）
- 分辨率探测失败时跳过，避免异常
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator, Optional

import numpy as np

from ..config import settings
from ..utils.logger import get_logger

log = get_logger()


def _probe_duration_seconds(path_or_bytes: Optional[str]) -> Optional[float]:
    try:
        cmd = [settings.ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1"]
        if path_or_bytes:
            cmd.append(path_or_bytes)
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        else:
            return None
        if out.returncode == 0:
            return float(out.stdout.strip() or 0)
    except Exception:  # noqa: BLE001
        pass
    return None


def extract_frames(
    video_path: Optional[str] = None,
    video_bytes: Optional[bytes] = None,
) -> Iterator[np.ndarray]:
    """抽取视频关键帧（生成器，逐帧产出 BGR uint8 数组）。二选一入参。"""
    interval = max(0.5, settings.video_frame_interval)
    max_frames = max(1, settings.video_max_frames)

    tmp_file: Optional[str] = None
    src_arg: str
    if video_path:
        src_arg = video_path
    elif video_bytes:
        suffix = ".mp4"
        fd, tmp_file = tempfile.mkstemp(suffix=suffix, prefix="facevid_")
        with os.fdopen(fd, "wb") as f:
            f.write(video_bytes)
        src_arg = tmp_file
    else:
        return

    cmd = [
        settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error",
        "-i", src_arg,
        "-vf", f"fps=1/{interval}",
        "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        width = height = None
        try:
            pr = subprocess.run(
                [settings.ffprobe_bin, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "csv=p=0:s=x", src_arg],
                capture_output=True, text=True, timeout=30,
            )
            if pr.returncode == 0 and "x" in pr.stdout.strip():
                w, h = pr.stdout.strip().split("x")[:2]
                width, height = int(w), int(h)
        except Exception:  # noqa: BLE001
            pass
        if not width or not height:
            log.warning("无法解析分辨率，跳过抽帧: %s", src_arg)
            proc.kill()
            return

        frame_size = width * height * 3
        buf = bytearray()
        count = 0
        assert proc.stdout is not None
        while count < max_frames:
            need = frame_size - len(buf)
            data = proc.stdout.read(need)
            if not data:
                break
            buf.extend(data)
            if len(buf) >= frame_size:
                frame = np.frombuffer(bytes(buf[:frame_size]), dtype=np.uint8)
                buf = buf[frame_size:]
                yield frame.reshape((height, width, 3))
                count += 1

        proc.stdout.close()
        rc = proc.wait(timeout=30)
        if rc != 0 and count == 0:
            err = proc.stderr.read().decode("utf-8", "ignore") if proc.stderr else ""
            log.warning("ffmpeg 抽帧失败: %s err=%s", src_arg, err[:200])
    finally:
        if tmp_file and os.path.exists(tmp_file):
            try:
                os.remove(tmp_file)
            except OSError:
                pass
