"""
发布历史落库。

用 SQLite 而不是 MySQL：单机、单写者，要的就是零运维。
一个部署工具要是自己还得先保证数据库活着，那本身就是设计缺陷。
"""
import sqlite3
from contextlib import closing
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "releases.db"

# 状态存英文，显示的时候再翻成中文——机器读的和人读的分开
STATUS_TEXT = {
    "success": "成功",
    "failed": "失败",
    "rolled_back": "已回滚",
    "rollback": "手动回滚",
}


def connect():
    conn = sqlite3.connect(DB_PATH)
    # 打开 row_factory 后能按列名取值，不用去数第几列
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(connect()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS releases (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                app        TEXT NOT NULL,
                version    TEXT NOT NULL,
                git_sha    TEXT,
                status     TEXT NOT NULL,
                operator   TEXT,
                started_at TEXT,
                finished_at TEXT,
                note       TEXT
            )
        """)
        conn.commit()


def record(app, version, git_sha, status, operator, started_at, finished_at, note=""):
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO releases"
            " (app, version, git_sha, status, operator, started_at, finished_at, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (app, version, git_sha, status, operator, started_at, finished_at, note),
        )
        conn.commit()


def history(app, limit=10):
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT * FROM releases WHERE app = ? ORDER BY id DESC LIMIT ?",
            (app, limit),
        ).fetchall()
