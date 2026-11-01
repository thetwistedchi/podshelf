import os
import sqlite3
import threading
from contextlib import contextmanager

from . import config

_local = threading.local()
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS podcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_url TEXT UNIQUE NOT NULL,
    title TEXT,
    author TEXT,
    description TEXT,
    image_url TEXT,
    folder TEXT,
    keep_count INTEGER NOT NULL DEFAULT 10,      -- 0 = keep all
    check_hours INTEGER NOT NULL DEFAULT 6,
    initial_count INTEGER NOT NULL DEFAULT 10,
    auto_download INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    last_checked TEXT,
    last_error TEXT,
    initialized INTEGER NOT NULL DEFAULT 0,
    max_new INTEGER NOT NULL DEFAULT 3,
    cover_saved TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    podcast_id INTEGER NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
    guid TEXT NOT NULL,
    title TEXT,
    description TEXT,
    pub_date TEXT,                -- ISO 8601 UTC
    enclosure_url TEXT,
    enclosure_type TEXT,
    image_url TEXT,
    duration TEXT,
    status TEXT NOT NULL DEFAULT 'new',  -- new|queued|downloading|done|failed|deleted|skipped
    file_path TEXT,
    file_size INTEGER,
    pinned INTEGER NOT NULL DEFAULT 0,  -- manual downloads survive retention
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try TEXT,
    error TEXT,
    downloaded_at TEXT,
    UNIQUE(podcast_id, guid)
);
CREATE INDEX IF NOT EXISTS idx_ep_status ON episodes(status);
CREATE INDEX IF NOT EXISTS idx_ep_podcast ON episodes(podcast_id, pub_date);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        _local.conn = c
    return c


@contextmanager
def tx():
    """Serialized write transaction."""
    with _write_lock:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def query(sql, params=()):
    return conn().execute(sql, params).fetchall()


def one(sql, params=()):
    return conn().execute(sql, params).fetchone()


def init():
    with tx() as c:
        c.executescript(SCHEMA)
        for k, v in config.DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        # anything stuck mid-download after a restart goes back to the queue
        c.execute("UPDATE episodes SET status='queued' WHERE status='downloading'")


def get_setting(key):
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else config.DEFAULT_SETTINGS.get(key)


def set_setting(key, value):
    with tx() as c:
        c.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
