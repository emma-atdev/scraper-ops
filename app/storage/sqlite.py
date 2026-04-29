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
        status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'superseded')),
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

    _migrate_approval_request_check(conn)


def _migrate_approval_request_check(conn: sqlite3.Connection) -> None:
    """기존 approval_request 테이블의 CHECK constraint에 'superseded'가 빠져 있으면 재생성.

    SQLite는 CHECK constraint를 ALTER로 변경할 수 없으므로 표준 패턴(테이블 복사)을 쓴다.
    M6.6b 도입 전 운영 DB에 4종 status로 데이터가 있는 경우에만 실행.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='approval_request'"
    ).fetchone()
    if row is None:
        return
    sql = row["sql"] or ""
    if "'superseded'" in sql:
        return  # 이미 마이그레이션됨

    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        cur.execute("ALTER TABLE approval_request RENAME TO approval_request_old")
        # 새 테이블 생성 (위 SCHEMA의 approval_request 정의와 일치)
        cur.execute("""
            CREATE TABLE approval_request (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                site TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'superseded')),
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
        """)
        cur.execute("""
            INSERT INTO approval_request
              (id, run_id, site, status, patch_json, dry_run_json,
               slack_thread_ts, slack_channel, created_at, expires_at,
               decided_at, decided_by, decision_reason)
            SELECT id, run_id, site, status, patch_json, dry_run_json,
                   slack_thread_ts, slack_channel, created_at, expires_at,
                   decided_at, decided_by, decision_reason
            FROM approval_request_old
        """)
        cur.execute("DROP TABLE approval_request_old")
        # 인덱스 재생성 (DROP TABLE이 인덱스도 같이 날림)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_approval_status "
            "ON approval_request(status, expires_at)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_approval_run "
            "ON approval_request(run_id)"
        )
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
