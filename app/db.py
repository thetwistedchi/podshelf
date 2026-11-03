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
CREATE TABLE IF NOT EXISTS podcast_tags (
    podcast_id INTEGER NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
    tag TEXT NOT NULL,
    PRIMARY KEY (podcast_id, tag)
);
"""

# columns added after 1.0; added to existing databases on start
MIGRATIONS = {
    "podcasts": {
        "title_override": "TEXT",
        "keep_days": "INTEGER NOT NULL DEFAULT 0",
        "include_filter": "TEXT NOT NULL DEFAULT ''",
        "exclude_filter": "TEXT NOT NULL DEFAULT ''",
        "min_minutes": "INTEGER NOT NULL DEFAULT 0",
        "max_minutes": "INTEGER NOT NULL DEFAULT 0",
        "trim_start": "INTEGER NOT NULL DEFAULT 0",
        "trim_end": "INTEGER NOT NULL DEFAULT 0",
        "cut_ad_chapters": "INTEGER NOT NULL DEFAULT 0",
        "auth_user": "TEXT NOT NULL DEFAULT ''",
        "auth_pass": "TEXT NOT NULL DEFAULT ''",
        "split_mode": "TEXT NOT NULL DEFAULT 'none'",   # none|season|bonus|both
        "delete_played_days": "INTEGER NOT NULL DEFAULT -1",  # -1 = never
    },
    "episodes": {
        "played": "INTEGER NOT NULL DEFAULT 0",
        "played_at": "TEXT",
        "season": "INTEGER",
        "episode_type": "TEXT",
        "chapters": "TEXT",          # JSON [{"start": sec, "title": str}] as published in the feed
        "file_chapters": "TEXT",     # chapters as they are in the file (after ad cuts)
        "chapters_url": "TEXT",
        "title_override": "TEXT",
        "number_override": "INTEGER",
        "skip_reason": "TEXT",
        "length_secs": "INTEGER",    # real length after download
        "cut_secs": "INTEGER",       # seconds removed by ad skipping
    },
}


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
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for name, ddl in cols.items():
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ep_played ON episodes(podcast_id, played)")
        for k, v in config.DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        # anything stuck mid-download after a restart goes back to the queue
        c.execute("UPDATE episodes SET status='queued' WHERE status='downloading'")


def backup_to(path: str):
    """Consistent copy of the live database (safe while the app runs)."""
    dest = sqlite3.connect(path)
    try:
        with _write_lock:
            conn().backup(dest)
    finally:
        dest.close()


def restore_from(path: str):
    """Replace the live database contents with a backup file."""
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"podcasts", "episodes", "settings"} <= names:
            raise ValueError("not a Podshelf backup")
        with _write_lock:
            src.backup(conn())
    finally:
        src.close()
    init()  # apply migrations to older backups


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
