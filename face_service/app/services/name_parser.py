"""
视频文件名人名解析。
- 从文件名中提取疑似人名（中文 2-4 字姓名、常见姓氏开头）。
- 支持分隔符：空格、下划线、横杠、方括号、顿号等。
- 不依赖外部 AI，纯规则匹配，零成本。
"""
from __future__ import annotations

import re
from typing import List

# 常见中文姓氏（覆盖率高，足以做粗筛）
SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟黄"
    "穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌"
    "霍虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉"
    "钮龚程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌"
    "焦巴弓牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘"
    "景詹束龙叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴"
    "胥能苍双闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边扈燕"
    "冀浦尚农温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡"
)

# 人名模式：2-4 个连续中文，且首字为姓氏
_CN_NAME_RE = re.compile(r"([\u4e00-\u9fa5]{2,4})")

# 常见标点分隔
_SPLIT_RE = re.compile(r"[\s_\-–—\[\]【】()（）,，、;；:：.]+")

# 噪声词（避免把 "少女" "美女" "合集" 等当人名）
NOISE_WORDS = {
    "少女", "美女", "合集", "高清", "无码", "有码", "全集", "番号", "字幕",
    "中文", "日文", "中字", "无字", "未删减", "完整", "片段", "片段集",
    "自拍", "偷拍", "现场", "直播", "录像", "花絮", "预告", "正片",
    "国模", "私拍", "流出", " leaked", "4K", "1080P", "720P",
}


def _looks_like_name(token: str) -> bool:
    if len(token) < 2 or len(token) > 4:
        return False
    if token[0] not in SURNAMES:
        return False
    if token in NOISE_WORDS:
        return False
    # 全中文
    for ch in token:
        if ch < "\u4e00" or ch > "\u9fa5":
            return False
    return True


def extract_names(filename: str) -> List[str]:
    """从文件名中提取人名候选，去重保序返回。"""
    if not filename:
        return []
    stem = filename
    # 去扩展名
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]

    candidates: List[str] = []
    seen = set()

    # 先按分隔符切分
    for part in _SPLIT_RE.split(stem):
        if not part:
            continue
        # 在每段中找连续中文块
        for m in _CN_NAME_RE.finditer(part):
            chunk = m.group(1)
            # 尝试从 chunk 起始截取 2~4 字
            for length in (4, 3, 2):
                if len(chunk) >= length:
                    cand = chunk[:length]
                    if _looks_like_name(cand) and cand not in seen:
                        seen.add(cand)
                        candidates.append(cand)
                        break

    return candidates
