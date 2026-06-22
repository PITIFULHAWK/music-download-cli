"""SQLite DB for cross-session download history. No src/ files touched."""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path.home() / ".local" / "share" / "AppleMusicDecrypt" / "downloads.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS downloads (
            song_id     TEXT PRIMARY KEY,
            url         TEXT NOT NULL,
            codec       TEXT NOT NULL,
            status      TEXT NOT NULL CHECK(status IN ('done','failed','skipped')),
            file_path   TEXT,
            title       TEXT,
            artist      TEXT,
            error_msg   TEXT,
            retry_count INTEGER DEFAULT 0,
            duration_ms INTEGER,
            downloaded_at TIMESTAMP
        );
    """)
    # Migration: add duration_ms if missing on existing DBs
    try:
        conn.execute("ALTER TABLE downloads ADD COLUMN duration_ms INTEGER")
    except Exception:
        pass
    conn.commit()
    conn.close()


def get_download(song_id: str, codec: str) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM downloads WHERE song_id = ? AND codec = ?",
        (song_id, codec),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def is_done_and_exists(song_id: str, codec: str) -> bool:
    dl = get_download(song_id, codec)
    if dl and dl["status"] == "done" and dl["file_path"]:
        p = Path(dl["file_path"])
        if p.exists():
            return True
        # DB says .flac but file might still be .m4a — check fallback
        if p.suffix == ".flac":
            return p.with_suffix(".m4a").exists()
    return False


def verify_file_duration(file_path: str, expected_duration_ms: int, tolerance_sec: int = 3) -> bool:
    """Returns True if the file exists and its audio duration matches expected within tolerance."""
    try:
        from mutagen import File
        f = File(file_path)
        if f is None:
            return False
        actual_sec = f.info.length
        expected_sec = expected_duration_ms / 1000
        return abs(actual_sec - expected_sec) <= tolerance_sec
    except Exception:
        return False


def upsert_download(
    song_id: str,
    url: str,
    codec: str,
    status: str,
    file_path: str | None = None,
    title: str | None = None,
    artist: str | None = None,
    error_msg: str | None = None,
    duration_ms: int | None = None,
):
    conn = get_conn()
    existing = conn.execute(
        "SELECT retry_count FROM downloads WHERE song_id = ?", (song_id,)
    ).fetchone()

    if status == "done":
        retry_count = 0
    elif status == "failed":
        retry_count = (existing["retry_count"] + 1) if existing else 1
    else:
        retry_count = existing["retry_count"] if existing else 0

    conn.execute(
        """INSERT INTO downloads
               (song_id, url, codec, status, file_path, title, artist, error_msg, retry_count, duration_ms, downloaded_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(song_id) DO UPDATE SET
               codec         = excluded.codec,
               status        = excluded.status,
               file_path     = excluded.file_path,
               title         = excluded.title,
               artist        = excluded.artist,
               error_msg     = excluded.error_msg,
               retry_count   = excluded.retry_count,
               duration_ms   = excluded.duration_ms,
               downloaded_at = excluded.downloaded_at""",
        (
            song_id,
            url,
            codec,
            status,
            file_path,
            title,
            artist,
            error_msg,
            retry_count,
            duration_ms,
            int(time.time()) if status == "done" else None,
        ),
    )
    conn.commit()
    conn.close()


def get_failed_downloads(codec: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM downloads WHERE status = 'failed' AND codec = ?",
        (codec,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
