"""SQLite storage: conversation history, pending order proposals, placed orders, preferences."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    user TEXT PRIMARY KEY,
    messages TEXT NOT NULL,      -- JSON list, append-only within a session
    turn INTEGER NOT NULL,
    updated REAL NOT NULL,
    pending TEXT                 -- JSON proposal awaiting a yes, or NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL,
    platform TEXT NOT NULL,
    total REAL NOT NULL,
    items TEXT NOT NULL,
    result TEXT,
    status TEXT NOT NULL DEFAULT 'placed',   -- 'paid' (wallet) or 'awaiting_payment' (UPI link)
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    user TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user, key)
);
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(orders)")}
        if "status" not in cols:  # databases created before payment status existed
            self.db.execute("ALTER TABLE orders ADD COLUMN status TEXT NOT NULL DEFAULT 'placed'")

    # --- sessions -------------------------------------------------------
    def load_session(self, user: str, idle_hours: float, max_turns: int) -> dict:
        row = self.db.execute("SELECT * FROM sessions WHERE user = ?", (user,)).fetchone()
        fresh = {"messages": [], "turn": 0, "pending": None}
        if row is None:
            return fresh
        idle = time.time() - row["updated"] > idle_hours * 3600
        if idle or row["turn"] >= max_turns:
            # Start a new conversation instead of trimming the old one: history is
            # only ever appended to, never edited.
            return fresh
        return {
            "messages": json.loads(row["messages"]),
            "turn": row["turn"],
            "pending": json.loads(row["pending"]) if row["pending"] else None,
        }

    def save_session(self, user: str, session: dict) -> None:
        self.db.execute(
            "INSERT INTO sessions (user, messages, turn, updated, pending) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user) DO UPDATE SET messages=excluded.messages, turn=excluded.turn, "
            "updated=excluded.updated, pending=excluded.pending",
            (user, json.dumps(session["messages"], ensure_ascii=False), session["turn"],
             time.time(), json.dumps(session["pending"]) if session["pending"] else None),
        )
        self.db.commit()

    # --- orders ---------------------------------------------------------
    def record_order(self, user: str, platform: str, total: float, items: list, result: str,
                     status: str = "placed") -> None:
        self.db.execute(
            "INSERT INTO orders (user, platform, total, items, result, status, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user, platform, total, json.dumps(items, ensure_ascii=False), result, status, time.time()),
        )
        self.db.commit()

    def recent_products(self, user: str, limit: int = 15) -> list[str]:
        """Distinct products from this person's recent orders, newest first."""
        rows = self.db.execute(
            "SELECT items FROM orders WHERE user = ? ORDER BY created DESC LIMIT 30", (user,))
        seen: list[str] = []
        for row in rows:
            for item in json.loads(row["items"]):
                name = " ".join(str(item.get(k, "")) for k in ("product", "pack")).strip()
                if name and name not in seen:
                    seen.append(name)
        return seen[:limit]

    def purge_older_than(self, days: int) -> None:
        """Data minimisation: drop old conversations and order records."""
        cutoff = time.time() - days * 86400
        self.db.execute("DELETE FROM sessions WHERE updated < ?", (cutoff,))
        self.db.execute("DELETE FROM orders WHERE created < ?", (cutoff,))
        self.db.commit()

    def spent_since(self, seconds: float) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(total), 0) AS s FROM orders WHERE created > ?",
            (time.time() - seconds,),
        ).fetchone()
        return float(row["s"])

    # --- preferences ("usual milk" = Amul Taaza 1 L) ----------------------
    def set_preference(self, user: str, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO preferences (user, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user, key) DO UPDATE SET value=excluded.value",
            (user, key.strip().lower(), value.strip()),
        )
        self.db.commit()

    def preferences(self, user: str) -> dict[str, str]:
        rows = self.db.execute("SELECT key, value FROM preferences WHERE user = ?", (user,))
        return {r["key"]: r["value"] for r in rows}
