"""日志工具。"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_inited = False


def get_logger(name: str = "face_service") -> logging.Logger:
    global _inited
    logger = logging.getLogger(name)
    if _inited:
        return logger
    _inited = True

    from ..config import settings

    logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 文件（带轮转）
    try:
        log_path = Path(settings.log_file)
        if not log_path.is_absolute():
            log_path = Path(__file__).resolve().parent.parent / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            str(log_path), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:  # noqa: BLE001
        # 日志路径写不进去就忽略
        pass

    return logger
