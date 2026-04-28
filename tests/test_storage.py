"""SQLite storage 테스트. tmp_path에 임시 DB 사용."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def test_summarize_window_aggregates_runs(tmp_path):
    repo = _setup(tmp_path)
    repo.start_run("catch", "run-1")
    repo.upsert_postings(
        "catch",
        "run-1",
        [JobPosting(external_id="1", site="catch", title="t", company="c", deadline="d")],
    )
    from app.models import UpsertStats

    repo.finish_run("run-1", status="success", stats=UpsertStats(inserted=1))

    repo.start_run("catch", "run-2")
    repo.finish_run("run-2", status="failed")

    # [지금-1일, 지금+1분) 으로 윈도우 잡으면 둘 다 들어와야 한다
    until = datetime.now(timezone.utc) + timedelta(minutes=1)
    since = until - timedelta(days=1)
    summary = repo.summarize_window(since, until)
    assert "catch" in summary["by_site"]
    s = summary["by_site"]["catch"]
    assert s["runs"] == 2
    assert s["failed"] == 1
    assert s["success"] == 1
    assert s["inserted"] == 1


def test_summarize_window_excludes_runs_outside(tmp_path):
    """until exclusive 동작 + 윈도우 밖 run은 빠진다."""
    repo = _setup(tmp_path)
    repo.start_run("catch", "run-1")
    repo.finish_run("run-1", status="success")

    # 1년 전~6개월 전 윈도우 — 방금 만든 run은 포함되면 안 됨
    long_ago_until = datetime.now(timezone.utc) - timedelta(days=180)
    long_ago_since = long_ago_until - timedelta(days=180)
    summary = repo.summarize_window(long_ago_since, long_ago_until)
    assert summary["by_site"] == {}
