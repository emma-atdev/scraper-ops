"""runs / job_postings 영속화 로직."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from app.models import JobPosting, UpsertStats


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def start_run(self, site: str, run_id: str) -> None:
        self.conn.execute(
            "INSERT INTO runs (run_id, site, started_at, status) VALUES (?, ?, ?, ?)",
            (run_id, site, _now(), "running"),
        )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        stats: UpsertStats | None = None,
        notes: str | None = None,
    ) -> None:
        s = stats or UpsertStats()
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, inserted=?, updated=?, unchanged=?, notes=? WHERE run_id=?",
            (_now(), status, s.inserted, s.updated, s.unchanged, notes, run_id),
        )

    def upsert_postings(
        self,
        site: str,
        run_id: str,
        postings: Iterable[JobPosting],
    ) -> UpsertStats:
        stats = UpsertStats()
        cur = self.conn.cursor()
        cur.execute("BEGIN")
        try:
            for p in postings:
                row = cur.execute(
                    "SELECT content_hash FROM job_postings WHERE site=? AND external_id=?",
                    (site, p.external_id),
                ).fetchone()
                new_hash = p.content_hash()
                now = _now()
                if row is None:
                    cur.execute(
                        """
                        INSERT INTO job_postings
                            (site, external_id, title, company, deadline, link, raw_json,
                             content_hash, first_seen_run, last_seen_run, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            site,
                            p.external_id,
                            p.title,
                            p.company,
                            p.deadline,
                            p.link,
                            p.raw_json(),
                            new_hash,
                            run_id,
                            run_id,
                            now,
                            now,
                        ),
                    )
                    stats.inserted += 1
                elif row["content_hash"] != new_hash:
                    cur.execute(
                        """
                        UPDATE job_postings
                        SET title=?, company=?, deadline=?, link=?, raw_json=?,
                            content_hash=?, last_seen_run=?, updated_at=?
                        WHERE site=? AND external_id=?
                        """,
                        (
                            p.title,
                            p.company,
                            p.deadline,
                            p.link,
                            p.raw_json(),
                            new_hash,
                            run_id,
                            now,
                            site,
                            p.external_id,
                        ),
                    )
                    stats.updated += 1
                else:
                    cur.execute(
                        "UPDATE job_postings SET last_seen_run=? WHERE site=? AND external_id=?",
                        (run_id, site, p.external_id),
                    )
                    stats.unchanged += 1
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return stats

    def previous_count(self, site: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM job_postings WHERE site=?",
            (site,),
        ).fetchone()
        return int(row["c"]) if row else 0
