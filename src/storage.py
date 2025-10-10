# src/storage.py
from __future__ import annotations
import os
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

DB_PATH = os.path.join("data", "bot.db")


class Storage:
    """
    Простое хранилище на SQLite:
      - users: настройки пользователей
      - signals: журнал опубликованных сигналов (для антидубля/истории)
    """

    def __init__(self, path: str = DB_PATH) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        
    def _database_url() -> str:
        url = os.getenv("DATABASE_URL", "").strip()
        if url:
        # старые форматы → современные
            if url.startswith("postgres://"):
                url = url.replace("postgres://", "postgresql://", 1)
        # принудительно просим драйвер psycopg v3
            if url.startswith("postgresql://"):
                url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url
        os.makedirs("data", exist_ok=True)
        return "sqlite:///data/bot.db"
    

    # ---------- schema ----------
    def _init_schema(self) -> None:
        cur = self.conn.cursor()

        # Таблица пользователей (совместима с текущим кодом)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                pairs TEXT DEFAULT 'BTCUSDT,TRXUSDT,INJUSDT',
                frequency_seconds INTEGER DEFAULT 3600,
                sensitivity TEXT DEFAULT 'medium',
                category TEXT DEFAULT 'spot'
            );
            """
        )

        # Журнал сигналов
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,     -- 'buy' (на будущее можно 'sell')
                confidence REAL,
                entry REAL,
                take_profit REAL,
                stop_loss REAL,
                exit_horizon TEXT,
                created_at INTEGER NOT NULL,   -- epoch sec
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            """
        )

        # Индексы на сигналы
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_user ON signals(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(created_at);")

        self.conn.commit()

    # ---------- users API ----------
    def get_user(self, user_id: int) -> Optional[Tuple[int, str, int, str, str]]:
        cur = self.conn.cursor()
        cur.execute("SELECT user_id, pairs, frequency_seconds, sensitivity, category FROM users WHERE user_id=?;", (user_id,))
        row = cur.fetchone()
        if not row:
            return None
        return (row["user_id"], row["pairs"], row["frequency_seconds"], row["sensitivity"], row["category"])

    def upsert_user(
        self,
        user_id: int,
        *,
        pairs: Optional[str] = None,
        frequency_seconds: Optional[int] = None,
        sensitivity: Optional[str] = None,
        category: Optional[str] = None,
    ) -> None:
        cur = self.conn.cursor()
        # вставим, если нет
        cur.execute(
            """
            INSERT INTO users (user_id) VALUES (?)
            ON CONFLICT(user_id) DO NOTHING;
            """,
            (user_id,),
        )
        # обновим только переданные поля
        sets = []
        args: List[Any] = []
        if pairs is not None:
            sets.append("pairs=?")
            args.append(pairs)
        if frequency_seconds is not None:
            sets.append("frequency_seconds=?")
            args.append(int(frequency_seconds))
        if sensitivity is not None:
            sets.append("sensitivity=?")
            args.append(sensitivity)
        if category is not None:
            sets.append("category=?")
            args.append(category)
        if sets:
            args.append(user_id)
            cur.execute(f"UPDATE users SET {', '.join(sets)} WHERE user_id=?;", tuple(args))
        self.conn.commit()

    def all_users(self) -> Iterable[Tuple[int, str, int, str, str]]:
        cur = self.conn.cursor()
        cur.execute("SELECT user_id, pairs, frequency_seconds, sensitivity, category FROM users;")
        for row in cur.fetchall():
            yield (row["user_id"], row["pairs"], row["frequency_seconds"], row["sensitivity"], row["category"])

    # ---------- signals API ----------
    def log_signal(
        self,
        *,
        user_id: int,
        symbol: str,
        signal_type: str,
        confidence: Optional[float],
        entry: Optional[float],
        take_profit: Optional[float],
        stop_loss: Optional[float],
        exit_horizon: Optional[str],
        created_at: Optional[int] = None,
    ) -> None:
        ts = int(created_at or time.time())
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO signals
            (user_id, symbol, signal_type, confidence, entry, take_profit, stop_loss, exit_horizon, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (user_id, symbol, signal_type, confidence, entry, take_profit, stop_loss, exit_horizon, ts),
        )
        self.conn.commit()

    def last_signal(self, *, user_id: int, symbol: str) -> Optional[sqlite3.Row]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT * FROM signals
            WHERE user_id=? AND symbol=?
            ORDER BY created_at DESC
            LIMIT 1;
            """,
            (user_id, symbol),
        )
        return cur.fetchone()

    def recent_signals(self, *, user_id: int, limit: int = 20) -> List[sqlite3.Row]:
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT * FROM signals
            WHERE user_id=?
            ORDER BY created_at DESC
            LIMIT ?;
            """,
            (user_id, int(limit)),
        )
        return cur.fetchall()

    def is_duplicate_like(
        self,
        *,
        user_id: int,
        symbol: str,
        new_conf: Optional[float],
        new_entry: Optional[float],
        new_tp: Optional[float],
        new_sl: Optional[float],
        cooldown_hours: float = 6.0,
        tol_pct: float = 0.5,     # проценты допуска для entry/tp/sl
        conf_tol: float = 0.03    # допуск для confidence
    ) -> bool:
        """
        Возвращает True, если последний сигнал для symbol:
          - моложе cooldown_hours,
          - и значения "похожи" (в пределах допусков).
        """
        row = self.last_signal(user_id=user_id, symbol=symbol)
        if not row:
            return False

        now = time.time()
        if (now - float(row["created_at"])) > cooldown_hours * 3600.0:
            return False

        def close_enough(a: Optional[float], b: Optional[float]) -> bool:
            if a is None or b is None:
                return a is None and b is None
            if b == 0:
                return abs(a - b) < 1e-9
            return abs(a - b) / abs(b) <= (tol_pct / 100.0)

        if not close_enough(new_entry, row["entry"]):
            return False
        if not close_enough(new_tp, row["take_profit"]):
            return False
        if not close_enough(new_sl, row["stop_loss"]):
            return False

        # confidence сравним отдельным допуском
        try:
            last_conf = None if row["confidence"] is None else float(row["confidence"])
        except Exception:
            last_conf = None
        if new_conf is None or last_conf is None:
            return True  # если одно из них None — считаем «достаточно похоже» по ключевым ценам

        return abs(float(new_conf) - float(last_conf)) <= conf_tol
