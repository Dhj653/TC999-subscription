"""
基于 FAISS 的人脸增量聚类。
- 聚类质心在任务生命周期内常驻（跨视频保留，使同一演员跨视频归一组）。
- 单视频处理完成后，仅清空"该视频的人脸特征/帧缓存"，质心保留。
- 人名冲突判定：同一聚类分组解析出多个不同人名 → 标记 name_conflict，禁止自动改名。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from ..config import settings
from ..database import (
    add_extracted_name,
    create_group,
    get_group,
    get_repr_embedding,
    list_groups,
    set_repr_embedding,
    update_group,
)
from ..utils.logger import get_logger

log = get_logger()


def _emb_to_blob(emb: np.ndarray) -> bytes:
    return np.ascontiguousarray(emb.astype(np.float32)).tobytes()


def _blob_to_emb(blob: Optional[bytes]) -> Optional[np.ndarray]:
    if not blob:
        return None
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.size != 512:
        return None
    n = np.linalg.norm(arr)
    return (arr / n).astype(np.float32) if n > 1e-6 else None


@dataclass
class Cluster:
    group_id: int
    group_name: str            # 当前显示名（可能=演员名）
    original_name: str          # 人物{n}
    status: str                 # auto_numbered / renamed / name_conflict / multi_person
    centroid: np.ndarray        # (512,) float32 归一化
    member_count: int = 0
    extracted_names: List[str] = field(default_factory=list)


class ClusterStore:
    """任务级聚类存储。一个扫描任务一个实例。"""

    # 专门存放"多人视频"的特殊分组名
    MULTI_PERSON_GROUP_NAME = "多人"

    def __init__(self, task_id: int) -> None:
        self.task_id = task_id
        self._clusters: List[Cluster] = []
        self._index = None  # faiss.IndexFlatIP
        self._lock = threading.Lock()
        self._multi_group_id: Optional[int] = None
        self._auto_counter = 0
        self._dirty: set[int] = set()
        self._init_index()

    def _init_index(self) -> None:
        try:
            import faiss
        except ImportError as e:
            raise RuntimeError("未安装 faiss-cpu，请 pip install -r requirements.txt") from e
        self._index = faiss.IndexFlatIP(512)

    @classmethod
    def load_existing(cls, task_id: int) -> "ClusterStore":
        """从数据库重建任务级聚类存储（供 reprocess_single 匹配既有分组）。"""
        store = cls(task_id)
        for g in list_groups(task_id):
            if g["status"] in ("multi_person", "deleted", "merged"):
                if g["status"] == "multi_person" and store._multi_group_id is None:
                    store._multi_group_id = g["group_id"]
                continue
            emb = _blob_to_emb(get_repr_embedding(g["group_id"]))
            if emb is None:
                continue
            cluster = Cluster(
                group_id=g["group_id"],
                group_name=g["group_name"],
                original_name=g["original_group_name"],
                status=g["status"],
                centroid=emb,
                member_count=g["video_count"],
            )
            store._clusters.append(cluster)
            # 自增计数器不低于既有编号
            try:
                num = int(g["original_group_name"].replace("人物", ""))
                if num > store._auto_counter:
                    store._auto_counter = num
            except ValueError:
                pass
        store._rebuild_index()
        return store

    # ---------- 内部 ----------
    def _add_cluster(self, centroid: np.ndarray) -> Cluster:
        with self._lock:
            self._auto_counter += 1
            name = f"人物{self._auto_counter}"
            gid = create_group(self.task_id, name, status="auto_numbered")
            cluster = Cluster(
                group_id=gid,
                group_name=name,
                original_name=name,
                status="auto_numbered",
                centroid=centroid,
                member_count=1,
            )
            self._clusters.append(cluster)
            # FAISS 添加质心（需 C-contiguous float32）
            vec = np.ascontiguousarray(centroid.reshape(1, -1).astype(np.float32))
            self._index.add(vec)
            set_repr_embedding(gid, _emb_to_blob(centroid))
            return cluster

    def _update_centroid(self, cluster: Cluster, emb: np.ndarray) -> None:
        with self._lock:
            c = cluster.centroid
            n = cluster.member_count
            new_c = (c * n + emb) / (n + 1)
            norm = np.linalg.norm(new_c)
            if norm > 1e-6:
                new_c = new_c / norm
            cluster.centroid = new_c.astype(np.float32)
            cluster.member_count = n + 1
            self._dirty.add(cluster.group_id)
            # 同步 FAISS：重建索引（质心更新后整体替换）
            self._rebuild_index()

    def flush_dirty(self) -> None:
        """把变更过的质心写回数据库。"""
        if not self._dirty:
            return
        for gid in list(self._dirty):
            cluster = self.cluster_by_id(gid)
            if cluster is not None:
                set_repr_embedding(gid, _emb_to_blob(cluster.centroid))
        self._dirty.clear()

    def _rebuild_index(self) -> None:
        import faiss
        self._index = faiss.IndexFlatIP(512)
        if self._clusters:
            mat = np.stack([c.centroid for c in self._clusters]).astype(np.float32)
            self._index.add(np.ascontiguousarray(mat))

    # ---------- 公开 ----------
    def get_or_create(self, emb: np.ndarray) -> Optional[Cluster]:
        """把一个人脸特征归入最相近的聚类；不满足阈值则新建。返回所属 Cluster。"""
        emb = emb.astype(np.float32)
        with self._lock:
            n = self._index.ntotal
        if n == 0:
            return self._add_cluster(emb)
        q = np.ascontiguousarray(emb.reshape(1, -1).astype(np.float32))
        sim, idx = self._index.search(q, 1)
        sim = float(sim[0][0])
        best = int(idx[0][0])
        if sim >= settings.face_similarity_threshold and 0 <= best < len(self._clusters):
            cluster = self._clusters[best]
            self._update_centroid(cluster, emb)
            return cluster
        return self._add_cluster(emb)

    def ensure_multi_person_group(self) -> int:
        """确保存在"多人"分组，返回其 group_id。"""
        if self._multi_group_id is not None:
            return self._multi_group_id
        gid = create_group(self.task_id, self.MULTI_PERSON_GROUP_NAME, status="multi_person")
        self._multi_group_id = gid
        return gid

    def apply_extracted_name(self, cluster: Cluster, name: Optional[str]) -> None:
        """对某聚类应用一个从视频文件名解析出的人名，按规则更新分组名/状态。"""
        if not name:
            return
        names = add_extracted_name(cluster.group_id, name)
        cluster.extracted_names = list(names)
        db_group = get_group(cluster.group_id)
        cur_status = db_group["status"] if db_group else cluster.status

        distinct = set(n for n in names if n)
        if len(distinct) >= 2:
            # 多个不同人名 → 冲突，禁止自动改名，恢复原始编号名
            cluster.status = "name_conflict"
            cluster.group_name = cluster.original_name
            update_group(
                cluster.group_id,
                status="name_conflict",
                group_name=cluster.original_name,
            )
            log.info(
                "分组 %s 出现人名冲突 %s，保持原名 %s，交人工确认",
                cluster.group_id, list(distinct), cluster.original_name,
            )
        elif len(distinct) == 1:
            only = next(iter(distinct))
            if cur_status == "name_conflict":
                # 冲突未解除前不自动改名
                return
            if cluster.group_name != only:
                cluster.group_name = only
                cluster.status = "renamed"
                update_group(cluster.group_id, group_name=only, status="renamed")
                log.info("分组 %s 自动重命名为: %s", cluster.group_id, only)

    def all_clusters(self) -> List[Cluster]:
        return list(self._clusters)

    def cluster_by_id(self, group_id: int) -> Optional[Cluster]:
        for c in self._clusters:
            if c.group_id == group_id:
                return c
        return None

    def free_video_cache(self, video_faces: List) -> None:
        """清空单视频人脸特征缓存（质心保留）。"""
        for f in video_faces:
            try:
                f.embedding = np.empty(0, dtype=np.float32)
            except Exception:  # noqa: BLE001
                pass
        video_faces.clear()
