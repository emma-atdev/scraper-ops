"""approve/reject 운영자 결정의 orchestration.

approve_and_apply: pending → approved → yaml 적용 → rerun → 결과 회신 (실패 시 롤백)
reject_decision:    pending → rejected → 슬랙 thread 회신
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Callable

from app.approval import approve, reject
from app.audit import audit_log
from app.collectors.fetchers.http import HttpFetcher
from app.config import load_all_sites
from app.healing.dry_run import PatchApplyError
from app.integrations import SlackNotifier
from app.llm import PatchCandidate
from app.patching.apply import (
    PatchApplyValidationError,
    apply_patch_to_file,
    rollback_from_backup,
)
from app.storage import ApprovalRepository, Repository

logger = logging.getLogger("scraper.patching.decision")


class ApprovalNotPending(RuntimeError):
    """approval이 pending 상태가 아닐 때."""


def approve_and_apply(
    *,
    approval_id: int,
    by: str,
    conn: sqlite3.Connection,
    configs_dir: Path,
    slack: SlackNotifier,
    rerun_runner: Callable[[str, sqlite3.Connection, SlackNotifier], dict[str, Any]],
) -> dict[str, Any]:
    """approve 흐름. 호출자(cli)가 conn/configs_dir/slack을 주입.

    rerun_runner(site, conn, slack) -> {"success": bool, "stats": UpsertStats|None, "error": str|None}
        site에 한 번 더 collect run을 돌리는 callback. cli가 _run_site를 lambda로 감싸서 넘긴다.

    Returns: {"applied": bool, "rerun_success": bool, "rolled_back": bool, ...}
    """
    approval_repo = ApprovalRepository(conn)
    row = approval_repo.get(approval_id)
    if row is None:
        raise LookupError(f"approval id={approval_id} not found")
    if row.status != "pending":
        raise ApprovalNotPending(f"approval #{approval_id} is {row.status}, not pending")

    # 1. DB 전이 (approve) — audit는 state_machine이 처리
    approve(approval_repo, approval_id, by=by)

    # 2. patch 역직렬화
    patch = PatchCandidate.model_validate_json(row.patch_json)

    # 3. yaml 파일 결정 + 적용
    yaml_path = Path(configs_dir) / f"{row.site}.yaml"
    try:
        applied = apply_patch_to_file(
            yaml_path=yaml_path, patch=patch, site=row.site,
        )
    except (PatchApplyError, PatchApplyValidationError) as e:
        # yaml 적용 자체가 실패. DB는 이미 approved지만 적용 실패 — 사람 점검 필요.
        audit_log("patch_apply_failed", id=approval_id, site=row.site, error=str(e))
        logger.error(
            "patch apply failed after approve",
            extra={"event": "patch_apply_failed", "approval_id": approval_id, "error": str(e)},
        )
        slack.notify_apply_result(
            channel=row.slack_channel or "", thread_ts=row.slack_thread_ts or "",
            site=row.site, approval_id=approval_id, success=False,
            message=f"yaml 적용 실패: {e}",
        )
        return {"applied": False, "rerun_success": False, "rolled_back": False, "error": str(e)}

    audit_log(
        "patch_applied", id=approval_id, site=row.site,
        yaml_path=str(applied.yaml_path), backup_path=str(applied.backup_path),
        by=by,
    )

    # 4. rerun (동기)
    rerun_outcome = rerun_runner(row.site, conn, slack)
    rerun_success = bool(rerun_outcome.get("success"))

    if rerun_success:
        stats = rerun_outcome.get("stats")
        slack.notify_apply_result(
            channel=row.slack_channel or "", thread_ts=row.slack_thread_ts or "",
            site=row.site, approval_id=approval_id, success=True,
            rerun_inserted=getattr(stats, "inserted", 0),
            rerun_updated=getattr(stats, "updated", 0),
        )
        return {
            "applied": True, "rerun_success": True, "rolled_back": False,
            "backup_path": str(applied.backup_path),
        }

    # 5. rerun 실패 → 자동 롤백 (B-1)
    rollback_from_backup(yaml_path=yaml_path, backup_path=applied.backup_path)
    audit_log(
        "patch_rolled_back", id=approval_id, site=row.site,
        yaml_path=str(yaml_path), backup_path=str(applied.backup_path),
        reason=str(rerun_outcome.get("error") or "rerun failed"),
    )
    slack.notify_apply_result(
        channel=row.slack_channel or "", thread_ts=row.slack_thread_ts or "",
        site=row.site, approval_id=approval_id, success=False,
        message=str(rerun_outcome.get("error") or "rerun failed"),
    )
    return {
        "applied": True, "rerun_success": False, "rolled_back": True,
        "backup_path": str(applied.backup_path),
    }


def reject_decision(
    *,
    approval_id: int,
    by: str,
    reason: str,
    conn: sqlite3.Connection,
    slack: SlackNotifier,
) -> None:
    approval_repo = ApprovalRepository(conn)
    row = approval_repo.get(approval_id)
    if row is None:
        raise LookupError(f"approval id={approval_id} not found")
    if row.status != "pending":
        raise ApprovalNotPending(f"approval #{approval_id} is {row.status}, not pending")

    reject(approval_repo, approval_id, by=by, reason=reason)

    if row.slack_thread_ts:
        slack.notify_decision_result(
            channel=row.slack_channel or "", thread_ts=row.slack_thread_ts,
            site=row.site, approval_id=approval_id,
            decision="rejected", by=by, reason=reason,
        )
