"""初始化/升级数据库：按顺序应用 migrations/ 下全部 SQL 文件。"""

import os
from pathlib import Path

from app.db import apply_migrations, connect


def main() -> None:
    database_path = Path(os.getenv("DATABASE_PATH", "data/app.sqlite3"))
    database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(database_path))
    applied = apply_migrations(conn)
    conn.close()
    if applied:
        print(f"数据库迁移完成：{database_path}（本次应用：{', '.join(applied)}）")
    else:
        print(f"数据库已是最新：{database_path}")


if __name__ == "__main__":
    main()
