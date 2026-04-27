"""Slack 알림. M4 범위: chat.postMessage로 실패/성공 요약 전송.

Webhook이 아니라 Bot token + chat.postMessage를 쓰는 이유:
- 채널 ID 환경변수로 라우팅 가능
- 추후 M6에서 같은 채널에 thread reply, 버튼 인터랙션을 자연스럽게 확장
- 응답에서 ts(메시지 timestamp)를 받아 approval_request DB에 저장 가능

버튼·대화는 M6에서 추가. M4는 단순 알림만.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.collectors.base import ValidationIssue
from app.diagnosis import Diagnosis, FailureCategory
from app.models import UpsertStats

logger = logging.getLogger("scraper.slack")

CHAT_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
DEFAULT_TIMEOUT = 10

# 진단 카테고리 한글 라벨. 운영자가 메시지에서 즉시 의미 파악할 수 있게 한다.
DIAGNOSIS_LABELS = {
    FailureCategory.NONE: "정상",
    FailureCategory.NETWORK_BLOCKED: "네트워크 차단",
    FailureCategory.EMPTY_RESULTS: "결과 없음",
    FailureCategory.SCHEMA_CHANGE: "응답 구조 변경",
    FailureCategory.VOLUME_DROP: "수집량 급감",
    FailureCategory.MISSING_FIELDS: "필수 필드 누락",
    FailureCategory.UNKNOWN: "미분류",
}


def _diagnosis_label(diagnosis: Diagnosis) -> str:
    label = DIAGNOSIS_LABELS.get(diagnosis.category, diagnosis.category.value)
    return f"{label} ({diagnosis.category.value})"


@dataclass
class SlackConfig:
    bot_token: str
    channel_id: str

    @classmethod
    def from_env(cls) -> "SlackConfig | None":
        token = os.environ.get("SLACK_BOT_TOKEN")
        channel = os.environ.get("SLACK_CHANNEL_ID")
        if not token or not channel:
            return None
        return cls(bot_token=token, channel_id=channel)


class SlackNotifier:
    """Slack chat.postMessage 호출. config가 없으면 no-op."""

    def __init__(self, config: SlackConfig | None):
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config is not None

    def notify_run_result(
        self,
        *,
        site: str,
        run_id: str,
        status: str,
        stats: UpsertStats,
        issues: list[ValidationIssue],
        diagnosis: Diagnosis,
        report_path: str | None = None,
    ) -> dict[str, Any] | None:
        """run 종료 후 호출. 성공/실패 둘 다 전송."""
        if not self.enabled:
            logger.info(
                "slack disabled (no token/channel), skip notify",
                extra={"event": "slack_skipped", "site": site, "run_id": run_id},
            )
            return None

        text, blocks = self._build_message(
            site=site, run_id=run_id, status=status, stats=stats,
            issues=issues, diagnosis=diagnosis, report_path=report_path,
        )
        return self._post(text=text, blocks=blocks, run_id=run_id, site=site)

    # -------- 메시지 구성 --------

    def _build_message(
        self,
        *,
        site: str,
        run_id: str,
        status: str,
        stats: UpsertStats,
        issues: list[ValidationIssue],
        diagnosis: Diagnosis,
        report_path: str | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        emoji = self._status_emoji(status, diagnosis)
        status_label = "수집 완료" if status == "success" else "수집 실패"
        header = f"{emoji} {site} {status_label}"

        stats_line = f"신규 {stats.inserted}건 · 변경 {stats.updated}건 · 유지 {stats.unchanged}건"
        fields = [
            {"type": "mrkdwn", "text": f"*상태*\n{status_label} (`{status}`)"},
            {"type": "mrkdwn", "text": f"*수집 결과*\n{stats_line}"},
        ]
        if diagnosis.category != FailureCategory.NONE:
            diag_text = f"*진단*\n{_diagnosis_label(diagnosis)}"
            if diagnosis.summary and diagnosis.summary != "no issues":
                diag_text += f"\n{diagnosis.summary}"
            fields.append({"type": "mrkdwn", "text": diag_text})

        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
            {"type": "section", "fields": fields},
        ]

        if issues:
            issue_lines = [f"• `{i.code}` — {i.message}" for i in issues[:5]]
            if len(issues) > 5:
                issue_lines.append(f"• … 외 {len(issues) - 5}건")
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*이슈*\n" + "\n".join(issue_lines)},
                }
            )

        context_elements = [{"type": "mrkdwn", "text": f"run_id: `{run_id}`"}]
        if report_path:
            context_elements.append({"type": "mrkdwn", "text": f"보고서: `{report_path}`"})
        blocks.append({"type": "context", "elements": context_elements})

        # fallback text (모바일 알림, blocks 미지원 클라이언트용)
        if status == "success" and (stats.inserted > 0 or stats.updated > 0):
            text = f"{emoji} {site} 수집 완료 — 신규 {stats.inserted}건 · 변경 {stats.updated}건"
        elif status == "success":
            text = f"{emoji} {site} 수집 완료 — 변화 없음"
        else:
            text = f"{emoji} {site} 수집 실패 — {_diagnosis_label(diagnosis)}"
        return text, blocks

    # -------- daily summary --------

    def notify_daily_summary(self, summary: dict[str, Any], *, hours: int = 24) -> dict[str, Any] | None:
        """직전 N시간 운영 요약을 Slack에 게시."""
        if not self.enabled:
            logger.info(
                "slack disabled (no token/channel), skip daily summary",
                extra={"event": "slack_skipped", "mode": "daily_summary"},
            )
            return None

        text, blocks = self._build_summary_message(summary, hours=hours)
        return self._post(text=text, blocks=blocks, run_id="daily_summary", site="*")

    @staticmethod
    def _build_summary_message(
        summary: dict[str, Any], *, hours: int
    ) -> tuple[str, list[dict[str, Any]]]:
        by_site: dict[str, dict] = summary.get("by_site", {}) or {}

        if not by_site:
            text = f"⏳ scraper-ops 일일 요약 — 최근 {hours}시간: 실행 기록 없음"
            blocks = [
                {"type": "header", "text": {"type": "plain_text", "text": text[:150]}},
            ]
            return text, blocks

        total_runs = sum(s["runs"] for s in by_site.values())
        total_failed = sum(s["failed"] for s in by_site.values())
        total_inserted = sum(s["inserted"] for s in by_site.values())
        total_updated = sum(s["updated"] for s in by_site.values())

        emoji = "📊" if total_failed == 0 else "⚠️"
        header = f"{emoji} scraper-ops 일일 요약 — 최근 {hours}시간"

        lines: list[str] = []
        for site_name in sorted(by_site.keys()):
            s = by_site[site_name]
            mark = "✅" if s["failed"] == 0 else f"❌ 실패 {s['failed']}회"
            last_status_label = "성공" if s["last_status"] == "success" else (
                "실패" if s["last_status"] == "failed" else (s["last_status"] or "-")
            )
            lines.append(
                f"*{site_name}* {mark} · 실행 {s['runs']}회 · 신규 {s['inserted']}건 · 변경 {s['updated']}건\n"
                f"  (마지막: {last_status_label} @ {s['last_finished_at']})"
            )

        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"기간: {summary.get('since')} → {summary.get('until')}\n"
                            f"전체: 실행 {total_runs}회 · 실패 {total_failed}회 · "
                            f"신규 {total_inserted}건 · 변경 {total_updated}건"
                        ),
                    }
                ],
            },
        ]

        text = (
            f"{emoji} 일일 요약 {hours}시간 — 실행 {total_runs}회 · 실패 {total_failed}회 · "
            f"신규 {total_inserted}건 · 변경 {total_updated}건"
        )
        return text, blocks

    @staticmethod
    def _status_emoji(status: str, diagnosis: Diagnosis) -> str:
        if status == "success":
            return "✅"
        if diagnosis.category == FailureCategory.NETWORK_BLOCKED:
            return "🚫"
        if diagnosis.category == FailureCategory.SCHEMA_CHANGE:
            return "⚠️"
        return "❌"

    # -------- HTTP 호출 --------

    def _post(
        self,
        *,
        text: str,
        blocks: list[dict[str, Any]],
        run_id: str,
        site: str,
    ) -> dict[str, Any] | None:
        assert self.config is not None
        body = json.dumps(
            {"channel": self.config.channel_id, "text": text, "blocks": blocks},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            CHAT_POST_MESSAGE_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.config.bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8")
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            logger.error(
                "slack post failed",
                extra={"event": "slack_post_failed", "error": str(e), "site": site, "run_id": run_id},
            )
            return None

        try:
            data = json.loads(raw)
        except ValueError:
            data = {"ok": False, "raw": raw[:200]}

        if not data.get("ok"):
            logger.error(
                "slack api returned not-ok",
                extra={"event": "slack_not_ok", "response": data, "site": site, "run_id": run_id},
            )
        else:
            logger.info(
                "slack posted",
                extra={"event": "slack_posted", "ts": data.get("ts"), "site": site, "run_id": run_id},
            )
        return data
