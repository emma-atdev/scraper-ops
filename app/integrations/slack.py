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
        header = f"{emoji} `{site}` run {status} — {run_id}"

        fields = [
            {"type": "mrkdwn", "text": f"*Status*\n{status}"},
            {
                "type": "mrkdwn",
                "text": f"*Stats*\ninserted {stats.inserted} / updated {stats.updated} / unchanged {stats.unchanged}",
            },
        ]
        if diagnosis.category != FailureCategory.NONE:
            fields.append(
                {"type": "mrkdwn", "text": f"*Diagnosis*\n{diagnosis.category.value}\n{diagnosis.summary}"}
            )

        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": header[:150]}},
            {"type": "section", "fields": fields},
        ]

        if issues:
            issue_lines = [f"- `{i.code}` {i.message}" for i in issues[:5]]
            if len(issues) > 5:
                issue_lines.append(f"- ... +{len(issues) - 5} more")
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*Issues*\n" + "\n".join(issue_lines)},
                }
            )

        if report_path:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": f"report: `{report_path}`"},
                    ],
                }
            )

        # fallback text (mobile notification, blocks 미지원 클라이언트)
        text = f"{emoji} {site} {status} — i={stats.inserted} u={stats.updated} k={stats.unchanged}"
        return text, blocks

    @staticmethod
    def _status_emoji(status: str, diagnosis: Diagnosis) -> str:
        if status == "success":
            return ":white_check_mark:"
        if diagnosis.category == FailureCategory.NETWORK_BLOCKED:
            return ":no_entry:"
        if diagnosis.category == FailureCategory.SCHEMA_CHANGE:
            return ":warning:"
        return ":x:"

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
