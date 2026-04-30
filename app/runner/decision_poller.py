"""VM의 approval_server에서 미처리 결정을 가져와 Mac에서 실제로 처리.

흐름:
1. GET {APPROVAL_SERVER_URL}/decisions/pending  (X-Poller-Token 헤더)
2. 각 결정에 대해 M6.6 decision 함수 호출 (approve_and_apply / reject_decision / regenerate_approval)
3. 처리 완료 후 POST {APPROVAL_SERVER_URL}/decisions/{qid}/ack

호출처: launchd plist 또는 cli `--mode poll_decisions`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("scraper.runner.decision_poller")


def poll_and_dispatch(
    *,
    server_url: str,
    poller_token: str,
    conn: sqlite3.Connection,
    configs_dir: Path,
    data_dir: Path,
    site_runner: Callable[[str, sqlite3.Connection, Any], dict[str, Any]],
    timeout: float = 10.0,
) -> dict[str, int]:
    """한 번 polling해서 받은 결정들을 처리. 결과 카운트 dict 반환.

    site_runner: cli._run_site를 lambda로 wrap한 callback (M6.6a approve용).
    """
    from app.integrations import SlackConfig, SlackNotifier
    from app.patching import (
        ApprovalNotPending,
        approve_and_apply,
        reject_decision,
    )
    from app.runner.healing_flow import (
        PreviousApprovalNotEligible,
        RegenerateLimitReached,
        regenerate_approval,
    )
    from app.runner.cli import _load_api_sample, _load_api_sample_prev
    from app.evidence import EvidenceStore
    from app.storage import ApprovalRepository

    slack = SlackNotifier(SlackConfig.from_env())
    approval_repo = ApprovalRepository(conn)
    evidence = EvidenceStore(data_dir)

    decisions = _fetch_pending(server_url, poller_token, timeout=timeout)
    counts = {"approved": 0, "rejected": 0, "regenerated": 0, "failed": 0, "total": len(decisions)}

    for d in decisions:
        try:
            kind = d["kind"]
            approval_id = int(d["approval_id"])
            by = d["by"]
            reason = d.get("reason")

            if kind == "approve":
                approve_and_apply(
                    approval_id=approval_id, by=by, conn=conn,
                    configs_dir=configs_dir, slack=slack,
                    rerun_runner=site_runner,
                )
                counts["approved"] += 1
            elif kind == "reject":
                reject_decision(
                    approval_id=approval_id, by=by,
                    reason=reason or "rejected from slack",
                    conn=conn, slack=slack,
                )
                counts["rejected"] += 1
            elif kind == "regenerate":
                prev = approval_repo.get(approval_id)
                if prev is None:
                    raise PreviousApprovalNotEligible(f"approval #{approval_id} not found")
                yaml_path = configs_dir / f"{prev.site}.yaml"
                regenerate_approval(
                    prev_approval_id=approval_id, by=by,
                    yaml_path=yaml_path,
                    evidence_loader=lambda: (
                        _load_api_sample(evidence, prev.site, prev.run_id),
                        _load_api_sample_prev(evidence, prev.site),
                    ),
                    approval_repo=approval_repo, slack=slack, db_conn=conn,
                )
                counts["regenerated"] += 1
            else:
                logger.warning("unknown decision kind", extra={"kind": kind})
                counts["failed"] += 1
                continue

            _ack(server_url, poller_token, d["queue_id"], timeout=timeout)
        except (LookupError, ApprovalNotPending,
                PreviousApprovalNotEligible, RegenerateLimitReached) as e:
            # 이미 결정됐거나 정책 위반 — ack해서 큐에서 빼되 카운트는 failed
            logger.warning(
                "decision not eligible, ack and skip",
                extra={"event": "decision_skipped",
                       "queue_id": d.get("queue_id"), "error": str(e)},
            )
            _ack(server_url, poller_token, d["queue_id"], timeout=timeout)
            counts["failed"] += 1
        except Exception as e:
            logger.exception(
                "decision dispatch crashed",
                extra={"event": "decision_crashed",
                       "queue_id": d.get("queue_id"), "error": str(e)},
            )
            # ack 안 함 — 다음 polling에서 재시도
            counts["failed"] += 1

    if counts["total"] > 0:
        logger.info("polling cycle done", extra={"event": "poll_cycle_done", **counts})
    return counts


# -------- HTTP helpers --------

def _fetch_pending(server_url: str, token: str, *, timeout: float) -> list[dict]:
    url = server_url.rstrip("/") + "/decisions/pending"
    req = urllib.request.Request(
        url, method="GET", headers={"X-Poller-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        logger.warning(
            "polling fetch failed",
            extra={"event": "poll_fetch_failed", "error": str(e), "url": url},
        )
        return []
    return data.get("decisions") or []


def _ack(server_url: str, token: str, queue_id: str, *, timeout: float) -> None:
    url = server_url.rstrip("/") + f"/decisions/{queue_id}/ack"
    req = urllib.request.Request(
        url, method="POST", data=b"",
        headers={"X-Poller-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        logger.warning(
            "ack failed",
            extra={"event": "ack_failed", "queue_id": queue_id, "error": str(e)},
        )
