"""approval_request 테이블 CRUD.

상태 머신(전이 규칙·audit 기록)은 app/approval/state_machine.py가 담당하고,
이 모듈은 SQL만 본다.
"""

from __future__ import annotations

import sqlite3

from app.approval.models import ApprovalRequest, ApprovalStatus


class ApprovalRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert(
        self,
        *,
        run_id: str,
        site: str,
        patch_json: str,
        dry_run_json: str | None,
        created_at: str,
        expires_at: str,
    ) -> int:
        """pending row 생성 후 id 반환."""
        cur = self.conn.execute(
            """
            INSERT INTO approval_request
              (run_id, site, status, patch_json, dry_run_json, created_at, expires_at)
            VALUES (?, ?, 'pending', ?, ?, ?, ?)
            """,
            (run_id, site, patch_json, dry_run_json, created_at, expires_at),
        )
        return int(cur.lastrowid)

    def get(self, id: int) -> ApprovalRequest | None:
        row = self.conn.execute(
            "SELECT * FROM approval_request WHERE id=?", (id,)
        ).fetchone()
        return _row_to_model(row) if row else None

    def list_pending(self, *, site: str | None = None) -> list[ApprovalRequest]:
        if site:
            rows = self.conn.execute(
                "SELECT * FROM approval_request WHERE status='pending' AND site=? "
                "ORDER BY created_at ASC",
                (site,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM approval_request WHERE status='pending' "
                "ORDER BY created_at ASC"
            ).fetchall()
        return [_row_to_model(r) for r in rows]

    def update_status(
        self,
        id: int,
        *,
        new_status: ApprovalStatus,
        decided_at: str,
        decided_by: str | None,
        decision_reason: str | None,
    ) -> int:
        """pending → 결정 상태로 1회만 전이. WHERE status='pending' 조건으로 race 보호."""
        cur = self.conn.execute(
            """
            UPDATE approval_request
            SET status=?, decided_at=?, decided_by=?, decision_reason=?
            WHERE id=? AND status='pending'
            """,
            (new_status, decided_at, decided_by, decision_reason, id),
        )
        return cur.rowcount

    def expire_pending_until(self, *, now_iso: str) -> list[int]:
        """expires_at <= now 인 pending들을 expired로 일괄 전이. 변경된 row id 목록 반환."""
        rows = self.conn.execute(
            "SELECT id FROM approval_request WHERE status='pending' AND expires_at <= ?",
            (now_iso,),
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(
            f"""
            UPDATE approval_request
            SET status='expired', decided_at=?, decided_by='system:expiry'
            WHERE id IN ({placeholders})
            """,
            (now_iso, *ids),
        )
        return ids

    def attach_slack(self, id: int, *, channel: str, thread_ts: str) -> None:
        """M6.5에서 슬랙 게시 후 thread_ts를 매핑."""
        self.conn.execute(
            "UPDATE approval_request SET slack_channel=?, slack_thread_ts=? WHERE id=?",
            (channel, thread_ts, id),
        )

    def list_recent_rejected(self, *, site: str, since_iso: str) -> list[ApprovalRequest]:
        """site에 대해 since_iso 이후 rejected된 approval들 (M6.6 D-1 가드용)."""
        rows = self.conn.execute(
            """
            SELECT * FROM approval_request
            WHERE site=? AND status='rejected' AND decided_at >= ?
            ORDER BY decided_at DESC
            """,
            (site, since_iso),
        ).fetchall()
        return [_row_to_model(r) for r in rows]


def _row_to_model(row: sqlite3.Row) -> ApprovalRequest:
    return ApprovalRequest(
        id=int(row["id"]),
        run_id=row["run_id"],
        site=row["site"],
        status=row["status"],
        patch_json=row["patch_json"],
        dry_run_json=row["dry_run_json"],
        slack_thread_ts=row["slack_thread_ts"],
        slack_channel=row["slack_channel"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        decision_reason=row["decision_reason"],
    )
