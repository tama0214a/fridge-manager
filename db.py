"""SQLite データベースの初期化・接続ヘルパー。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = DATA_DIR / "backups"
DB_PATH = DATA_DIR / "fridge.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fridges (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    temp     TEXT NOT NULL DEFAULT '',
    active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS researchers (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL,
    email  TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    quantity    TEXT NOT NULL DEFAULT '',
    position    TEXT NOT NULL DEFAULT '',
    owner_id    INTEGER NOT NULL REFERENCES researchers(id),
    fridge_id   INTEGER NOT NULL REFERENCES fridges(id),
    stored_date TEXT NOT NULL,
    expiry_date TEXT,
    status      TEXT NOT NULL DEFAULT '保管中',
    out_date    TEXT,
    out_by_id   INTEGER REFERENCES researchers(id),
    out_note    TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_status_expiry ON items(status, expiry_date);

CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    action    TEXT NOT NULL,
    item_id   INTEGER,
    item_name TEXT NOT NULL DEFAULT '',
    actor     TEXT NOT NULL DEFAULT '',
    detail    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notify_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject   TEXT NOT NULL DEFAULT '',
    ok        INTEGER NOT NULL,
    error     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
