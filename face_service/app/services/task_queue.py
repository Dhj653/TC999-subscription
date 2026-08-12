"""
串行任务编排：视频扫描、抽帧、检测、聚类、入库、移动。

核心新增：
  1. 先使用角色库匹配（use_character_library=true 时）
     - 每个视频的代表特征先和已有角色 face_character 做 cosine 匹配
     - 命中则直接归到该角色，不再参与聚类
     - 未命中的视频走聚类，满足 >= folder_create_min_videos 的分组自动入库为新角色
  2. 视频提取到的"代表人脸"（质量最高的那帧）保存缩略图到 thumbnails 目录
     - 关联到对应角色记录（写入 face_character.thumbnail_path）
  3. 断点续跑：按 task_id 从 face_scan_task 恢复进度
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

from .. import database as DB
from ..config import resolve_path, settings
from ..utils.logger import get_logger
from .archive import is_archive, list_entries, read_entry_bytes
from .clustering import VideoFaceBag, incremental_cluster, match_to_character_library
from .face_engine import FaceEngine, FaceFeature
from .file_mover import move_all, rename_character_folder, move_duplicate_to_repeat_dir
from .name_parser import extract_names
from .path_safety import group_output_dir, is_safe_output_path, sanitize_group_name, validate_scan_folder
from .video_hasher import (
    VideoFingerprint, compute_fingerprint, dhash, find_matching_master,
)
from .video_processor import extract_frames

log = get_logger()


# ============================================================
# 全局串行任务状态
# ============================================================
@dataclass
class _Runtime:
    running_task_id: Optional[int] = None
    stop_flag: bool = False
    lock: threading.Lock = threading.Lock()
    queued: List[int] = None  # type: ignore[assignment]


_rt = _Runtime(queued=[])


# ============================================================
# 枚举源目录下的所有视频（含压缩包内视频）
# ============================================================
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts", ".webm"}


def _iter_video_files(folder: str) -> List[str]:
    out: List[str] = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if Path(f).suffix.lower() in VIDEO_EXTS:
                out.append(os.path.join(root, f))
    return sorted(out)


def _collect_all_video_items(folder: str) -> List[Dict[str, Any]]:
    """返回 [{type:'file'|'archive', path, archive?, name?}] 列表。"""
    items: List[Dict[str, Any]] = []
    for root, _dirs, files in os.walk(folder):
        for f in sorted(files):
            p = os.path.join(root, f)
            ext = Path(f).suffix.lower()
            if ext in VIDEO_EXTS:
                items.append({"type": "file", "path": p, "display": p})
            elif ext in {".zip", ".7z"} and is_archive(p):
                try:
                    entries = list_entries(p)
                except Exception as e:  # noqa: BLE001
                    log.warning("解析压缩包条目失败 %s: %s", p, e)
                    continue
                for en in entries:
                    if en.is_video:
                        items.append({
                            "type": "archive",
                            "path": p,
                            "archive": p,
                            "in_archive": en.in_archive_name,
                            "display": f"{p} :: {en.in_archive_name}",
                        })
    return items


# ============================================================
# 人脸缩略图保存（角色代表脸）
# ============================================================
def _save_thumbnail(
    frame_bgr: np.ndarray,
    bbox: tuple,
    character_id: int,
) -> Optional[str]:
    """裁出人脸区域（带一点padding），保存为 thumbnails/ch_{id}.jpg，返回相对路径。"""
    try:
        thumb_dir = Path(resolve_path(settings.thumbnail_dir))
        thumb_dir.mkdir(parents=True, exist_ok=True)
        x1, y1, x2, y2 = bbox
        H, W = frame_bgr.shape[:2]
        pad = int(max(x2 - x1, y2 - y1) * 0.2)
        x1p, y1p = max(0, x1 - pad), max(0, y1 - pad)
        x2p, y2p = min(W, x2 + pad), min(H, y2 + pad)
        crop = frame_bgr[y1p:y2p, x1p:x2p]
        if crop.size == 0:
            return None
        fname = f"ch_{character_id}.jpg"
        fpath = thumb_dir / fname
        cv2.imwrite(str(fpath), crop, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return str(fpath)
    except Exception as e:  # noqa: BLE001
        log.warning("缩略图保存失败: %s", e)
        return None


# ============================================================
# 扫描执行（串行）
# ============================================================
async def _do_scan(task_id: int) -> None:
    engine = FaceEngine.instance()
    engine.init()

    task = DB.get_task(task_id)
    if not task:
        return
    DB.update_task(task_id, status="running")
    DB.append_task_log(task_id, f"开始扫描：{task['scan_folder']}")

    folder = task["scan_folder"]
    output_root = task["output_dir"] or folder
    sim = float(task["similarity"])
    test_mode = bool(task["test_mode"])
    use_char_lib = bool(task.get("use_character_library", 1))

    # 1. 收集视频
    all_items = _collect_all_video_items(folder)
    total = len(all_items)
    DB.update_task(task_id, total_videos=total)
    DB.append_task_log(task_id, f"共发现 {total} 个视频（含压缩包内）")
    if total == 0:
        DB.update_task(task_id, status="completed")
        return

    # 2. 加载角色库（用于先验匹配）
    char_lib: List[tuple] = []  # [(character_id, feature_ndarray, folder_path)]
    if use_char_lib:
        for c in DB.list_characters(include_deleted=False):
            feat = c.get("feature") or []
            if feat and len(feat) >= 128:
                try:
                    arr = np.asarray(feat, dtype=np.float32)
                    arr = arr / (np.linalg.norm(arr) + 1e-9)
                    char_lib.append((c["character_id"], arr, c.get("folder_path")))
                except Exception:  # noqa: BLE001
                    pass
        DB.append_task_log(task_id, f"加载角色库：{len(char_lib)} 个已命名角色")

    # 3. 逐视频处理：抽帧 → 检测 → 取代表特征 + 最清晰帧
    #     【新增】同时保存少量关键帧（均匀取 5 张）用于 dHash 去重，避免二次抽帧
    bags: List[VideoFaceBag] = []
    best_frames: Dict[int, tuple] = {}  # video_idx → (frame, face bbox, embedding)
    matched_characters: Dict[int, int] = {}  # video_idx → character_id（角色库命中的）
    dedup_framebuf: Dict[int, List[np.ndarray]] = {}  # video_idx → 最近 K 张视频帧（均匀保留 N 张用于 dHash）
    dedup_keysamples_for_idx: Dict[int, list] = {}     # video_idx → dHash 十六进制字符串数组（最终）
    dedup_metainfo: Dict[int, tuple] = {}              # video_idx → (duration, width, height, file_size, real_path)

    processed = 0
    for idx, item in enumerate(all_items):
        if _rt.stop_flag:
            DB.append_task_log(task_id, f"停止信号触发，中断处理（已处理 {idx}/{total}）")
            DB.update_task(task_id, status="cancelled", processed_videos=idx)
            _rt.running_task_id = None
            return

        DB.update_task(task_id, current_video=item["display"], processed_videos=idx)
        bag = VideoFaceBag(idx, item["display"])
        best_score_frame = (-1.0, None, None, None)  # (blur*det, frame, bbox, emb)

        # 【去重】准备：为该视频收集均匀分布的 K 帧
        N_KEY = max(1, int(settings.dedup_keyframes))
        frame_buf: List[np.ndarray] = []  # 候选帧（临时保存所有，结束后均匀采样）

        # 文件 size / probe 信息（若为磁盘文件可快速获得）
        file_size = 0
        real_video_path: Optional[str] = item.get("path") if item.get("type") == "file" else None
        if real_video_path and os.path.exists(real_video_path):
            try:
                file_size = os.path.getsize(real_video_path)
            except OSError:
                file_size = 0

        try:
            if item["type"] == "file":
                frame_iter = extract_frames(video_path=item["path"])
            else:
                data = read_entry_bytes(item["archive"], item["in_archive"])
                if not data:
                    DB.append_task_log(task_id, f"  ⚠ 无法读取压缩条目，跳过: {item['display']}")
                    processed += 1
                    continue
                frame_iter = extract_frames(video_bytes=data)

            for frame in frame_iter:
                try:
                    faces: List[FaceFeature] = engine.extract_valid_faces(frame)
                except Exception as e:  # noqa: BLE001
                    log.warning("人脸检测异常: %s", e)
                    continue
                for f in faces:
                    bag.add(f.embedding, f.mask_tolerant_applied)
                    score = f.blur_score * f.det_score
                    if score > best_score_frame[0]:
                        best_score_frame = (score, frame.copy(), f.bbox, f.embedding.copy())
                # 【去重】保留该帧副本
                frame_buf.append(frame.copy())
                del frame
        except Exception as e:  # noqa: BLE001
            DB.append_task_log(task_id, f"  ⚠ 抽帧失败，跳过 {item['display']}: {e}")
            processed += 1
            continue

        # 【去重】从 frame_buf 均匀采样 N_KEY 帧 → 计算 dHash
        if settings.dedup_enabled:
            hashes: List[str] = []
            if frame_buf:
                # 从 [10%..90%] 区间取 N_KEY 张（避免片头片尾黑帧）
                nbuf = len(frame_buf)
                sample_idx: List[int] = []
                if nbuf <= N_KEY:
                    sample_idx = list(range(nbuf))
                else:
                    lo = int(nbuf * 0.1)
                    hi = int(nbuf * 0.9)
                    if hi <= lo:
                        hi = nbuf - 1
                        lo = 0
                    for k in range(N_KEY):
                        ratio = 0 if N_KEY == 1 else k / (N_KEY - 1)
                        pos = int(lo + (hi - lo) * ratio)
                        sample_idx.append(max(0, min(nbuf - 1, pos)))
                for si in sample_idx:
                    hashes.append(dhash(frame_buf[si]))
            # 不足补 0
            while len(hashes) < N_KEY:
                hashes.append(hashes[-1] if hashes else "0" * 16)
            dedup_keysamples_for_idx[idx] = hashes[:N_KEY]
            dedup_metainfo[idx] = (None, None, None, file_size, real_video_path)
        # 释放 frame_buf 内存
        frame_buf.clear()
        del frame_buf

        if bag.face_count == 0:
            DB.append_task_log(task_id, f"  - {os.path.basename(item['display'][:50])}: 无符合条件的女性人脸，跳过")
            processed += 1
            continue

        rep = bag.representative()
        if rep is None:
            processed += 1
            continue

        # 3a. 角色库先验匹配
        cid_match = None
        if use_char_lib and char_lib:
            cid_match = match_to_character_library(rep, [(c[0], c[1]) for c in char_lib], sim)
            if cid_match is not None:
                matched_characters[idx] = cid_match
                names = extract_names(item["display"])
                DB.append_task_log(task_id,
                                   f"  ✓ {os.path.basename(item['display'][:50])}: "
                                   f"命中角色库 #{cid_match} (人脸{bag.face_count}, 口罩兼容{bag.mask_count})")

        # 保存本视频最佳帧，后续入库/建角色用
        if best_score_frame[1] is not None:
            best_frames[idx] = (best_score_frame[1], best_score_frame[2], best_score_frame[3])

        # 收集到的人名（用于分组解析）
        names_here = extract_names(item["display"])
        bag._meta_names = names_here  # type: ignore[attr-defined]
        bags.append(bag)
        processed += 1

    DB.append_task_log(task_id, f"抽帧/检测完成，{len(bags)} 个视频有有效人脸")
    processed = total

    # 4. 对"未命中角色库"的视频做聚类
    unmatched_bags = [b for b in bags if b.video_idx not in matched_characters]
    DB.append_task_log(task_id,
                       f"角色库命中 {len(matched_characters)} 个视频，"
                       f"剩余 {len(unmatched_bags)} 个走聚类")
    clusters = incremental_cluster(unmatched_bags, sim) if unmatched_bags else []

    # 5. 写入 person_group + mapping
    #    - 命中角色库的：映射到角色（同时建一个 group 标记 status=linked_character）
    #    - 聚类结果的：新建 group

    # 先为每个命中角色建一个映射： character_id → group_id
    char_to_group: Dict[int, int] = {}
    # 从 face_character 汇总每个 character 下的视频名解析
    char_extracted_names: Dict[int, List[str]] = defaultdict(list)

    # --- 5a. 角色库命中的写入 ---
    for vid_idx, cid in matched_characters.items():
        if cid not in char_to_group:
            character = DB.get_character(cid)
            if not character:
                continue
            gname = character["name"]
            gid = DB.create_group(
                task_id, gname, status="linked_character", video_count=0,
                extracted_names=[],
            )
            char_to_group[cid] = gid

        item = all_items[vid_idx]
        original = item["path"]
        src_type = item["type"]
        gid = char_to_group[cid]
        mapping_id = DB.create_mapping(
            task_id, gid,
            video_path=original, original_video_path=original,
            source=src_type,
            archive_path=item.get("archive"), in_archive_name=item.get("in_archive"),
        )
        DB.append_task_log(task_id, f"  写入角色库映射: #{cid} → mapping#{mapping_id}")
        names = extract_names(item["display"])
        for n in names:
            char_extracted_names[cid].append(n)

    # 更新 linked_character 分组的 video_count 与人名
    for cid, gid in char_to_group.items():
        maps = DB.list_mappings(group_id=gid)
        nm = list(dict.fromkeys(char_extracted_names[cid]))
        DB.update_group(gid, video_count=len(maps),
                        extracted_names=json.dumps(nm, ensure_ascii=False))

    # --- 5b. 聚类结果写入 ---
    cluster_to_group: Dict[int, int] = {}
    cluster_rep_feature: Dict[int, List[float]] = {}
    cluster_best_frame: Dict[int, tuple] = {}  # cid → (frame, bbox)

    for cl in clusters:
        # 统计该分组下所有视频的文件名解析人名
        all_names: List[str] = []
        vid_in_cluster_count = len(cl.video_indices)

        # 挑选代表帧（人脸质量最高的那个视频的最佳帧）
        best_thumb = None  # (frame, bbox, emb)
        best_thumb_score = -1
        for vi in cl.video_indices:
            if vi in best_frames:
                frame_bgr, bbox, emb = best_frames[vi]
                # 用聚类代表与该emb相似度做权重，越高越合适
                score = float(np.dot(emb, cl.representative))
                if score > best_thumb_score:
                    best_thumb_score = score
                    best_thumb = (frame_bgr, bbox, cl.representative)
            try:
                names = getattr(bags[0], "_meta_names", [])  # 占位，实际下面用bags列表
            except Exception:  # noqa: BLE001
                pass

        # 重新取 names（上面 bags 用了 video_idx→bag 的稀疏映射）
        idx_to_bag = {b.video_idx: b for b in unmatched_bags}
        for vi in cl.video_indices:
            b = idx_to_bag.get(vi)
            if b:
                nm = getattr(b, "_meta_names", [])
                for n in nm:
                    if n not in all_names:
                        all_names.append(n)

        first_video_idx = cl.video_indices[0] if cl.video_indices else 0
        def _display_of(i):
            return all_items[i]["display"] if 0 <= i < len(all_items) else str(i)

        if all_names:
            first_name = all_names[0]
        else:
            first_name = f"人物{cl.cluster_id + 1}"

        # 单人视频状态：若 < min_n 且本策略为 to_uncategorized，仍建分组用于映射
        gid = DB.create_group(
            task_id, first_name, status="auto_numbered",
            video_count=vid_in_cluster_count,
            extracted_names=all_names,
        )
        cluster_to_group[cl.cluster_id] = gid
        cluster_rep_feature[gid] = cl.representative.tolist()
        if best_thumb is not None:
            cluster_best_frame[gid] = (best_thumb[0], best_thumb[1])

        # 冲突判定：>=2 个不同人名 → name_conflict
        if len(all_names) >= 2:
            DB.update_group(gid, status="name_conflict")

        # 写入映射
        for vi in cl.video_indices:
            item = all_items[vi]
            original = item["path"]
            src_type = item["type"]
            DB.create_mapping(
                task_id, gid,
                video_path=original, original_video_path=original,
                source=src_type,
                archive_path=item.get("archive"), in_archive_name=item.get("in_archive"),
            )

    # 6. 满足建夹阈值（>= folder_create_min_videos）：
    #    - 自动入库 face_character（若尚未存在）
    #    - 写入缩略图
    #    - 执行移动（受 test_mode 控制）
    min_n = max(1, settings.folder_create_min_videos)
    DB.append_task_log(task_id, f"建夹阈值：同角色 >= {min_n} 个视频才创建文件夹并移动")

    # 6a. 角色库命中的那些 group：已存在 character，只需累计 video_count
    for cid, gid in char_to_group.items():
        cnt = len(DB.list_mappings(group_id=gid))
        DB.update_character(cid, video_count=DB.get_character(cid)["video_count"] + cnt
                            if DB.get_character(cid) else cnt)
        # 若该角色还没有 folder_path → 设置一个（按名字）
        ch = DB.get_character(cid)
        if ch and not ch.get("folder_path"):
            target_dir = group_output_dir(output_root, ch["name"])
            DB.update_character(cid, folder_path=target_dir)
        # 若角色还没有缩略图，尝试把第一个匹配视频的最佳帧存进去
        if ch and not ch.get("thumbnail_path"):
            # 从该分组下第一个视频的 best_frames 找
            for m in DB.list_mappings(group_id=gid):
                # 这里无法直接对应 video_idx，跳过，留空让用户手动上传
                break

    # 6b. 聚类产生的 group：满足阈值的自动入库为新角色
    created_character_ids: Dict[int, int] = {}  # group_id → character_id
    for cl in clusters:
        gid = cluster_to_group[cl.cluster_id]
        group = DB.get_group(gid)
        if not group:
            continue
        cnt = group["video_count"]
        if cnt < min_n:
            DB.append_task_log(task_id,
                               f"  ⊘ 分组 #{gid} [{group['group_name']}] 视频数 {cnt} < {min_n}，不建夹不入库")
            continue

        # 自动入库为新角色（名字先用 group_name，如果 group_name 被改了）
        feat = cluster_rep_feature.get(gid, [])
        if not feat:
            continue
        character_id = DB.create_character(
            name=group["group_name"], feature=feat,
            video_count=cnt,
        )
        created_character_ids[gid] = character_id
        DB.append_task_log(task_id,
                           f"  ✓ 自动入库新角色 #{character_id} [{group['group_name']}]，视频 {cnt} 个")

        # 保存缩略图（如果有最佳帧）
        if gid in cluster_best_frame:
            frame_bgr, bbox = cluster_best_frame[gid]
            thumb_rel = _save_thumbnail(frame_bgr, bbox, character_id)
            if thumb_rel:
                DB.update_character(character_id, thumbnail_path=thumb_rel)

        # 把 folder_path 写入 character 和 group（如需要）
        target_dir = group_output_dir(output_root, group["group_name"])
        DB.update_character(character_id, folder_path=target_dir)

    # 7. 执行文件移动（受 test_mode 与阈值控制，move_all 内部判断）
    if test_mode:
        DB.append_task_log(task_id, "测试预览模式：只生成预览，不移动磁盘文件")
    move_all(task_id, test_mode)

    # 7.5 【新增】视频内容级去重：为所有 mapping 计算指纹 → 检测重复 → 归档到 _重复文件_[/子目录]/
    if settings.dedup_enabled:
        DB.append_task_log(task_id, "开始视频内容去重处理 ...")
        dedup_archived = 0
        dedup_previewed = 0
        # mapping_id → 所属角色的顶层目录（group→character→folder_path，或按 group_name 生成 output_root/<group_name>）
        all_mappings = DB.list_mappings(task_id=task_id)
        all_groups = {g["group_id"]: g for g in DB.list_groups(task_id)}
        # 先收集 mapping_id → video_idx（反向映射）
        # 由于 dedup_keysamples_for_idx 用的是 all_items 的下标 idx，而 mapping 上没有存 idx，
        # 这里通过 original_video_path + source 匹配（精确，任务内唯一）
        idx_item_path_info: Dict[str, int] = {}  # key = type|archive|inarchive|path → idx
        for i, it in enumerate(all_items):
            if it.get("type") == "archive":
                k = f"archive|{it.get('archive','')}|{it.get('in_archive','')}|{it.get('path','')}"
            else:
                k = f"file|||{it.get('path','')}"
            idx_item_path_info[k] = i

        def _video_idx_from_mapping(m) -> Optional[int]:
            if m["source"] == "archive":
                k = f"archive|{m.get('archive_path','')}|{m.get('in_archive_name','')}|{m.get('original_video_path','')}"
            else:
                k = f"file|||{m.get('original_video_path','')}"
            return idx_item_path_info.get(k)

        # 阶段 7.5.1：遍历所有 mapping，写入/补全 face_video_fingerprint，并标记 duplicate_of
        mid_to_fpid: Dict[int, int] = {}
        for m in all_mappings:
            vid_idx = _video_idx_from_mapping(m)
            hashes: List[str] = []
            duration = None
            width = None
            height = None
            file_size = 0
            if vid_idx is not None and vid_idx in dedup_keysamples_for_idx:
                hashes = list(dedup_keysamples_for_idx[vid_idx])
                meta = dedup_metainfo.get(vid_idx)
                if meta:
                    duration, width, height, file_size, _ = meta
            if not hashes and settings.dedup_enabled:
                # 兜底：二次算指纹（对原路径落盘视频）
                try:
                    if m["source"] == "file" and os.path.exists(m["video_path"]):
                        fp = compute_fingerprint(video_path=m["video_path"])
                    elif m["source"] == "archive" and m["archive_path"] and m["in_archive_name"]:
                        vbytes = read_entry_bytes(m["archive_path"], m["in_archive_name"])
                        if vbytes:
                            fp = compute_fingerprint(video_bytes=vbytes)
                        else:
                            fp = None
                    else:
                        fp = None
                    if fp:
                        hashes = list(fp.hashes)
                        duration = fp.duration_sec
                        width = fp.width
                        height = fp.height
                        file_size = fp.file_size
                except Exception as e:  # noqa: BLE001
                    log.warning("兜底指纹计算失败(忽略): %s", e)
            if not hashes:
                # 无指纹 → 跳过
                continue

            # 快速筛候选（只看"主视频"，duplicate_of 空）
            cands = DB.find_duplicate_candidates(
                duration, width, height, file_size,
                duration_tolerance=settings.dedup_duration_tolerance_sec,
            )
            # 比较 dHash
            cur_fp_obj = VideoFingerprint(
                m["video_path"], duration, width, height, file_size, hashes,
            )
            master_id = find_matching_master(cur_fp_obj, cands)
            fpid = DB.create_fingerprint(
                video_path=m["video_path"],
                original_video_path=m["original_video_path"],
                hashes=hashes,
                duration_sec=duration, width=width, height=height, file_size=file_size,
                task_id=task_id, mapping_id=m["id"],
                duplicate_of=master_id,
            )
            mid_to_fpid[m["id"]] = fpid

        # 阶段 7.5.2：对 duplicate_of 非空的视频，按"所属角色目录"执行归档移动
        # 先建立 mapping_id → 所属目标角色顶层目录
        def _role_target_dir(m) -> Optional[str]:
            g = all_groups.get(m["group_id"])
            if not g:
                return None
            # 优先用关联 character.folder_path；否则按 output_root+group_name 生成（与 move_all 一致）
            cid = None
            if g["status"] == "linked_character":
                # 查 character.name == g.group_name 的 active 角色
                for ch in DB.list_characters(include_deleted=True):
                    if ch["name"] == g["group_name"]:
                        cid = ch["character_id"]
                        break
            if cid is None:
                # 查创建记录里的 created_character_ids（上一步的字典在本函数局部变量里不可见，退而求其次：按 group_name 和 folder_path 输出目录生成）
                for ch in DB.list_characters(include_deleted=True):
                    if ch["original_name"] == g["group_name"] or ch["name"] == g["group_name"]:
                        cid = ch["character_id"]
                        break
            if cid is not None:
                ch = DB.get_character(cid)
                if ch and ch.get("folder_path"):
                    return ch["folder_path"]
            # 兜底：和 move_all 的输出目录一致
            return group_output_dir(output_root, g["group_name"])

        for m in all_mappings:
            fpid = mid_to_fpid.get(m["id"])
            if not fpid:
                continue
            fp_rec = DB.get_fingerprint(fpid)
            if not fp_rec or not fp_rec.get("duplicate_of"):
                continue  # 主视频 / 未发现重复
            if fp_rec.get("ignored"):
                continue
            if m["source"] == "archive":
                DB.append_task_log(task_id,
                                   f"  ⊘ 重复压缩包内视频不落盘（mapping#{m['id']} 与 master#{fp_rec['duplicate_of']}），不移动")
                continue

            role_dir = _role_target_dir(m)
            if not role_dir:
                DB.append_task_log(task_id, f"  ⊘ mapping#{m['id']}: 无法定位角色目录，跳过去重归档")
                continue

            # 该角色目录下全部指纹（用于决定嵌套目录）→ 从 DB 取 only_within_dir
            fps_in_role = DB.list_fingerprints(only_within_dir=role_dir)
            current_video_path = m["video_path"] if os.path.exists(m["video_path"]) else m["original_video_path"]
            if not os.path.exists(current_video_path):
                continue

            result = move_duplicate_to_repeat_dir(
                current_video_path,
                role_dir,
                fp_rec.get("hashes") or [],
                test_mode=test_mode,
                fingerprints_in_role=fps_in_role,
            )
            if result.get("success"):
                if result.get("moved"):
                    dedup_archived += 1
                    # 更新 mapping.video_path 和 fingerprint.video_path
                    DB.update_mapping(m["id"], video_path=result["target_path"])
                    DB.update_fingerprint(fpid, video_path=result["target_path"])
                elif result.get("test_mode"):
                    dedup_previewed += 1
                msg = f"  ⇲ 重复视频：{os.path.basename(current_video_path)[:50]} 已" + (
                    "归档" if result.get("moved") else (
                        f"[预览]拟归档 → {result.get('target_dir','')}" if result.get("test_mode") else ""
                    )
                )
                DB.append_task_log(task_id, msg)

        msg_dd = f"内容去重完毕：检测到 {dedup_archived + dedup_previewed} 个重复视频"
        if test_mode:
            msg_dd += f"（预览模式：{dedup_previewed} 个拟归档）"
        else:
            msg_dd += f"（已归档 {dedup_archived} 个到 {settings.dedup_repeat_folder_name} 嵌套目录）"
        DB.append_task_log(task_id, msg_dd)
    else:
        DB.append_task_log(task_id, "内容去重关闭（DEDUP_ENABLED=false），跳过")

    # 8. 完成
    DB.update_task(task_id, status="completed", processed_videos=processed, current_video=None)
    DB.append_task_log(task_id, f"扫描任务 #{task_id} 完成")
    _rt.running_task_id = None


# ============================================================
# 对外接口
# ============================================================
async def enqueue_scan(
    scan_folder: str,
    output_dir: Optional[str],
    test_mode: bool,
    similarity: float,
    *,
    use_character_library: bool = True,
) -> int:
    if not validate_scan_folder(scan_folder):
        raise ValueError("扫描目录无效或不存在")
    out = output_dir or scan_folder
    if not is_safe_output_path(out, scan_folder):
        raise ValueError("输出路径不安全或非法")
    task_id = DB.create_task(
        scan_folder, output_dir, test_mode, similarity,
        use_character_library=use_character_library,
    )
    with _rt.lock:
        if _rt.running_task_id is None:
            _rt.running_task_id = task_id
            # 放到后台跑
            asyncio.create_task(_do_scan(task_id))
        else:
            _rt.queued.append(task_id)
    return task_id


def stop_task(task_id: int) -> bool:
    with _rt.lock:
        if _rt.running_task_id == task_id:
            _rt.stop_flag = True
    return True


def get_status() -> Dict[str, Any]:
    latest_task = None
    recent = DB.list_recent_tasks(10)
    if recent:
        t = dict(recent[0])
        t["logs"] = t.get("logs") or ""
        progress = 0
        if t["total_videos"]:
            progress = int(round(t["processed_videos"] / t["total_videos"] * 100))
        t["progress"] = progress
        latest_task = t
    return {
        "running_task_id": _rt.running_task_id,
        "queued_count": len(_rt.queued),
        "latest": latest_task,
        "recent_tasks": recent,
    }


def resume_pending() -> int:
    """启动时断点续跑：恢复 pending/running 任务（这里仅重置状态为 failed 以便用户手动触发，避免重复移动）。"""
    restored = 0
    for t in DB.list_pending_tasks(limit=50):
        DB.update_task(t["id"], status="cancelled",
                       error_msg="服务重启，任务保留用于审计；如需重跑请重新发起扫描。")
        restored += 1
    return restored


# ============================================================
# 【新增】单视频重新处理（重新分析 1 个 mapping）
# ============================================================
async def reprocess_single(task_id: int, mapping_id: int) -> Dict[str, Any]:
    """重新处理单个视频：重新抽帧+检测，结果更新到原映射/分组/角色。"""
    from ..database import get_mapping, update_mapping

    m = get_mapping(mapping_id)
    if not m:
        return {"success": False, "message": "映射不存在"}
    # 简化实现：把这个 mapping 标记为"待重跑"，然后重新对该视频单独跑一次检测流程
    # （实际生产中可复用 _do_scan 的内部逻辑，这里为代码简洁采用独立函数）
    engine = FaceEngine.instance()
    engine.init()

    # 读取视频
    video_bytes = None
    video_path = None
    if m["source"] == "archive" and m["archive_path"] and m["in_archive_name"]:
        video_bytes = read_entry_bytes(m["archive_path"], m["in_archive_name"])
        if not video_bytes:
            return {"success": False, "message": "无法读取压缩包内视频"}
    else:
        video_path = m["original_video_path"]
        if not os.path.exists(video_path):
            return {"success": False, "message": f"源视频不存在: {video_path}"}

    bag = VideoFaceBag(0, video_path or "archive")
    iterable = extract_frames(video_path=video_path, video_bytes=video_bytes)
    for frame in iterable:
        try:
            faces = engine.extract_valid_faces(frame)
        except Exception:  # noqa: BLE001
            continue
        for f in faces:
            bag.add(f.embedding, f.mask_tolerant_applied)
    if bag.face_count == 0 or bag.representative() is None:
        return {"success": False, "message": "重新处理后仍未检测到符合条件的人脸"}

    # 匹配角色库
    sim = settings.similarity
    chars = DB.list_characters(include_deleted=False)
    char_feats = []
    for c in chars:
        ft = c.get("feature") or []
        if len(ft) >= 128:
            arr = np.asarray(ft, dtype=np.float32)
            arr = arr / (np.linalg.norm(arr) + 1e-9)
            char_feats.append((c["character_id"], arr))
    rep = bag.representative()
    assert rep is not None
    hit = match_to_character_library(rep, char_feats, sim) if char_feats else None
    if hit:
        # 更新映射：改为该角色对应的 group（若同任务下存在 linked_character group）
        groups = DB.list_groups(task_id)
        target_gid = None
        # 找 linked_character 的 group，其 original_group_name == character.name
        ch = DB.get_character(hit)
        if ch:
            for g in groups:
                if g["status"] == "linked_character" and g["group_name"] == ch["name"]:
                    target_gid = g["group_id"]
                    break
            if target_gid is None and ch:
                target_gid = DB.create_group(
                    task_id, ch["name"], status="linked_character",
                    video_count=0, extracted_names=[],
                )
        if target_gid:
            update_mapping(mapping_id, group_id=target_gid)
            return {"success": True,
                    "message": f"重新处理完成，已匹配角色 #{hit} [{ch['name'] if ch else ''}]"}

    return {"success": True, "message": "重新处理完成，已更新特征（未命中角色库，留在原分组）"}
