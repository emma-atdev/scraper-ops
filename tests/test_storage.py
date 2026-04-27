"""SQLite storage 테스트. tmp_path에 임시 DB 사용."""

from __future__ import annotations

from app.models import JobPosting
from app.storage import Repository, init_schema, open_connection


def _setup(tmp_path):
    conn = open_connection(tmp_path / "test.db")
    init_schema(conn)
    return Repository(conn)


def test_upsert_inserts_and_marks_unchanged(tmp_path):
    repo = _setup(tmp_path)
    repo.start_run("catch", "run-1")
    p = JobPosting(external_id="1", site="catch", title="Backend", company="Acme", deadline="2026-12-31", link="https://x")
    stats = repo.upsert_postings("catch", "run-1", [p])
    assert (stats.inserted, stats.updated, stats.unchanged) == (1, 0, 0)

    repo.start_run("catch", "run-2")
    stats = repo.upsert_postings("catch", "run-2", [p])
    assert (stats.inserted, stats.updated, stats.unchanged) == (0, 0, 1)


def test_upsert_detects_change(tmp_path):
    repo = _setup(tmp_path)
    repo.start_run("catch", "run-1")
    p = JobPosting(external_id="1", site="catch", title="Backend", company="Acme", deadline="2026-12-31")
    repo.upsert_postings("catch", "run-1", [p])

    p2 = JobPosting(external_id="1", site="catch", title="Backend Senior", company="Acme", deadline="2026-12-31")
    repo.start_run("catch", "run-2")
    stats = repo.upsert_postings("catch", "run-2", [p2])
    assert (stats.inserted, stats.updated, stats.unchanged) == (0, 1, 0)


def test_finish_run_records_stats(tmp_path):
    repo = _setup(tmp_path)
    repo.start_run("catch", "run-1")
    p = JobPosting(external_id="1", site="catch", title="t", company="c", deadline="d")
    stats = repo.upsert_postings("catch", "run-1", [p])
    repo.finish_run("run-1", status="success", stats=stats)

    row = repo.conn.execute("SELECT * FROM runs WHERE run_id='run-1'").fetchone()
    assert row["status"] == "success"
    assert row["inserted"] == 1
    assert row["finished_at"] is not None


def test_pragma_wal_and_busy_timeout(tmp_path):
    conn = open_connection(tmp_path / "test.db")
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert journal_mode.lower() == "wal"
    assert busy_timeout == 5000
