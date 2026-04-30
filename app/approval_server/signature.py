"""Slack request signature 검증.

Slack은 모든 inbound 요청에 HMAC-SHA256 서명을 붙인다. 우리 서버는 SLACK_SIGNING_SECRET으로
재계산해 일치하는지 확인한다. 검증 실패 = 요청 거부.

근거: https://api.slack.com/authentication/verifying-requests-from-slack
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_VERSION = "v0"
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 60 * 5  # ±5분


class SlackSignatureError(RuntimeError):
    """signature 검증 실패. 401로 거부할 케이스."""


def verify_slack_signature(
    *,
    signing_secret: str,
    request_body: bytes,
    timestamp_header: str,
    signature_header: str,
    now: float | None = None,
    tolerance_seconds: int = DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
) -> None:
    """Slack에서 온 요청인지 검증. 실패 시 SlackSignatureError raise.

    Args:
        signing_secret: env에서 읽은 SLACK_SIGNING_SECRET.
        request_body: 원본 raw body (parsed JSON 아님).
        timestamp_header: X-Slack-Request-Timestamp 값.
        signature_header: X-Slack-Signature 값. "v0=..." 형식.
        now: 테스트용 현재 시각. None이면 time.time().
        tolerance_seconds: 허용 시간차 (replay attack 방어).
    """
    if not signing_secret:
        raise SlackSignatureError("signing_secret not configured")
    if not timestamp_header or not signature_header:
        raise SlackSignatureError("missing timestamp or signature header")

    try:
        ts_value = int(timestamp_header)
    except (TypeError, ValueError) as e:
        raise SlackSignatureError(f"invalid timestamp: {timestamp_header!r}") from e

    current = now if now is not None else time.time()
    if abs(current - ts_value) > tolerance_seconds:
        raise SlackSignatureError(
            f"timestamp out of tolerance: header={ts_value}, now={current:.0f}"
        )

    base_string = f"{SIGNATURE_VERSION}:{ts_value}:".encode() + request_body
    digest = hmac.new(
        signing_secret.encode("utf-8"),
        msg=base_string,
        digestmod=hashlib.sha256,
    ).hexdigest()
    expected = f"{SIGNATURE_VERSION}={digest}"

    if not hmac.compare_digest(expected, signature_header):
        raise SlackSignatureError("signature mismatch")
