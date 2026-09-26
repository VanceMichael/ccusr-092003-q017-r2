"""数据库连接与迁移执行。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def connect(database_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(database_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_migrations(conn: sqlite3.Connection, migrations_dir: str | Path | None = None) -> list[str]:
    """按文件名顺序应用 migrations/ 下尚未执行的 SQL，返回本次应用的版本号。"""
    directory = Path(migrations_dir) if migrations_dir else MIGRATIONS_DIR
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    newly_applied: list[str] = []
    for sql_file in sorted(directory.glob("*.sql")):
        version = sql_file.stem
        if version in applied:
            continue
        conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (version,))
        newly_applied.append(version)
    conn.commit()
    return newly_applied
