"""1회 실행 CLI. systemd timer가 호출하는 진입점.

흐름:
- 환경변수 로드
- 사이트 config 로드
- runtime 정책 통과 검사
- file lock 획득
- collector 실행 → DB 저장
- file lock 해제
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from pathlib import Path

from app.approval import expire_due
from app.clock import yesterday_kst_window
from app.collectors.registry import get_collector
from app.config import SiteConfig, load_all_sites
from app.diagnosis import classify_failure
from app.env import load_dotenv
from app.evidence import EvidenceStore
from app.integrations import SlackConfig, SlackNotifier
from app.locking import RunLock
from app.logging_setup import setup_logging
from app.models import UpsertStats
from app.runner.healing_flow import maybe_trigger_healing
from app.runtime import current_environment, parse_allowed_sites, should_run_site
from app.storage import ApprovalRepository, Repository, init_schema, open_connection
from app.validators import validate_postings

logger = logging.getLogger("scraper.runner")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scraper-runner")
    parser.add_argument("--site", help="Run only this site. Default: all sites that pass runtime policy.")
    parser.add_argument(
        "--environment",
        help="Override SCRAPER_ENVIRONMENT for this run (local/vm/ci/container).",
    )
    parser.add_argument("--configs-dir", default="configs/sites", help="Directory with site YAML configs.")
    parser.add_argument("--lock-path", default="data/locks/scraper-ops.lock")
    parser.add_argument("--db-path", default="data/scraper.db")
    parser.add_argument("--data-dir", default="data", help="Root for snapshots/, reports/.")
    parser.add_argument(
        "--notify",
        choices=["always", "failure_or_change", "failure", "never"],
        default="failure_or_change",
        help=(
            "When to send Slack notifications. "
            "failure_or_change(default): on failure or when records changed. "
            "failure: only on failure. always: every run. never: silent."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["collect", "daily_summary", "approve", "reject", "regenerate", "poll_decisions"],
        default="collect",
        help=(
            "collect(default): scheduled crawl. "
            "daily_summary: post yesterday-summary to Slack. "
            "approve: approve approval_request <id>. "
            "reject: reject approval_request <id>. "
            "regenerate: supersede pending approval and request alt patch from LLM. "
            "poll_decisions: pull decisions from VM approval_server and dispatch."
        ),
    )
    parser.add_argument("--id", type=int, help="approval_request id (for approve/reject mode)")
    parser.add_argument("--reason", type=str, default=None, help="reject reason")
    parser.add_argument("--dry-run", action="store_true", help="Do not execute collectors. Just print plan.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    load_dotenv()
    setup_logging()

    if args.mode == "daily_summary":
        return _run_daily_summary(args)
    if args.mode == "approve":
        return _run_approve(args)
    if args.mode == "reject":
        return _run_reject(args)
    if args.mode == "regenerate":
        return _run_regenerate(args)
    if args.mode == "poll_decisions":
        return _run_poll_decisions(args)

    environment = args.environment or current_environment()
    allowed_sites = parse_allowed_sites(os.environ.get("SCRAPER_ALLOWED_SITES"))

    configs = load_all_sites(args.configs_dir)
    if not configs:
        print(f"[runner] no site configs found in {args.configs_dir}")
        return 0

    target_sites: list[str]
    if args.site:
        if args.site not in configs:
            print(f"[runner] site '{args.site}' not found in {args.configs_dir}")
            return 2
        target_sites = [args.site]
    else:
        target_sites = list(configs.keys())

    plan: list[str] = []
    for site_name in target_sites:
        cfg = configs[site_name]
        if should_run_site(cfg, environment=environment, allowed_sites=allowed_sites):
            plan.append(site_name)
        else:
            print(f"[runner] skip {site_name} (runtime policy)")

    if not plan:
        print("[runner] nothing to run")
        return 0

    run_id = str(uuid.uuid4())
    print(f"[runner] run_id={run_id} env={environment} sites={plan}")

    if args.dry_run:
        print("[runner] dry-run mode, exiting before lock + execution")
        return 0

    Path(args.lock_path).parent.mkdir(parents=True, exist_ok=True)
    lock = RunLock(args.lock_path)
    if not lock.acquire():
        print(f"[runner] another run is in progress (lock={args.lock_path})")
        return 1

    try:
        conn = open_connection(args.db_path)
        init_schema(conn)
        repo = Repository(conn)
        evidence = EvidenceStore(args.data_dir)
        # 매 collect run 시작 시 만료된 approval들을 expired로 정리 (M6.4 C-1)
        expire_due(ApprovalRepository(conn))
        slack = SlackNotifier(SlackConfig.from_env())

        # 보관 정책 cleanup (실패해도 수집 계속)
        try:
            removed = evidence.cleanup()
            if any(removed.values()):
                logger.info(
                    "evidence cleanup",
                    extra={"event": "evidence_cleanup", **removed},
                )
        except Exception as e:
            logger.warning(
                "evidence cleanup failed",
                extra={"event": "evidence_cleanup_failed", "error": str(e)},
            )

        exit_code = 0
        approval_repo = ApprovalRepository(conn)
        for site_name in plan:
            try:
                site_cfg = configs[site_name]
                site_run_id = f"{run_id}:{site_name}"
                yaml_path = Path(args.configs_dir) / f"{site_name}.yaml"
                stats = _run_site(
                    site_cfg,
                    site_run_id=site_run_id,
                    repo=repo,
                    evidence=evidence,
                    slack=slack,
                    notify_policy=args.notify,
                    yaml_path=yaml_path,
                    approval_repo=approval_repo,
                    db_conn=conn,
                )
                logger.info(
                    f"{site_name} done",
                    extra={
                        "event": "site_done",
                        "site": site_name,
                        "run_id": site_run_id,
                        "inserted": stats.inserted,
                        "updated": stats.updated,
                        "unchanged": stats.unchanged,
                    },
                )
            except Exception:
                exit_code = 1
                logger.exception(
                    f"{site_name} failed",
                    extra={"event": "site_failed", "site": site_name, "run_id": run_id},
                )
        return exit_code
    finally:
        lock.release()


def _run_site(
    site_cfg: SiteConfig,
    *,
    site_run_id: str,
    repo: Repository,
    evidence: EvidenceStore,
    slack: SlackNotifier,
    notify_policy: str = "failure",
    yaml_path: Path | None = None,
    approval_repo: ApprovalRepository | None = None,
    db_conn=None,
) -> UpsertStats:
    site = site_cfg.site
    repo.start_run(site, site_run_id)
    total = UpsertStats()
    last_status = "success"
    all_issues: list = []
    notes: list[str] = []

    try:
        for collector_name, collector_cfg in site_cfg.collectors.items():
            if collector_cfg.purpose != "postings":
                # diagnostics/enrichment는 별도 분기 (M3+에서 확장).
                continue

            collector = get_collector(collector_cfg.type)
            logger.info(
                "collector start",
                extra={"event": "collector_start", "site": site, "collector": collector_name, "run_id": site_run_id},
            )
            result = collector.run(collector_cfg, site=site)

            # API sample 저장
            sample = result.evidence.get("first_page_sample") if result.evidence else None
            if sample is not None:
                evidence.write_api_sample(site, site_run_id, sample)

            # validation
            previous_count = repo.previous_count(site)
            outcome = validate_postings(
                result.records,
                collector_cfg.validation,
                previous_count=previous_count,
            )
            issues = list(result.issues) + list(outcome.issues)
            all_issues.extend(issues)

            if issues:
                notes.append(
                    f"{collector_name}: "
                    + ", ".join(f"{i.code}" for i in issues[:5])
                )

            if result.records:
                stats = repo.upsert_postings(site, site_run_id, result.records)
                total.inserted += stats.inserted
                total.updated += stats.updated
                total.unchanged += stats.unchanged

            # 실패로 분류: records 0건이고 issue 있음
            if not result.records and issues:
                last_status = "failed"

    except Exception as e:
        last_status = "failed"
        notes.append(f"exception:{type(e).__name__}:{e}")
        repo.finish_run(site_run_id, status=last_status, stats=total, notes="; ".join(notes))
        diagnosis = classify_failure(all_issues)
        report_path = evidence.write_report(
            site,
            site_run_id,
            status=last_status,
            records_count=total.inserted + total.updated + total.unchanged,
            issues=all_issues,
            diagnosis=diagnosis,
            meta={"exception": f"{type(e).__name__}:{e}"},
        )
        if _should_notify(notify_policy, last_status, total):
            slack.notify_run_result(
                site=site,
                run_id=site_run_id,
                status=last_status,
                stats=total,
                issues=all_issues,
                diagnosis=diagnosis,
                report_path=str(report_path),
            )
        raise

    repo.finish_run(site_run_id, status=last_status, stats=total, notes="; ".join(notes) or None)

    # 정상 run의 sample을 prev로 승격 (다음 run의 schema diff 기준)
    if last_status == "success":
        evidence.promote_prev_sample(site, site_run_id)

    diagnosis = classify_failure(all_issues)
    report_path = evidence.write_report(
        site,
        site_run_id,
        status=last_status,
        records_count=total.inserted + total.updated + total.unchanged,
        issues=all_issues,
        diagnosis=diagnosis,
        meta={"inserted": total.inserted, "updated": total.updated, "unchanged": total.unchanged},
    )
    if _should_notify(notify_policy, last_status, total):
        slack.notify_run_result(
            site=site,
            run_id=site_run_id,
            status=last_status,
            stats=total,
            issues=all_issues,
            diagnosis=diagnosis,
            report_path=str(report_path),
        )

    # M6.5: 실패한 run에 self-healing 흐름 트리거 (조건 충족 시에만)
    if (
        last_status == "failed"
        and yaml_path is not None
        and approval_repo is not None
        and db_conn is not None
    ):
        maybe_trigger_healing(
            site=site,
            site_run_id=site_run_id,
            yaml_path=yaml_path,
            diagnosis=diagnosis,
            api_sample=_load_api_sample(evidence, site, site_run_id),
            api_sample_prev=_load_api_sample_prev(evidence, site),
            repo=repo,
            approval_repo=approval_repo,
            slack=slack,
            db_conn=db_conn,
        )

    return total


def _run_daily_summary(args) -> int:
    """어제 캘린더 일자(KST 기준) 운영 요약을 Slack에 게시한다."""
    conn = open_connection(args.db_path)
    init_schema(conn)
    repo = Repository(conn)

    since, until, target_date = yesterday_kst_window()
    summary = repo.summarize_window(since, until)

    slack = SlackNotifier(SlackConfig.from_env())
    slack.notify_daily_summary(summary, target_date=target_date.isoformat())

    by_site = summary.get("by_site") or {}
    logger.info(
        "daily summary posted",
        extra={
            "event": "daily_summary_posted",
            "target_date": target_date.isoformat(),
            "sites": list(by_site.keys()),
            "total_runs": sum(s["runs"] for s in by_site.values()),
            "total_failed": sum(s["failed"] for s in by_site.values()),
        },
    )
    return 0


def _load_api_sample(evidence: EvidenceStore, site: str, run_id: str) -> dict | None:
    """방금 실패한 run의 api_sample.json을 읽는다 (없으면 None)."""
    from app.evidence import _safe_run_id

    path = evidence.base / "snapshots" / site / _safe_run_id(run_id) / "api_sample.json"
    if not path.exists():
        return None
    try:
        import json as _json

        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_api_sample_prev(evidence: EvidenceStore, site: str) -> dict | None:
    """직전 정상 run에서 promote된 prev sample (있으면)."""
    path = evidence.base / "snapshots" / site / "api_sample_prev.json"
    if not path.exists():
        return None
    try:
        import json as _json

        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _should_notify(policy: str, status: str, stats: UpsertStats) -> bool:
    if policy == "never":
        return False
    if policy == "always":
        return True
    if policy == "failure_or_change":
        if status != "success":
            return True
        return stats.inserted > 0 or stats.updated > 0
    # "failure" 정책
    return status != "success"


def _run_approve(args) -> int:
    """approve_and_apply orchestration. yaml 적용 + rerun까지 동기로."""
    if args.id is None:
        print("[runner] --id is required for approve mode")
        return 2

    from app.patching import approve_and_apply, ApprovalNotPending

    by = f"cli:{os.environ.get('USER', 'unknown')}"
    conn = open_connection(args.db_path)
    init_schema(conn)
    slack = SlackNotifier(SlackConfig.from_env())

    def _rerun(site: str, conn_, slack_) -> dict:
        """approve 후 같은 site 한 번 더 collect run을 동기로 돌린다.

        성공 여부 + UpsertStats를 dict로 반환. 예외는 catch해서 success=False.
        """
        configs = load_all_sites(args.configs_dir)
        if site not in configs:
            return {"success": False, "stats": None, "error": f"site config not found: {site}"}
        site_cfg = configs[site]
        repo = Repository(conn_)
        evidence = EvidenceStore(args.data_dir)
        approval_repo = ApprovalRepository(conn_)
        rerun_run_id = f"{uuid.uuid4()}:{site}:rerun"
        try:
            stats = _run_site(
                site_cfg,
                site_run_id=rerun_run_id,
                repo=repo,
                evidence=evidence,
                slack=slack_,
                notify_policy="never",  # rerun 자체 알림은 끄고 thread reply로 통합
                yaml_path=Path(args.configs_dir) / f"{site}.yaml",
                approval_repo=approval_repo,
                db_conn=conn_,
            )
        except Exception as e:
            return {"success": False, "stats": None, "error": f"{type(e).__name__}:{e}"}

        # 실패 판정: stats가 다 0이면 패치가 효과 없는 것으로 간주
        success = stats.inserted > 0 or stats.updated > 0 or stats.unchanged > 0
        return {"success": success, "stats": stats, "error": None}

    try:
        result = approve_and_apply(
            approval_id=args.id, by=by, conn=conn,
            configs_dir=Path(args.configs_dir), slack=slack,
            rerun_runner=_rerun,
        )
    except (LookupError, ApprovalNotPending) as e:
        print(f"[runner] approve failed: {e}")
        return 2

    print(
        f"[runner] approve done: applied={result['applied']} "
        f"rerun_success={result['rerun_success']} rolled_back={result['rolled_back']}"
    )
    return 0 if result.get("rerun_success") else 1


def _run_reject(args) -> int:
    if args.id is None:
        print("[runner] --id is required for reject mode")
        return 2
    if not args.reason:
        print("[runner] --reason is required for reject mode")
        return 2

    from app.patching import reject_decision, ApprovalNotPending

    by = f"cli:{os.environ.get('USER', 'unknown')}"
    conn = open_connection(args.db_path)
    init_schema(conn)
    slack = SlackNotifier(SlackConfig.from_env())

    try:
        reject_decision(
            approval_id=args.id, by=by, reason=args.reason,
            conn=conn, slack=slack,
        )
    except (LookupError, ApprovalNotPending) as e:
        print(f"[runner] reject failed: {e}")
        return 2

    print(f"[runner] approval #{args.id} rejected")
    return 0


def _run_regenerate(args) -> int:
    """이전 pending approval을 supersede하고 LLM에 다른 후보 patch를 요청 (M6.6b)."""
    if args.id is None:
        print("[runner] --id is required for regenerate mode")
        return 2

    from app.runner.healing_flow import (
        PreviousApprovalNotEligible,
        RegenerateLimitReached,
        regenerate_approval,
    )

    by = f"cli:{os.environ.get('USER', 'unknown')}"
    conn = open_connection(args.db_path)
    init_schema(conn)
    approval_repo = ApprovalRepository(conn)
    slack = SlackNotifier(SlackConfig.from_env())
    evidence = EvidenceStore(args.data_dir)

    prev = approval_repo.get(args.id)
    if prev is None:
        print(f"[runner] approval #{args.id} not found")
        return 2

    yaml_path = Path(args.configs_dir) / f"{prev.site}.yaml"

    def _evidence_loader():
        return (
            _load_api_sample(evidence, prev.site, prev.run_id),
            _load_api_sample_prev(evidence, prev.site),
        )

    try:
        new_id = regenerate_approval(
            prev_approval_id=args.id, by=by,
            yaml_path=yaml_path, evidence_loader=_evidence_loader,
            approval_repo=approval_repo, slack=slack, db_conn=conn,
        )
    except RegenerateLimitReached as e:
        print(f"[runner] regenerate failed: {e}")
        return 2
    except PreviousApprovalNotEligible as e:
        print(f"[runner] regenerate failed: {e}")
        return 2

    print(f"[runner] regenerate done: prev #{args.id} → new #{new_id}")
    return 0


def _run_poll_decisions(args) -> int:
    """VM의 approval_server에서 미처리 결정을 가져와 처리 (M6.7)."""
    from app.runner.decision_poller import poll_and_dispatch

    server_url = os.environ.get("APPROVAL_SERVER_URL", "")
    poller_token = os.environ.get("POLLER_TOKEN", "")
    if not server_url or not poller_token:
        print("[runner] APPROVAL_SERVER_URL and POLLER_TOKEN must be set in env")
        return 2

    conn = open_connection(args.db_path)
    init_schema(conn)

    def _site_runner(site: str, conn_, slack_) -> dict:
        configs = load_all_sites(args.configs_dir)
        if site not in configs:
            return {"success": False, "stats": None, "error": f"site config not found: {site}"}
        site_cfg = configs[site]
        repo = Repository(conn_)
        evidence = EvidenceStore(args.data_dir)
        approval_repo = ApprovalRepository(conn_)
        rerun_run_id = f"{uuid.uuid4()}:{site}:rerun"
        try:
            stats = _run_site(
                site_cfg,
                site_run_id=rerun_run_id,
                repo=repo, evidence=evidence, slack=slack_,
                notify_policy="never",
                yaml_path=Path(args.configs_dir) / f"{site}.yaml",
                approval_repo=approval_repo,
                db_conn=conn_,
            )
        except Exception as e:
            return {"success": False, "stats": None, "error": f"{type(e).__name__}:{e}"}
        success = stats.inserted > 0 or stats.updated > 0 or stats.unchanged > 0
        return {"success": success, "stats": stats, "error": None}

    counts = poll_and_dispatch(
        server_url=server_url, poller_token=poller_token,
        conn=conn, configs_dir=Path(args.configs_dir),
        data_dir=Path(args.data_dir),
        site_runner=_site_runner,
    )
    print(f"[runner] poll done: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
