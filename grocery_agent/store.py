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
CREATE TABLE IF NOT EXISTS read_watch (   -- a price quote she has not opened yet
    user TEXT PRIMARY KEY,
    proposal_turn INTEGER NOT NULL,
    platform TEXT NOT NULL,
    total REAL NOT NULL,
    media_id TEXT,               -- the voice note, so a reminder can resend it as-is
    started REAL NOT NULL,
    last_sent REAL NOT NULL,
    reminders INTEGER NOT NULL DEFAULT 0,
    alerted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sent_messages (
    id TEXT PRIMARY KEY,         -- WhatsApp message id
    user TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent',
    watched INTEGER NOT NULL DEFAULT 0,   -- part of the quote being waited on
    sent REAL NOT NULL
);
"""

# Receipts can arrive out of order; never move a message backwards.
_STATUS_RANK = {"failed": 0, "sent": 1, "delivered": 2, "read": 3, "played": 4}


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
        self.db.execute("DELETE FROM read_watch WHERE last_sent < ?", (cutoff,))
        self.db.execute("DELETE FROM sent_messages WHERE sent < ?", (cutoff,))
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

    # --- read receipts for price quotes -----------------------------------
    def watch_reply(self, user: str, pending: dict, message_ids: list[str], media_id: str | None,
                    now: float | None = None) -> None:
        """Start waiting for her to open the reply that asks her to confirm `pending`."""
        now = now or time.time()
        self.db.execute(
            "INSERT OR REPLACE INTO read_watch (user, proposal_turn, platform, total, media_id, "
            "started, last_sent) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user, pending["turn"], pending["platform"], pending["total"], media_id, now, now))
        self._watch_messages(user, message_ids, now)
        self.db.commit()

    def _watch_messages(self, user: str, message_ids: list[str], now: float) -> None:
        # A receipt may already have arrived for these ids, so keep its status.
        self.db.executemany(
            "INSERT INTO sent_messages (id, user, watched, sent) VALUES (?, ?, 1, ?) "
            "ON CONFLICT(id) DO UPDATE SET watched = 1", [(m, user, now) for m in message_ids])

    def clear_watch(self, user: str) -> None:
        self.db.execute("DELETE FROM read_watch WHERE user = ?", (user,))
        self.db.execute("DELETE FROM sent_messages WHERE user = ?", (user,))
        self.db.commit()

    def record_status(self, message_id: str, status: str, recipient: str | None,
                      now: float | None = None) -> None:
        rank = _STATUS_RANK.get(status)
        if rank is None:
            return
        row = self.db.execute("SELECT status FROM sent_messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            if recipient:
                self.db.execute("INSERT INTO sent_messages (id, user, status, sent) VALUES (?, ?, ?, ?)",
                                (message_id, recipient, status, now or time.time()))
        elif (rank > _STATUS_RANK.get(row["status"], 0)
              or (status == "failed" and row["status"] in ("sent", "delivered"))):
            self.db.execute("UPDATE sent_messages SET status = ? WHERE id = ?", (status, message_id))
        self.db.commit()

    def record_reminder(self, user: str, message_id: str | None, now: float | None = None) -> None:
        now = now or time.time()
        self.db.execute("UPDATE read_watch SET reminders = reminders + 1, last_sent = ? WHERE user = ?",
                        (now, user))
        if message_id:
            self._watch_messages(user, [message_id], now)
        self.db.commit()

    def mark_alerted(self, user: str) -> None:
        self.db.execute("UPDATE read_watch SET alerted = 1 WHERE user = ?", (user,))
        self.db.commit()

    def open_watches(self, user: str | None = None) -> list[dict]:
        """Watches not yet escalated, with whether she has opened any of the messages."""
        rows = self.db.execute(
            "SELECT w.*, "
            " COALESCE(MAX(m.status IN ('read', 'played')), 0) AS seen, "
            " COUNT(m.id) > 0 AND COALESCE(MIN(m.status = 'failed'), 0) AS all_failed "
            "FROM read_watch w LEFT JOIN sent_messages m ON m.user = w.user AND m.watched = 1 "
            "WHERE w.alerted = 0 AND (? IS NULL OR w.user = ?) GROUP BY w.user", (user, user))
        return [dict(r) for r in rows]
