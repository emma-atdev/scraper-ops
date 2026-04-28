"""approval_request 상태 머신.

전이 규칙:
- create        → status='pending' row 생성
- approve(id)   → pending → approved (1회만)
- reject(id)    → pending → rejected (1회만)
- expire_due()  → expires_at 지난 pending들을 expired로

이미 결정된 row(approved/rejected/expired)에 대한 재결정은 ApprovalAlreadyDecided로 거부.
모든 전이는 audit.log에 append.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.audit import audit_log
from app.storage.approval import ApprovalRepository

logger = logging.getLogger("scraper.approval")

DEFAULT_EXPIRY_HOURS = 24


class ApprovalAlreadyDecided(RuntimeError):
    """이미 결정 완료된 approval에 대한 재결정 시도."""


class ApprovalNotFound(LookupError):
    pass


def create_approval(
    repo: ApprovalRepository,
    *,
    run_id: str,
    site: str,
    patch_json: str,
    dry_run_json: str | None,
    expiry_hours: int = DEFAULT_EXPIRY_HOURS,
    audit_path: str | Path | None = None,
) -> int:
    """pending row를 생성하고 audit에 기록. 같은 run_id에 다른 pending이 있어도 차단하지 않는다 (A-1)."""
    now = datetime.now(timezone.utc)
    created_at = now.isoformat(timespec="seconds")
    expires_at = (now + timedelta(hours=expiry_hours)).isoformat(timespec="seconds")

    new_id = repo.insert(
        run_id=run_id,
        site=site,
        patch_json=patch_json,
        dry_run_json=dry_run_json,
        created_at=created_at,
        expires_at=expires_at,
    )
    audit_log(
        "approval_created",
        path=audit_path,
        id=new_id,
        run_id=run_id,
        site=site,
        expires_at=expires_at,
    )
    logger.info(
        "approval created",
        extra={"event": "approval_created", "id": new_id, "site": site, "run_id": run_id},
    )
    return new_id


def approve(
    repo: ApprovalRepository,
    id: int,
    *,
    by: str,
    reason: str | None = None,
    audit_path: str | Path | None = None,
) -> None:
    _decide(repo, id, new_status="approved", by=by, reason=reason, audit_path=audit_path)


def reject(
    repo: ApprovalRepository,
    id: int,
    *,
    by: str,
    reason: str,
    audit_path: str | Path | None = None,
) -> None:
    _decide(repo, id, new_status="rejected", by=by, reason=reason, audit_path=audit_path)


def expire_due(
    repo: ApprovalRepository,
    *,
    audit_path: str | Path | None = None,
) -> list[int]:
    """만료 시각 지난 pending들을 일괄 expired 처리. 처리된 id 목록 반환."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    expired_ids = repo.expire_pending_until(now_iso=now_iso)
    for rid in expired_ids:
        audit_log("approval_expired", path=audit_path, id=rid, at=now_iso)
    if expired_ids:
        logger.info(
            "approvals expired",
            extra={"event": "approvals_expired", "ids": expired_ids, "count": len(expired_ids)},
        )
    return expired_ids


# -------- internal --------

def _decide(
    repo: ApprovalRepository,
    id: int,
    *,
    new_status: str,
    by: str,
    reason: str | None,
    audit_path: str | Path | None,
) -> None:
    current = repo.get(id)
    if current is None:
        raise ApprovalNotFound(f"approval id={id} not found")
    if current.status != "pending":
        raise ApprovalAlreadyDecided(
            f"approval id={id} already decided: {current.status}"
        )

    decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed = repo.update_status(
        id,
        new_status=new_status,  # type: ignore[arg-type]
        decided_at=decided_at,
        decided_by=by,
        decision_reason=reason,
    )
    if changed == 0:
        # 다른 트랜잭션이 먼저 결정한 경우
        raise ApprovalAlreadyDecided(
            f"approval id={id} was concurrently decided"
        )

    audit_log(
        f"approval_{new_status}",
        path=audit_path,
        id=id,
        by=by,
        reason=reason,
    )
    logger.info(
        "approval decided",
        extra={"event": f"approval_{new_status}", "id": id, "by": by},
    )
