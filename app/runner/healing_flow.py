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
from datetime import datetime
from pathlib import Path
from typing import Any

from app.approval import create_approval
from app.clock import KST
from app.diagnosis import Diagnosis, FailureCategory
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
