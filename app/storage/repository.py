"""runs / job_postings 영속화 로직."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from app.models import JobPosting, UpsertStats


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_utc_iso(dt: datetime) -> str:
    """timezone-aware datetime을 UTC ISO 문자열로. naive면 UTC로 간주."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


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

    def summarize_window(self, since: datetime, until: datetime) -> dict:
        """[since, until) 윈도우 내 started_at 기준 run을 site별로 집계.

        DB의 started_at은 UTC ISO 문자열이므로 입력 datetime이 timezone-aware라면
        UTC로 변환해 비교한다. naive면 UTC로 간주한다.

        반환 형식:
        {
            "since": iso8601, "until": iso8601,
            "by_site": {
                "catch": {
                    "runs": 9, "success": 9, "failed": 0,
                    "inserted": 73, "updated": 4, "unchanged": 21000,
                    "last_status": "success", "last_finished_at": "..."
                }
            }
        }
        """
        since_iso = _to_utc_iso(since)
        until_iso = _to_utc_iso(until)

        rows = self.conn.execute(
            """
            SELECT site,
                   COUNT(*) AS runs,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                   SUM(CASE WHEN status='failed'  THEN 1 ELSE 0 END) AS failed,
                   SUM(COALESCE(inserted, 0))  AS inserted,
                   SUM(COALESCE(updated, 0))   AS updated,
                   SUM(COALESCE(unchanged, 0)) AS unchanged
            FROM runs
            WHERE started_at >= ? AND started_at < ?
            GROUP BY site
            """,
            (since_iso, until_iso),
        ).fetchall()

        by_site: dict[str, dict] = {}
        for r in rows:
            site = r["site"]
            last = self.conn.execute(
                """
                SELECT status, finished_at FROM runs
                WHERE site=? AND started_at >= ? AND started_at < ?
                ORDER BY started_at DESC LIMIT 1
                """,
                (site, since_iso, until_iso),
            ).fetchone()
            by_site[site] = {
                "runs": int(r["runs"] or 0),
                "success": int(r["success"] or 0),
                "failed": int(r["failed"] or 0),
                "inserted": int(r["inserted"] or 0),
                "updated": int(r["updated"] or 0),
                "unchanged": int(r["unchanged"] or 0),
                "last_status": last["status"] if last else None,
                "last_finished_at": last["finished_at"] if last else None,
            }

        return {"since": since_iso, "until": until_iso, "by_site": by_site}
