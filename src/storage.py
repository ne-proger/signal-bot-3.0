from __future__ import annotations
import sqlite3
from pathlib import Path
from typing import Optional, Tuple, List

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "bot.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    pairs TEXT NOT NULL DEFAULT 'BTCUSDT',
    frequency_seconds INTEGER NOT NULL DEFAULT 3600,
    sensitivity TEXT NOT NULL DEFAULT 'medium'
);
"""

class Storage:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._conn() as con:
            con.execute(SCHEMA)
            # миграция: добавить колонку category, если её нет
            cols = {r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()}
            if "category" not in cols:
                try:
                    con.execute("ALTER TABLE users ADD COLUMN category TEXT NOT NULL DEFAULT 'spot'")
                except Exception:
                    pass

    def upsert_user(
        self,
        user_id: int,
        pairs: Optional[str] = None,
        frequency_seconds: Optional[int] = None,
        sensitivity: Optional[str] = None,
        category: Optional[str] = None,
    ) -> None:
        with self._conn() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO users(user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
                (user_id,),
            )
            if pairs is not None:
                cur.execute("UPDATE users SET pairs=? WHERE user_id=?", (pairs, user_id))
            if frequency_seconds is not None:
                cur.execute("UPDATE users SET frequency_seconds=? WHERE user_id=?", (frequency_seconds, user_id))
            if sensitivity is not None:
                cur.execute("UPDATE users SET sensitivity=? WHERE user_id=?", (sensitivity, user_id))
            if category is not None:
                cur.execute("UPDATE users SET category=? WHERE user_id=?", (category, user_id))

    def get_user(self, user_id: int) -> Optional[Tuple[int, str, int, str, str]]:
        with self._conn() as con:
            row = con.execute(
                "SELECT user_id, pairs, frequency_seconds, sensitivity, category FROM users WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return row

    def all_users(self) -> List[Tuple[int, str, int, str, str]]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT user_id, pairs, frequency_seconds, sensitivity, category FROM users"
            ).fetchall()
        return rows
