
import os
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def migrate(database_path: str) -> list[str]:
    """按文件名顺序应用 migrations/ 下所有 SQL 迁移，返回本次应用的版本。"""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    applied: list[str] = []
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, "
            "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        done = {
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations")
        }
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if sql_file.stem in done:
                continue
            connection.executescript(sql_file.read_text(encoding="utf-8"))
            applied.append(sql_file.stem)
    return applied


def main() -> None:
    database_path = os.getenv("DATABASE_PATH", "data/app.sqlite3")
    applied = migrate(database_path)
    print(f"数据库迁移完成：{database_path}（本次应用：{', '.join(applied) or '无'}）")


if __name__ == "__main__":
    main()
