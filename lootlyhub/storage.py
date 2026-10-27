"""Локальное состояние в SQLite: какие заказы обработаны, зарезервированные ключи, настройки."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id            TEXT PRIMARY KEY,
    product_id    TEXT NOT NULL,
    product_title TEXT NOT NULL,
    state         TEXT NOT NULL,          -- new | reserved | delivered | manual | cancelled | failed
    goods         TEXT,                   -- файл goods/, из которого зарезервированы значения
    reserved      TEXT,                   -- JSON-список зарезервированных значений
    first_seen    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id       TEXT PRIMARY KEY,
    order_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_order ON messages(order_id);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: str | Path = "storage/lootlyhub.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)  # autocommit; транзакции — явно
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ kv

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))

    # -------------------------------------------------------------- orders

    def order(self, order_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    def add_order(self, order_id: str, product_id: str, product_title: str) -> bool:
        """Запоминает заказ. False — если он уже был."""
        now = time.time()
        cur = self.db.execute(
            "INSERT OR IGNORE INTO orders(id, product_id, product_title, state, first_seen, updated_at) "
            "VALUES(?, ?, ?, 'new', ?, ?)", (order_id, product_id, product_title, now, now))
        return cur.rowcount == 1

    def set_order_state(self, order_id: str, state: str, *, goods: str | None = None,
                        reserved: list[str] | None = None, clear_reserved: bool = False) -> None:
        sets, args = ["state = ?", "updated_at = ?"], [state, time.time()]
        if goods is not None:
            sets.append("goods = ?")
            args.append(goods)
        if reserved is not None:
            sets.append("reserved = ?")
            args.append(json.dumps(reserved, ensure_ascii=False))
        elif clear_reserved:
            sets.append("reserved = NULL")
        self.db.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id = ?", (*args, order_id))

    def reserved_orders(self) -> list[sqlite3.Row]:
        """Заказы, под которые взяли значения, но выдача не подтверждена (например, упали посреди)."""
        return self.db.execute("SELECT * FROM orders WHERE state = 'reserved'").fetchall()

    @staticmethod
    def reserved_values(row: sqlite3.Row) -> list[str]:
        return json.loads(row["reserved"]) if row["reserved"] else []

    def stats(self) -> dict[str, int]:
        rows = self.db.execute("SELECT state, COUNT(*) AS n FROM orders GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    # ------------------------------------------------------------ messages

    def is_message_seen(self, message_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM messages WHERE id = ?", (message_id,)).fetchone() is not None

    def mark_messages_seen(self, order_id: str, message_ids: list[str]) -> None:
        self.db.executemany("INSERT OR IGNORE INTO messages(id, order_id) VALUES(?, ?)",
                            [(m, order_id) for m in message_ids])

    def has_messages(self, order_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM messages WHERE order_id = ? LIMIT 1",
                               (order_id,)).fetchone() is not None
