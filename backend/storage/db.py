"""SQLite persistence (stdlib only). Schema is designed for later subscription support:

  accounts (parent)  1──*  devices  1──*  conversations
       │                       │
       └── subscriptions       └── usage_events (device_id + account_id kept separately,
                                   so device-level and user-level usage can be aggregated independently)
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    plan TEXT NOT NULL DEFAULT 'free',
    status TEXT NOT NULL DEFAULT 'active',          -- active | past_due | cancelled | trial
    monthly_minutes_quota REAL NOT NULL DEFAULT 60,
    monthly_llm_token_quota INTEGER NOT NULL DEFAULT 200000,
    period_start REAL NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    firmware_version TEXT DEFAULT 'unknown',
    hardware TEXT DEFAULT 'unknown',
    mac TEXT,
    is_virtual INTEGER NOT NULL DEFAULT 0,
    personality_id TEXT NOT NULL DEFAULT 'teddy',
    account_id TEXT REFERENCES accounts(id) ON DELETE SET NULL,
    -- parental controls
    daily_minutes_limit REAL NOT NULL DEFAULT 30,
    daily_turns_limit INTEGER NOT NULL DEFAULT 100,
    quiet_hours_start TEXT,    -- 'HH:MM' or NULL
    quiet_hours_end TEXT,
    store_transcripts INTEGER NOT NULL DEFAULT 1,
    -- child profile (parent-entered) + what the toy learned (JSON); see services/child_profile.py
    child_name TEXT,
    child_age INTEGER,
    child_interests TEXT,
    child_memory TEXT DEFAULT '{}',
    created_at REAL NOT NULL,
    last_seen REAL,
    last_heartbeat REAL,
    last_rssi INTEGER,
    last_ip TEXT,
    meta TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    personality_id TEXT NOT NULL,
    user_text TEXT NOT NULL,
    reply_text TEXT NOT NULL,
    safety_flags TEXT DEFAULT '[]',
    audio_seconds REAL DEFAULT 0,
    latency_ms TEXT DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_device ON conversations(device_id, created_at);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    account_id TEXT,
    kind TEXT NOT NULL,      -- conversation_seconds | stt_seconds | llm_input_tokens | llm_output_tokens | tts_chars | turn
    amount REAL NOT NULL,
    provider TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_device ON usage_events(device_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_account ON usage_events(account_id, created_at);
"""


class Database:
    _ADDED_COLUMNS = {"devices": [("child_name", "TEXT"), ("child_age", "INTEGER"),
                                  ("child_interests", "TEXT"), ("child_memory", "TEXT DEFAULT '{}'")]}

    def _migrate(self):
        """Add columns introduced after a database was created (SQLite has no ADD COLUMN IF NOT EXISTS)."""
        for table, cols in self._ADDED_COLUMNS.items():
            have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                    log.info("migrated: %s.%s added", table, name)

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()
        log.info("SQLite ready at %s", self.path)

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def now() -> float:
    return time.time()


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))
