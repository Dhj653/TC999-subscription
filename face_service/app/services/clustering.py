"""
基于 FAISS 的人脸特征增量聚类 + 角色库先验匹配。
新增：聚类产生的分组满足 ">= folder_create_min_videos 个视频" 时，
     自动将代表特征写入 face_character 角色库（若同名/同特征不重复写）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..utils.logger import get_logger

log = get_logger()


@dataclass
class Cluster:
    cluster_id: int
    video_indices: List[int]         # 原视频列表的下标集合
    representative: np.ndarray       # (512,) 代表特征（归一化平均）
    video_count: int = 0


class VideoFaceBag:
    """单视频提取的人脸特征袋：聚合后取平均作为视频代表特征。"""

    def __init__(self, video_idx: int, video_path: str) -> None:
        self.video_idx = video_idx
        self.video_path = video_path
        self.embeddings: List[np.ndarray] = []
        self.face_count = 0
        self.mask_count = 0  # 多少个人脸命中了口罩兼容模式

    def add(self, emb: np.ndarray, mask_tolerant: bool = False) -> None:
        self.embeddings.append(emb.astype(np.float32, copy=False))
        self.face_count += 1
        if mask_tolerant:
            self.mask_count += 1

    def representative(self) -> Optional[np.ndarray]:
        if not self.embeddings:
            return None
        stacked = np.vstack(self.embeddings)
        mean = stacked.mean(axis=0)
        norm = np.linalg.norm(mean)
        if norm < 1e-6:
            return None
        return mean / norm


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def incremental_cluster(
    bags: List[VideoFaceBag],
    similarity_threshold: float,
) -> List[Cluster]:
    """
    增量聚类：依次处理每个视频的代表特征，
    与所有已存在聚类的代表特征比较，最高相似度 >= 阈值则归到最大那个，
    否则新建聚类。最后更新代表特征为该聚类的所有视频平均。
    """
    clusters: List[Cluster] = []
    next_id = 0

    for bag in bags:
        rep = bag.representative()
        if rep is None:
            continue
        best_score = -1.0
        best_idx = -1
        for i, c in enumerate(clusters):
            s = _cosine(rep, c.representative)
            if s > best_score:
                best_score = s
                best_idx = i
        if best_idx >= 0 and best_score >= similarity_threshold:
            clusters[best_idx].video_indices.append(bag.video_idx)
            clusters[best_idx].video_count += 1
            # 更新代表特征：增量平均
            old_cnt = len(clusters[best_idx].video_indices) - 1
            clusters[best_idx].representative = (
                (clusters[best_idx].representative * old_cnt + rep) / (old_cnt + 1)
            )
            norm = np.linalg.norm(clusters[best_idx].representative)
            if norm > 1e-6:
                clusters[best_idx].representative /= norm
        else:
            clusters.append(Cluster(
                cluster_id=next_id,
                video_indices=[bag.video_idx],
                representative=rep.copy(),
                video_count=1,
            ))
            next_id += 1

    log.info("聚类完成: %d 个有效视频 → %d 个分组 (阈值=%.2f)",
             len([b for b in bags if b.representative() is not None]),
             len(clusters), similarity_threshold)
    return clusters


def match_to_character_library(
    bag_rep: np.ndarray,
    character_features: List[Tuple[int, np.ndarray]],
    similarity_threshold: float,
) -> Optional[int]:
    """
    【新增】把视频代表特征和角色库特征一一比较，返回命中的 character_id 或 None。
    character_features: [(character_id, 512维归一化特征), ...]
    """
    best_id = None
    best_score = -1.0
    for cid, feat in character_features:
        s = _cosine(bag_rep, feat)
        if s > best_score:
            best_score = s
            best_id = cid
    if best_id is not None and best_score >= similarity_threshold:
        return best_id
    return None
