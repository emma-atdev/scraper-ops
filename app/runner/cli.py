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

from app.collectors.registry import get_collector
from app.config import SiteConfig, load_all_sites
from app.diagnosis import classify_failure
from app.env import load_dotenv
from app.evidence import EvidenceStore
from app.integrations import SlackConfig, SlackNotifier
from app.locking import RunLock
from app.logging_setup import setup_logging
from app.models import UpsertStats
from app.runtime import current_environment, parse_allowed_sites, should_run_site
from app.storage import Repository, init_schema, open_connection
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
        choices=["collect", "daily_summary"],
        default="collect",
        help="collect(default): scheduled crawl. daily_summary: post 24h summary to Slack.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not execute collectors. Just print plan.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    load_dotenv()
    setup_logging()

    if args.mode == "daily_summary":
        return _run_daily_summary(args)

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
        for site_name in plan:
            try:
                site_cfg = configs[site_name]
                site_run_id = f"{run_id}:{site_name}"
                stats = _run_site(
                    site_cfg,
                    site_run_id=site_run_id,
                    repo=repo,
                    evidence=evidence,
                    slack=slack,
                    notify_policy=args.notify,
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
    return total


def _run_daily_summary(args) -> int:
    """직전 24시간 동안의 운영 요약을 Slack에 게시한다."""
    conn = open_connection(args.db_path)
    init_schema(conn)
    repo = Repository(conn)
    summary = repo.summarize_recent(hours=24)

    slack = SlackNotifier(SlackConfig.from_env())
    slack.notify_daily_summary(summary, hours=24)

    by_site = summary.get("by_site") or {}
    logger.info(
        "daily summary posted",
        extra={
            "event": "daily_summary_posted",
            "sites": list(by_site.keys()),
            "total_runs": sum(s["runs"] for s in by_site.values()),
            "total_failed": sum(s["failed"] for s in by_site.values()),
        },
    )
    return 0


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


if __name__ == "__main__":
    sys.exit(main())
