"""
数据库访问层：复用萤核 SQLite 数据库，仅新增表，不修改原表。

新增表（均以前缀 face_ 隔离，避免与萤核原表冲突）：
  1. face_scan_task        — 扫描任务队列 / 进度 / 断点续跑
  2. face_person_group     — 聚类后的人物分组（自动编号/人工命名）
  3. face_video_mapping    — 视频 → 分组的映射（含移动前后路径，支持回滚）
  4. face_character        — 【新增】角色库：命名角色 + 特征向量 + 缩略图 + 文件夹路径
  5. face_service_setting  — 【新增】外挂服务配置：工作文件夹路径等

启动阶段内置幂等迁移：建表 + 列缺失 ALTER，无需人工执行迁移脚本。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, List, Optional

from .config import resolve_path, settings
from .utils.logger import get_logger

log = get_logger()

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def _db_path() -> str:
    return resolve_path(settings.firefly_db_path)


def _connect() -> sqlite3.Connection:
    """获取（或新建）单例连接，开启 WAL + 外键。"""
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                dbp = _db_path()
                Path(dbp).parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(dbp, check_same_thread=False, timeout=30)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                try:
                    conn.execute("PRAGMA journal_mode = WAL")
                except Exception:  # noqa: BLE001
                    pass
                _conn = conn
    return _conn


def _cur() -> sqlite3.Cursor:
    return _connect().cursor()


def _commit() -> None:
    _connect().commit()


# ==============================================================
# 启动阶段：幂等建表 + 迁移
# ==============================================================
_TABLES_DDL: dict[str, str] = {
    "face_scan_task": """
        CREATE TABLE IF NOT EXISTS face_scan_task (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_folder      TEXT    NOT NULL,
            output_dir       TEXT,
            test_mode        INTEGER NOT NULL DEFAULT 1,
            similarity       REAL    NOT NULL DEFAULT 0.55,
            status           TEXT    NOT NULL DEFAULT 'pending',
            -- pending / running / completed / failed / cancelled
            total_videos     INTEGER NOT NULL DEFAULT 0,
            processed_videos INTEGER NOT NULL DEFAULT 0,
            current_video    TEXT,
            logs             TEXT    NOT NULL DEFAULT '',
            error_msg        TEXT,
            created_at       REAL    NOT NULL,
            updated_at       REAL    NOT NULL
        )
    """,
    "face_person_group": """
        CREATE TABLE IF NOT EXISTS face_person_group (
            group_id             INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id              INTEGER NOT NULL,
            group_name           TEXT    NOT NULL,
            original_group_name  TEXT    NOT NULL,
            status               TEXT    NOT NULL DEFAULT 'auto_numbered',
            -- auto_numbered / renamed / name_conflict / multi_person / deleted / merged
            video_count          INTEGER NOT NULL DEFAULT 0,
            extracted_names      TEXT,              -- JSON 字符串数组
            created_at           REAL    NOT NULL,
            updated_at           REAL    NOT NULL,
            FOREIGN KEY (task_id) REFERENCES face_scan_task(id) ON DELETE CASCADE
        )
    """,
    "face_video_mapping": """
        CREATE TABLE IF NOT EXISTS face_video_mapping (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id             INTEGER NOT NULL,
            group_id            INTEGER NOT NULL,
            video_path          TEXT    NOT NULL,        -- 当前路径（移动后）
            original_video_path TEXT    NOT NULL,        -- 原始路径（用于回滚）
            moved               INTEGER NOT NULL DEFAULT 0,
            source              TEXT    NOT NULL DEFAULT 'file', -- file / archive
            archive_path        TEXT,                    -- 压缩包路径（source=archive 时）
            in_archive_name     TEXT,
            created_at          REAL    NOT NULL,
            updated_at          REAL    NOT NULL,
            FOREIGN KEY (task_id)  REFERENCES face_scan_task(id)    ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES face_person_group(group_id) ON DELETE CASCADE
        )
    """,
    "face_character": """
        CREATE TABLE IF NOT EXISTS face_character (
            character_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT    NOT NULL,             -- 角色名（用户可改）
            original_name  TEXT    NOT NULL,             -- 初始自动生成名（保留用于审计）
            -- 代表人脸特征向量（JSON 存 [512] 浮点数组）
            feature_json   TEXT    NOT NULL,
            -- 缩略图文件路径（相对于 THUMBNAIL_DIR，可为空）
            thumbnail_path TEXT,
            -- 该角色最终的文件夹绝对路径（命名变更时联动重命名）
            folder_path    TEXT,
            video_count    INTEGER NOT NULL DEFAULT 0,   -- 统计用，方便前端展示
            status         TEXT    NOT NULL DEFAULT 'active', -- active / deleted
            created_at     REAL    NOT NULL,
            updated_at     REAL    NOT NULL
        )
    """,
    "face_service_setting": """
        CREATE TABLE IF NOT EXISTS face_service_setting (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """,
}


def _ensure_columns(table: str, ddl: dict) -> None:
    """幂等：表已存在但缺失列时 ALTER ADD（按列名存在性判断）。"""
    c = _cur()
    c.execute(f"PRAGMA table_info({table})")
    existing_cols = {row[1] for row in c.fetchall()}
    for col_name, col_def in ddl.items():
        if col_name not in existing_cols:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
                log.info("ALTER TABLE %s ADD %s OK", table, col_name)
            except Exception as e:  # noqa: BLE001
                log.warning("ALTER TABLE %s ADD %s 失败（忽略）: %s", table, col_name, e)


# 列迁移：各表后续可能追加的列（key=列名，value=类型+默认定义）
_COL_MIGRATIONS: dict[str, dict[str, str]] = {
    # face_character 为新表，暂无追加列；预留扩展位
    "face_scan_task": {
        "use_character_library": "INTEGER NOT NULL DEFAULT 1",
    },
}


def init_db() -> None:
    """启动时调用：幂等建表 + 幂等列迁移。数据库不存在则自动创建目录与文件。"""
    with _lock:
        c = _cur()
        for table, ddl in _TABLES_DDL.items():
            c.execute(ddl)
            if table in _COL_MIGRATIONS:
                _ensure_columns(table, _COL_MIGRATIONS[table])
        _commit()
        log.info("数据库初始化完成（仅新增表）: %s", _db_path())


# ==============================================================
# 通用
# ==============================================================
def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row else None


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def _now() -> float:
    return time.time()


# ==============================================================
# face_scan_task
# ==============================================================
def create_task(
    scan_folder: str,
    output_dir: Optional[str],
    test_mode: bool,
    similarity: float,
    *,
    use_character_library: bool = True,
) -> int:
    c = _cur()
    now = _now()
    c.execute(
        """
        INSERT INTO face_scan_task
        (scan_folder, output_dir, test_mode, similarity, status,
         total_videos, processed_videos, logs, created_at, updated_at,
         use_character_library)
        VALUES (?,?,?,?,?, 0,0,'',?,?, ?)
        """,
        (
            scan_folder, output_dir, 1 if test_mode else 0, similarity, "pending",
            now, now, 1 if use_character_library else 0,
        ),
    )
    _commit()
    return int(c.lastrowid)


def get_task(task_id: int) -> Optional[dict]:
    c = _cur()
    c.execute("SELECT * FROM face_scan_task WHERE id=?", (task_id,))
    return _row_to_dict(c.fetchone())


def list_recent_tasks(limit: int = 20) -> list[dict]:
    c = _cur()
    c.execute("SELECT * FROM face_scan_task ORDER BY id DESC LIMIT ?", (limit,))
    return _rows_to_dicts(c.fetchall())


def update_task(task_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [task_id]
    _cur().execute(f"UPDATE face_scan_task SET {cols} WHERE id=?", vals)
    _commit()


def append_task_log(task_id: int, line: str) -> None:
    """追加日志（控制总长度，避免单行无限增长）。"""
    c = _cur()
    c.execute("SELECT logs FROM face_scan_task WHERE id=?", (task_id,))
    row = c.fetchone()
    if not row:
        return
    old = row[0] or ""
    new = (old + line + "\n")[-300_000:]  # 保留尾部 30 万字节约束
    c.execute(
        "UPDATE face_scan_task SET logs=?, updated_at=? WHERE id=?",
        (new, _now(), task_id),
    )
    _commit()


def list_pending_tasks(limit: int = 50) -> list[dict]:
    """断点续跑用：获取 pending / running 状态的任务。"""
    c = _cur()
    c.execute(
        "SELECT * FROM face_scan_task WHERE status IN ('pending','running') ORDER BY id LIMIT ?",
        (limit,),
    )
    return _rows_to_dicts(c.fetchall())


# ==============================================================
# face_person_group
# ==============================================================
def create_group(
    task_id: int,
    group_name: str,
    *,
    status: str = "auto_numbered",
    video_count: int = 0,
    extracted_names: Optional[list[str]] = None,
) -> int:
    c = _cur()
    now = _now()
    c.execute(
        """
        INSERT INTO face_person_group
        (task_id, group_name, original_group_name, status, video_count, extracted_names, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            task_id, group_name, group_name, status, video_count,
            json.dumps(extracted_names or [], ensure_ascii=False),
            now, now,
        ),
    )
    _commit()
    return int(c.lastrowid)


def get_group(group_id: int) -> Optional[dict]:
    c = _cur()
    c.execute("SELECT * FROM face_person_group WHERE group_id=?", (group_id,))
    return _row_to_dict(c.fetchone())


def list_groups(task_id: Optional[int] = None) -> list[dict]:
    c = _cur()
    if task_id is None:
        c.execute("SELECT * FROM face_person_group ORDER BY group_id")
    else:
        c.execute("SELECT * FROM face_person_group WHERE task_id=? ORDER BY group_id", (task_id,))
    return _rows_to_dicts(c.fetchall())


def update_group(group_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [group_id]
    _cur().execute(f"UPDATE face_person_group SET {cols} WHERE group_id=?", vals)
    _commit()


# ==============================================================
# face_video_mapping
# ==============================================================
def create_mapping(
    task_id: int,
    group_id: int,
    video_path: str,
    original_video_path: str,
    *,
    moved: bool = False,
    source: str = "file",
    archive_path: Optional[str] = None,
    in_archive_name: Optional[str] = None,
) -> int:
    c = _cur()
    now = _now()
    c.execute(
        """
        INSERT INTO face_video_mapping
        (task_id, group_id, video_path, original_video_path, moved,
         source, archive_path, in_archive_name, created_at, updated_at)
        VALUES (?,?,?,?,?, ?,?,?,?,?)
        """,
        (
            task_id, group_id, video_path, original_video_path, 1 if moved else 0,
            source, archive_path, in_archive_name, now, now,
        ),
    )
    _commit()
    return int(c.lastrowid)


def get_mapping(mapping_id: int) -> Optional[dict]:
    c = _cur()
    c.execute("SELECT * FROM face_video_mapping WHERE id=?", (mapping_id,))
    return _row_to_dict(c.fetchone())


def list_mappings(
    *,
    task_id: Optional[int] = None,
    group_id: Optional[int] = None,
) -> list[dict]:
    c = _cur()
    sql = "SELECT * FROM face_video_mapping WHERE 1=1"
    args: list[Any] = []
    if task_id is not None:
        sql += " AND task_id=?"
        args.append(task_id)
    if group_id is not None:
        sql += " AND group_id=?"
        args.append(group_id)
    sql += " ORDER BY id"
    c.execute(sql, args)
    return _rows_to_dicts(c.fetchall())


def update_mapping(mapping_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [mapping_id]
    _cur().execute(f"UPDATE face_video_mapping SET {cols} WHERE id=?", vals)
    _commit()


# ==============================================================
# 【新增】face_character 角色库
# ==============================================================
def create_character(
    name: str,
    feature: list[float],
    *,
    thumbnail_path: Optional[str] = None,
    folder_path: Optional[str] = None,
    video_count: int = 0,
) -> int:
    c = _cur()
    now = _now()
    c.execute(
        """
        INSERT INTO face_character
        (name, original_name, feature_json, thumbnail_path, folder_path, video_count, status, created_at, updated_at)
        VALUES (?,?,?,?,?,?, 'active', ?, ?)
        """,
        (
            name, name,
            json.dumps(feature, ensure_ascii=False),
            thumbnail_path, folder_path, video_count,
            now, now,
        ),
    )
    _commit()
    return int(c.lastrowid)


def get_character(character_id: int) -> Optional[dict]:
    c = _cur()
    c.execute("SELECT * FROM face_character WHERE character_id=?", (character_id,))
    row = _row_to_dict(c.fetchone())
    if row and row.get("feature_json"):
        try:
            row["feature"] = json.loads(row["feature_json"])
        except Exception:  # noqa: BLE001
            row["feature"] = []
    return row


def list_characters(include_deleted: bool = False) -> list[dict]:
    c = _cur()
    if include_deleted:
        c.execute("SELECT * FROM face_character ORDER BY character_id")
    else:
        c.execute("SELECT * FROM face_character WHERE status != 'deleted' ORDER BY character_id")
    rows = _rows_to_dicts(c.fetchall())
    for r in rows:
        try:
            r["feature"] = json.loads(r["feature_json"]) if r.get("feature_json") else []
        except Exception:  # noqa: BLE001
            r["feature"] = []
    return rows


def update_character(character_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    if "feature" in fields:
        fields["feature_json"] = json.dumps(fields.pop("feature"), ensure_ascii=False)
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [character_id]
    _cur().execute(f"UPDATE face_character SET {cols} WHERE character_id=?", vals)
    _commit()


def delete_character(character_id: int) -> None:
    """软删除角色（不删磁盘文件夹和视频，也不删除 group / mapping）。"""
    update_character(character_id, status="deleted")


# ==============================================================
# 【新增】face_service_setting 简单 KV 表
# ==============================================================
def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    c = _cur()
    c.execute("SELECT value FROM face_service_setting WHERE key=?", (key,))
    row = c.fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    c = _cur()
    c.execute(
        "INSERT INTO face_service_setting(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    _commit()


def all_settings() -> dict[str, str]:
    c = _cur()
    c.execute("SELECT key, value FROM face_service_setting")
    return {r[0]: r[1] for r in c.fetchall()}
