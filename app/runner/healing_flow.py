"""실패한 collect run에서 self-healing 흐름을 트리거.

흐름:
  1. 트리거 조건 체크 (diagnosis 카테고리, 신규 사이트 가드, pending 중복 방지)
  2. healing.generate_patch_candidate (LLM 1회)
  3. dry_run.run_dry_run (효과 검증)
  4. approval.create_approval (DB row + audit)
  5. slack.notify_approval_request 또는 notify_healing_unavailable
  6. ApprovalRepository.attach_slack

이 모듈은 self-healing을 위한 _옵션_ 경로다. 실패 시 정상 collect 흐름을 막지 않는다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.approval import create_approval, supersede
from app.clock import KST
from app.diagnosis import Diagnosis, FailureCategory, classify_failure
from app.healing import (
    DryRunResult,
    generate_patch_candidate,
    load_yaml_text,
    run_dry_run,
)
from app.integrations import SlackNotifier
from app.llm import (
    LLMClient,
    LLMConfig,
    LLMNotConfiguredError,
    LLMOutputRejectedError,
    PatchCandidate,
)
from app.storage import ApprovalRepository, Repository

logger = logging.getLogger("scraper.runner.healing")

HEALABLE_CATEGORIES = {
    FailureCategory.SCHEMA_CHANGE,
    FailureCategory.EMPTY_RESULTS,
}

REJECT_GUARD_HOURS = 24  # M6.6 D-1: 최근 24h 내 rejected 있으면 자동 healing 스킵
REGENERATE_LIMIT_PER_RUN = 3  # M6.6b E-1: 같은 run_id에 regenerate 최대 3회


def maybe_trigger_healing(
    *,
    site: str,
    site_run_id: str,
    yaml_path: Path,
    diagnosis: Diagnosis,
    api_sample: dict[str, Any] | None,
    api_sample_prev: dict[str, Any] | None,
    repo: Repository,
    approval_repo: ApprovalRepository,
    slack: SlackNotifier,
    db_conn,
    llm_client: LLMClient | None = None,
) -> None:
    """실패한 run에 대해 healing이 적합하면 patch candidate → dry_run → approval → slack 흐름.

    예외는 잡아서 로깅한다. 호출자(_run_site)의 정상 흐름을 막지 않는다.
    """
    # 1. 트리거 조건들
    if diagnosis.category not in HEALABLE_CATEGORIES:
        logger.info(
            "healing skipped: category not healable",
            extra={"event": "healing_skipped", "site": site, "reason": "category",
                   "category": diagnosis.category.value},
        )
        return

    if not repo.has_successful_history(site):
        logger.info(
            "healing skipped: no prior successful run (new site onboarding)",
            extra={"event": "healing_skipped", "site": site, "reason": "no_history"},
        )
        return

    if approval_repo.list_pending(site=site):
        logger.info(
            "healing skipped: site already has pending approval",
            extra={"event": "healing_skipped", "site": site, "reason": "pending_exists"},
        )
        return

    # M6.6 D-1: 최근 24h 내 reject된 처방이 있으면 자동 healing 안 부른다.
    # 운영자가 명시적으로 regenerate 명령을 치는 경우는 별도 (M6.6b).
    guard_since = (datetime.now(timezone.utc) - timedelta(hours=REJECT_GUARD_HOURS))
    guard_since_iso = guard_since.isoformat(timespec="seconds")
    if approval_repo.list_recent_rejected(site=site, since_iso=guard_since_iso):
        logger.info(
            "healing skipped: recent rejection within guard window",
            extra={"event": "healing_skipped", "site": site, "reason": "recent_reject"},
        )
        return

    if api_sample is None:
        logger.info(
            "healing skipped: no api sample captured",
            extra={"event": "healing_skipped", "site": site, "reason": "no_sample"},
        )
        return

    client = llm_client or LLMClient(LLMConfig.from_env())
    if not client.enabled:
        logger.info(
            "healing skipped: LLM not configured",
            extra={"event": "healing_skipped", "site": site, "reason": "no_llm"},
        )
        return

    # 2~6: 단일 try로 묶고 실패 시 simple 알림 fallback
    try:
        _run_healing_pipeline(
            site=site,
            site_run_id=site_run_id,
            yaml_path=yaml_path,
            diagnosis=diagnosis,
            api_sample=api_sample,
            api_sample_prev=api_sample_prev,
            approval_repo=approval_repo,
            slack=slack,
            db_conn=db_conn,
            client=client,
        )
    except Exception as e:
        logger.exception(
            "healing pipeline crashed",
            extra={"event": "healing_crashed", "site": site, "error": str(e)},
        )


def _run_healing_pipeline(
    *,
    site: str,
    site_run_id: str,
    yaml_path: Path,
    diagnosis: Diagnosis,
    api_sample: dict[str, Any],
    api_sample_prev: dict[str, Any] | None,
    approval_repo: ApprovalRepository,
    slack: SlackNotifier,
    db_conn,
    client: LLMClient,
) -> None:
    yaml_text = load_yaml_text(yaml_path)

    evidence_for_llm = {
        "report": {
            "diagnosis": {
                "category": diagnosis.category.value,
                "summary": diagnosis.summary,
            },
            "issues": [],
            "meta": {},
        },
        "api_sample": api_sample,
        "api_sample_prev": api_sample_prev,
    }

    # LLM patch 생성
    try:
        patch = generate_patch_candidate(
            site=site, yaml_path=yaml_path, evidence=evidence_for_llm, client=client,
        )
    except (LLMNotConfiguredError, LLMOutputRejectedError) as e:
        logger.warning(
            "healing patch generation failed",
            extra={"event": "healing_patch_failed", "site": site, "error": str(e)},
        )
        slack.notify_healing_unavailable(
            site=site, run_id=site_run_id,
            reason_label="LLM 처방 생성 실패 (retry 한도 도달)",
            detail=str(e)[:500],
        )
        return

    # 빈 changes (LLM이 infeasible 판정)
    if not patch.changes:
        logger.info(
            "healing patch empty (infeasible)",
            extra={"event": "healing_infeasible", "site": site},
        )
        slack.notify_healing_unavailable(
            site=site, run_id=site_run_id,
            reason_label="LLM이 처방 불가 판정",
            detail=patch.reason,
        )
        return

    # dry-run
    dry_run = run_dry_run(
        site=site, yaml_text=yaml_text, patch=patch, api_sample=api_sample,
    )

    if dry_run.verdict == "patch_invalid":
        slack.notify_healing_unavailable(
            site=site, run_id=site_run_id,
            reason_label="처방 적용 결과 yaml 스키마 위반",
            detail=dry_run.patch_invalid_reason,
        )
        return
    if dry_run.verdict == "patch_apply_failed":
        slack.notify_healing_unavailable(
            site=site, run_id=site_run_id,
            reason_label="처방 path가 yaml과 안 맞음",
            detail=dry_run.patch_apply_failed_reason,
        )
        return

    # approval row 생성 (improved/regressed/unchanged만)
    approval_id = create_approval(
        approval_repo,
        run_id=site_run_id,
        site=site,
        patch_json=_serialize_patch(patch),
        dry_run_json=_serialize_dry_run(dry_run),
    )

    # slack 메시지 게시
    expires_at_kst = _format_kst_deadline(approval_repo, approval_id)
    resp = slack.notify_approval_request(
        approval_id=approval_id,
        site=site,
        run_id=site_run_id,
        diagnosis=diagnosis,
        patch=patch,
        dry_run=dry_run,
        expires_at_kst=expires_at_kst,
    )

    # thread_ts 매핑
    if resp and resp.get("ok") and resp.get("ts"):
        approval_repo.attach_slack(
            approval_id,
            channel=resp.get("channel") or "",
            thread_ts=resp["ts"],
        )

    logger.info(
        "healing approval posted",
        extra={"event": "healing_approval_posted", "site": site,
               "approval_id": approval_id, "verdict": dry_run.verdict},
    )


def _serialize_patch(patch: PatchCandidate) -> str:
    return patch.model_dump_json()


def _serialize_dry_run(result: DryRunResult) -> str:
    return json.dumps(
        {
            "verdict": result.verdict,
            "before_count": result.before_count,
            "after_count": result.after_count,
            "before_missing_required": result.before_missing_required,
            "after_missing_required": result.after_missing_required,
            "before_issues": result.before_issues,
            "after_issues": result.after_issues,
            "sample_records": result.sample_records,
            "patch_invalid_reason": result.patch_invalid_reason,
            "patch_apply_failed_reason": result.patch_apply_failed_reason,
        },
        ensure_ascii=False,
    )


def _format_kst_deadline(repo: ApprovalRepository, approval_id: int) -> str:
    row = repo.get(approval_id)
    if row is None:
        return "-"
    try:
        dt = datetime.fromisoformat(row.expires_at)
        return dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return row.expires_at


# ---------- M6.6b: regenerate ----------

class RegenerateLimitReached(RuntimeError):
    """같은 run_id에 대한 regenerate 호출 횟수가 한도 도달."""


class PreviousApprovalNotEligible(RuntimeError):
    """regenerate 대상 approval이 pending이 아니거나 존재하지 않음."""


def regenerate_approval(
    *,
    prev_approval_id: int,
    by: str,
    yaml_path: Path,
    evidence_loader,  # callable() -> (api_sample, api_sample_prev) 디스크에서 로드
    approval_repo: ApprovalRepository,
    slack: SlackNotifier,
    db_conn,
    llm_client: LLMClient | None = None,
) -> int:
    """이전 approval을 superseded로 마감하고 같은 evidence로 다른 후보 patch를 생성.

    반환: 새 approval id. 실패 시 예외 raise.
    """
    prev = approval_repo.get(prev_approval_id)
    if prev is None:
        raise PreviousApprovalNotEligible(f"approval #{prev_approval_id} not found")
    if prev.status != "pending":
        raise PreviousApprovalNotEligible(
            f"approval #{prev_approval_id} is {prev.status}, not pending"
        )

    if approval_repo.count_for_run(prev.run_id) >= REGENERATE_LIMIT_PER_RUN:
        raise RegenerateLimitReached(
            f"regenerate limit reached for run_id={prev.run_id} "
            f"({REGENERATE_LIMIT_PER_RUN} attempts already)"
        )

    client = llm_client or LLMClient(LLMConfig.from_env())
    if not client.enabled:
        raise LLMNotConfiguredError("OPENAI_API_KEY not set")

    api_sample, api_sample_prev = evidence_loader()
    if api_sample is None:
        raise PreviousApprovalNotEligible(
            f"api_sample for run_id={prev.run_id} not found on disk"
        )

    # 이전 후보 정보를 LLM prompt에 박제 (다른 접근 유도)
    prev_patch = PatchCandidate.model_validate_json(prev.patch_json)
    previous_attempts = [{
        "reason": prev_patch.reason,
        "risk": prev_patch.risk,
        "changes": [c.model_dump() for c in prev_patch.changes],
    }]

    # 이전 dry_run에 박혀 있던 진단 카테고리 복원 (없으면 schema_change 디폴트)
    diagnosis = _diagnosis_from_dry_run(prev.dry_run_json) or _diagnosis_default()

    yaml_text = load_yaml_text(yaml_path)
    evidence_for_llm = {
        "report": {
            "diagnosis": {
                "category": diagnosis.category.value,
                "summary": diagnosis.summary,
            },
            "issues": [],
            "meta": {},
        },
        "api_sample": api_sample,
        "api_sample_prev": api_sample_prev,
    }

    new_patch = generate_patch_candidate(
        site=prev.site, yaml_path=yaml_path, evidence=evidence_for_llm,
        client=client, previous_attempts=previous_attempts,
    )

    if not new_patch.changes:
        # 빈 changes — supersede만 하고 healing_unavailable
        supersede(approval_repo, prev_approval_id, by=by)
        slack.notify_healing_unavailable(
            site=prev.site, run_id=prev.run_id,
            reason_label="regenerate 결과도 LLM 처방 불가 판정",
            detail=new_patch.reason,
        )
        raise PreviousApprovalNotEligible(
            "LLM returned empty changes on regenerate"
        )

    dry_run = run_dry_run(
        site=prev.site, yaml_text=yaml_text, patch=new_patch, api_sample=api_sample,
    )

    if dry_run.verdict in ("patch_invalid", "patch_apply_failed"):
        supersede(approval_repo, prev_approval_id, by=by)
        slack.notify_healing_unavailable(
            site=prev.site, run_id=prev.run_id,
            reason_label=f"regenerate 결과 {dry_run.verdict}",
            detail=dry_run.patch_invalid_reason or dry_run.patch_apply_failed_reason,
        )
        raise PreviousApprovalNotEligible(
            f"regenerate produced unusable patch: {dry_run.verdict}"
        )

    # 새 approval 생성
    new_id = create_approval(
        approval_repo,
        run_id=prev.run_id,  # 같은 evidence라 run_id 공유
        site=prev.site,
        patch_json=new_patch.model_dump_json(),
        dry_run_json=_serialize_dry_run(dry_run),
    )

    # 이전 approval supersede (새 id 참조)
    supersede(approval_repo, prev_approval_id, by=by, superseded_by_id=new_id)

    # 이전 thread에 안내
    if prev.slack_thread_ts:
        slack.notify_decision_result(
            channel=prev.slack_channel or "", thread_ts=prev.slack_thread_ts,
            site=prev.site, approval_id=prev_approval_id,
            decision="superseded", by=by,
            reason=f"새 후보 #{new_id}로 이어짐",
        )

    # 새 카드는 새 메시지로 (4-A)
    expires_at_kst = _format_kst_deadline(approval_repo, new_id)
    resp = slack.notify_approval_request(
        approval_id=new_id, site=prev.site, run_id=prev.run_id,
        diagnosis=diagnosis, patch=new_patch, dry_run=dry_run,
        expires_at_kst=expires_at_kst,
    )
    if resp and resp.get("ok") and resp.get("ts"):
        approval_repo.attach_slack(
            new_id, channel=resp.get("channel") or "", thread_ts=resp["ts"],
        )

    logger.info(
        "regenerate produced new approval",
        extra={"event": "approval_regenerated", "prev_id": prev_approval_id,
               "new_id": new_id, "site": prev.site},
    )
    return new_id


def _diagnosis_from_dry_run(dry_run_json: str | None) -> Diagnosis | None:
    """dry_run_json에는 diagnosis가 직접 없지만 issues는 있음 — 거기서 복원 시도."""
    if not dry_run_json:
        return None
    try:
        import json as _json

        data = _json.loads(dry_run_json)
        # dry_run_json에는 diagnosis가 아니라 issues만 있어서 정확한 복원은 어렵다.
        # before_issues/after_issues 코드를 ValidationIssue로 만들 수도 있지만,
        # 여기서는 단순화 — None 반환 후 호출자가 디폴트 사용.
        return None
    except Exception:
        return None


def _diagnosis_default() -> Diagnosis:
    """regenerate 시 진단 정보가 없을 때 fallback."""
    return Diagnosis(
        category=FailureCategory.SCHEMA_CHANGE,
        summary="regenerate (이전 후보 거절 후 다른 접근 요청)",
        issue_codes=[],
    )
