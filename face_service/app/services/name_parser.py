"""
视频文件名 → 人名解析：基于规则提取候选人名。
新增：NOISE_WORDS 包含 "双人/单人/多人/作品/个人" 等噪声词。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

# 噪声词（避免把 "双人" "美女" "合集" 等当人名）
NOISE_WORDS = {
    "少女", "美女", "合集", "高清", "无码", "有码", "全集", "番号", "字幕",
    "中文", "日文", "中字", "无字", "未删减", "完整", "片段", "片段集",
    "自拍", "偷拍", "现场", "直播", "录像", "花絮", "预告", "正片",
    "国模", "私拍", "流出", "4K", "1080P", "720P",
    # 人数描述词（不应被识别为人名）
    "单人", "双人", "三人", "多人", "单人秀", "双人秀", "三人行",
    # 其他常见噪声
    "作品", "个人", "专辑", "精选", "特辑", "续集", "番外", "未分类", "待补充",
}

# 常见分隔符（用于切分）
_SEPS = r"[_\-\.\s\[\]【】()（）]{2,}|[_\-\.\s]"


def _clean(word: str) -> str:
    w = word.strip()
    # 去除前后的数字编号如 "01_" "02-" 等
    w = re.sub(r"^\d{1,4}", "", w).strip()
    return w


def _looks_like_name(token: str) -> bool:
    """简单人名判断：2~4个汉字 / 英文 2~20 字母。"""
    if not token:
        return False
    if token in NOISE_WORDS:
        return False
    # 全中文 2~4 字
    if re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", token):
        return True
    # 英文姓名（2~20字母，允许首字母大写）
    if re.fullmatch(r"[A-Za-z][A-Za-z ]{1,19}", token):
        return True
    return False


def extract_names(filename_or_path: str) -> List[str]:
    """从视频文件名中解析出人名候选（去重保持顺序）。"""
    stem = Path(filename_or_path).stem
    seen: set[str] = set()
    result: List[str] = []

    # 1. 方括号/中文括号内的名称： [XXX] 【XXX】 (XXX) （XXX）
    for m in re.finditer(r"[\[【(（]([^]\]）)]{2,32})[\]】)）]", stem):
        tok = m.group(1).strip()
        if _looks_like_name(tok) and tok not in seen:
            seen.add(tok)
            result.append(tok)

    # 2. 按分隔符切分
    tokens = re.split(_SEPS, stem)
    for t in tokens:
        tok = _clean(t)
        if _looks_like_name(tok) and tok not in seen:
            seen.add(tok)
            result.append(tok)

    # 3. 兜底：2~4 字中文块
    for m in re.finditer(r"[\u4e00-\u9fa5]{2,4}", stem):
        tok = m.group(0)
        if _looks_like_name(tok) and tok not in seen:
            seen.add(tok)
            result.append(tok)

    return result
