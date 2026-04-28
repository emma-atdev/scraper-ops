"""SQLite 초기화. WAL + busy_timeout 적용."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS runs (
        run_id TEXT PRIMARY KEY,
        site TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT,
        inserted INTEGER DEFAULT 0,
        updated INTEGER DEFAULT 0,
        unchanged INTEGER DEFAULT 0,
        notes TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_postings (
        site TEXT NOT NULL,
        external_id TEXT NOT NULL,
        title TEXT,
        company TEXT,
        deadline TEXT,
        link TEXT,
        raw_json TEXT,
        content_hash TEXT,
        first_seen_run TEXT,
        last_seen_run TEXT,
        created_at TEXT,
        updated_at TEXT,
        PRIMARY KEY (site, external_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON job_postings(site, last_seen_run)",
    """
    CREATE TABLE IF NOT EXISTS approval_request (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        site TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
        patch_json TEXT NOT NULL,
        dry_run_json TEXT,
        slack_thread_ts TEXT,
        slack_channel TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT,
        decided_by TEXT,
        decision_reason TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_approval_status ON approval_request(status, expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_approval_run ON approval_request(run_id)",
]


def open_connection(path: str | Path) -> sqlite3.Connection:
    """SQLite 연결을 열고 WAL + busy_timeout을 설정한다."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit, transaction은 명시적
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        for stmt in SCHEMA:
            cur.execute(stmt)
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
