"""
数据库访问层。
- 复用萤核原版 SQLite 数据库（FIREFLY_DB_PATH），绝不新建独立库。
- 仅新增 3 张表：video_task / face_person_group / face_video_mapping。
- 不修改萤核原有任何表结构与初始化逻辑（全部 CREATE TABLE IF NOT EXISTS）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .config import settings
from .utils.logger import get_logger

log = get_logger()

# 连接锁（写串行，避免 SQLite 并发写冲突）
_lock = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _db_path() -> str:
    p = Path(settings.firefly_db_path).expanduser().resolve()
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # 外键约束
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """服务启动时自动建表（幂等）。"""
    with _lock, get_conn() as conn:
        # 任务表
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_task (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_folder     TEXT NOT NULL,
                output_dir      TEXT NOT NULL,
                test_mode       INTEGER NOT NULL DEFAULT 1,
                status          TEXT NOT NULL DEFAULT 'queued',
                progress        REAL NOT NULL DEFAULT 0,
                total_videos    INTEGER NOT NULL DEFAULT 0,
                processed_videos INTEGER NOT NULL DEFAULT 0,
                current_video   TEXT,
                similarity      REAL NOT NULL DEFAULT 0.55,
                logs            TEXT NOT NULL DEFAULT '',
                error           TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_task_status ON video_task(status)"
        )

        # 人物分组表
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS face_person_group (
                group_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id         INTEGER NOT NULL,
                group_name      TEXT NOT NULL,
                original_group_name TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'auto_numbered',
                video_count     INTEGER NOT NULL DEFAULT 0,
                extracted_names TEXT NOT NULL DEFAULT '[]',
                repr_embedding  BLOB,
                created_at      TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES video_task(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_face_person_group_task ON face_person_group(task_id)"
        )
        # 兼容旧库：若表已存在但缺少 repr_embedding 列，自动补列
        try:
            conn.execute("SELECT repr_embedding FROM face_person_group LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE face_person_group ADD COLUMN repr_embedding BLOB")

        # 视频-分组映射表（含原始路径/原始分组名，用于回滚）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS face_video_mapping (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id            INTEGER NOT NULL,
                task_id             INTEGER NOT NULL,
                video_path          TEXT NOT NULL,
                original_video_path TEXT NOT NULL,
                original_group_name TEXT NOT NULL,
                moved               INTEGER NOT NULL DEFAULT 0,
                source              TEXT NOT NULL DEFAULT 'file',
                archive_path        TEXT,
                in_archive_name     TEXT,
                created_at          TEXT NOT NULL,
                FOREIGN KEY(group_id) REFERENCES face_person_group(group_id) ON DELETE CASCADE,
                FOREIGN KEY(task_id) REFERENCES video_task(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_face_video_mapping_group ON face_video_mapping(group_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_face_video_mapping_task ON face_video_mapping(task_id)"
        )

    log.info("数据库初始化完成（仅新增表）: %s", _db_path())


# ===================== video_task =====================
def create_task(scan_folder: str, output_dir: str, test_mode: bool, similarity: float) -> int:
    now = _now()
    with _lock, get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO video_task
               (scan_folder, output_dir, test_mode, status, progress, similarity, created_at, updated_at)
               VALUES (?,?,?,?,0,?,?,?)""",
            (scan_folder, output_dir, int(test_mode), "queued", similarity, now, now),
        )
        return int(cur.lastrowid)


def append_log(task_id: int, line: str) -> None:
    now = _now()
    safe_line = line.replace("\n", " ").replace("\r", " ")
    with _lock, get_conn() as conn:
        row = conn.execute(
            "SELECT logs FROM video_task WHERE id=?", (task_id,)
        ).fetchone()
        existing = row["logs"] if row else ""
        # 仅保留最后 800 行，避免无限增长
        lines = existing.split("\n") if existing else []
        lines.append(f"[{now}] {safe_line}")
        if len(lines) > 800:
            lines = lines[-800:]
        conn.execute(
            "UPDATE video_task SET logs=?, updated_at=? WHERE id=?",
            ("\n".join(lines), now, task_id),
        )


def update_task(task_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [task_id]
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE video_task SET {cols} WHERE id=?", vals)


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM video_task WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def list_tasks(limit: int = 50) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM video_task ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def latest_task() -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM video_task ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


# ===================== face_person_group =====================
def create_group(task_id: int, group_name: str, status: str = "auto_numbered") -> int:
    now = _now()
    with _lock, get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO face_person_group
               (task_id, group_name, original_group_name, status, video_count, extracted_names, created_at)
               VALUES (?,?,?,?,0,'[]',?)""",
            (task_id, group_name, group_name, status, now),
        )
        return int(cur.lastrowid)


def update_group(group_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [group_id]
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE face_person_group SET {cols} WHERE group_id=?", vals)


def get_group(group_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM face_person_group WHERE group_id=?", (group_id,)
        ).fetchone()
        return dict(row) if row else None


def list_groups(task_id: Optional[int] = None) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        if task_id is not None:
            rows = conn.execute(
                "SELECT * FROM face_person_group WHERE task_id=? ORDER BY group_id",
                (task_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM face_person_group ORDER BY group_id"
            ).fetchall()
        return [dict(r) for r in rows]


def add_extracted_name(group_id: int, name: str) -> List[str]:
    """为分组追加一个解析出的人名，返回去重后的人名列表。"""
    with _lock, get_conn() as conn:
        row = conn.execute(
            "SELECT extracted_names FROM face_person_group WHERE group_id=?", (group_id,)
        ).fetchone()
        names: List[str] = json.loads(row["extracted_names"]) if row else []
        if name and name not in names:
            names.append(name)
            conn.execute(
                "UPDATE face_person_group SET extracted_names=? WHERE group_id=?",
                (json.dumps(names, ensure_ascii=False), group_id),
            )
        return names


def set_repr_embedding(group_id: int, blob: Optional[bytes]) -> None:
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE face_person_group SET repr_embedding=? WHERE group_id=?",
            (blob, group_id),
        )


def get_repr_embedding(group_id: int) -> Optional[bytes]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT repr_embedding FROM face_person_group WHERE group_id=?", (group_id,)
        ).fetchone()
        return row["repr_embedding"] if row else None


# ===================== face_video_mapping =====================
def create_mapping(
    group_id: int,
    task_id: int,
    video_path: str,
    original_video_path: str,
    original_group_name: str,
    source: str = "file",
    archive_path: Optional[str] = None,
    in_archive_name: Optional[str] = None,
) -> int:
    now = _now()
    with _lock, get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO face_video_mapping
               (group_id, task_id, video_path, original_video_path, original_group_name,
                moved, source, archive_path, in_archive_name, created_at)
               VALUES (?,?,?,?,?,0,?,?,?,?)""",
            (
                group_id, task_id, video_path, original_video_path, original_group_name,
                source, archive_path, in_archive_name, now,
            ),
        )
        # 同步分组视频计数
        conn.execute(
            "UPDATE face_person_group SET video_count = video_count + 1 WHERE group_id=?",
            (group_id,),
        )
        return int(cur.lastrowid)


def list_mappings(group_id: Optional[int] = None, task_id: Optional[int] = None) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        sql = "SELECT * FROM face_video_mapping WHERE 1=1"
        params: List[Any] = []
        if group_id is not None:
            sql += " AND group_id=?"
            params.append(group_id)
        if task_id is not None:
            sql += " AND task_id=?"
            params.append(task_id)
        sql += " ORDER BY id"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def update_mapping(mapping_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [mapping_id]
    with _lock, get_conn() as conn:
        conn.execute(f"UPDATE face_video_mapping SET {cols} WHERE id=?", vals)


def get_mapping(mapping_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM face_video_mapping WHERE id=?", (mapping_id,)
        ).fetchone()
        return dict(row) if row else None


def delete_mapping(mapping_id: int) -> None:
    """删除映射并同步递减所属分组视频计数。"""
    with _lock, get_conn() as conn:
        row = conn.execute(
            "SELECT group_id FROM face_video_mapping WHERE id=?", (mapping_id,)
        ).fetchone()
        if not row:
            return
        conn.execute("DELETE FROM face_video_mapping WHERE id=?", (mapping_id,))
        conn.execute(
            "UPDATE face_person_group SET video_count = MAX(0, video_count - 1) WHERE group_id=?",
            (row["group_id"],),
        )
