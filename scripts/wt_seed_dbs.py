"""浏览器实测环境准备：源/目标 SQLite + 平台库（幂等，可重复执行）

用法（在包根目录）：
    runtime\\python.exe scripts/wt_seed_dbs.py
产物：data_wt/source.db、data_wt/target.db（表结构相同，目标先留空）
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WT = ROOT / "data_wt"
WT.mkdir(exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT,
    qty INTEGER,
    price REAL,
    updated_at TEXT
)
"""

ROWS = [
    (1, "P-1001", "注塑外壳", "注塑件", 120, 3.5, "2026-09-28 09:00:00"),
    (2, "P-1002", "五金支架", "五金件", 80, 7.25, "2026-09-28 10:30:00"),
    (3, "P-1003", "电子控制板", "电子料", 45, 128.0, "2026-09-29 08:15:00"),
    (4, "P-1004", "包装内衬", "包装材", 300, 0.85, "2026-09-29 14:40:00"),
    (5, "P-1005", "连接线束", "电子料", 210, 4.2, "2026-09-30 07:05:00"),
]


def main() -> None:
    source = WT / "source.db"
    target = WT / "target.db"
    for path in (source, target):
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        if path == source:
            conn.executemany(
                "INSERT OR REPLACE INTO products VALUES (?,?,?,?,?,?,?)", ROWS)
        conn.commit()
        conn.close()
    print(f"source -> {source}")
    print(f"target -> {target}")


if __name__ == "__main__":
    main()
