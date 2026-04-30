"""Slack inbound endpoint + Mac polling endpoint.

엔드포인트:
- POST /slack/actions       — 슬랙 버튼 클릭 payload 받기
- GET  /decisions/pending   — Mac이 폴링해서 미처리 결정 가져가기
- POST /decisions/{qid}/ack — Mac이 처리 완료 후 ack
- GET  /healthz             — 헬스체크

stdlib만 사용. 서명 검증은 SLACK_SIGNING_SECRET, polling 인증은 POLLER_TOKEN으로.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app.approval_server.queue import DecisionQueue
from app.approval_server.signature import (
    SlackSignatureError,
    verify_slack_signature,
)

logger = logging.getLogger("scraper.approval_server")

DEFAULT_PORT = 8765
DEFAULT_QUEUE_PATH = Path("data/approval_queue.jsonl")


def make_handler(
    *,
    queue: DecisionQueue,
    slack_signing_secret: str,
    poller_token: str,
):
    """closure로 의존성 주입한 핸들러 클래스 반환."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A003
            logger.info("http %s", format % args, extra={"event": "http_log"})

        # ---- Slack inbound ----
        def do_POST(self):  # noqa: N802
            if self.path == "/slack/actions":
                self._handle_slack_actions()
            elif self.path.startswith("/decisions/") and self.path.endswith("/ack"):
                self._handle_ack()
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_GET(self):  # noqa: N802
            if self.path == "/decisions/pending":
                self._handle_pending()
            elif self.path == "/healthz":
                self._send_json(HTTPStatus.OK, {"ok": True})
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        # ---- handlers ----
        def _handle_slack_actions(self):
            body = self._read_body()
            try:
                verify_slack_signature(
                    signing_secret=slack_signing_secret,
                    request_body=body,
                    timestamp_header=self.headers.get("X-Slack-Request-Timestamp", ""),
                    signature_header=self.headers.get("X-Slack-Signature", ""),
                )
            except SlackSignatureError as e:
                logger.warning("slack signature rejected", extra={"error": str(e)})
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "signature"})
                return

            payload = self._parse_slack_payload(body)
            if payload is None:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid payload"})
                return

            try:
                action = payload["actions"][0]
                action_id = action["action_id"]
                approval_id = int(action["value"])
                user_id = payload["user"]["id"]
            except (KeyError, IndexError, TypeError, ValueError) as e:
                logger.warning("payload parse failed", extra={"error": str(e)})
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing fields"})
                return

            kind = _action_id_to_kind(action_id)
            if kind is None:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"unknown action_id: {action_id}"})
                return

            # reject은 reason이 필요. 슬랙 버튼은 reason 입력을 받지 않으니 디폴트.
            reason = "rejected from slack" if kind == "reject" else None
            queue.enqueue(
                approval_id=approval_id, kind=kind,
                by=f"slack:{user_id}", reason=reason,
            )
            # 슬랙은 3초 안에 응답해야 함. ephemeral 메시지로 처리 중 안내.
            self._send_json(
                HTTPStatus.OK,
                {"text": f"#{approval_id} {kind} 요청 큐에 등록. Mac에서 곧 처리합니다."},
            )

        def _handle_pending(self):
            if not self._authorize_poller():
                return
            pending = queue.list_pending()
            self._send_json(
                HTTPStatus.OK,
                {"decisions": [asdict(d) for d in pending]},
            )

        def _handle_ack(self):
            if not self._authorize_poller():
                return
            qid = self.path.split("/")[2]
            queue.ack(qid)
            self._send_json(HTTPStatus.OK, {"ack": qid})

        # ---- helpers ----
        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or 0)
            return self.rfile.read(length) if length > 0 else b""

        def _send_json(self, status: HTTPStatus, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorize_poller(self) -> bool:
            token = self.headers.get("X-Poller-Token", "")
            if token != poller_token:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return False
            return True

        @staticmethod
        def _parse_slack_payload(raw: bytes) -> dict | None:
            """Slack interactive payload는 form-urlencoded 안에 'payload' 키로 JSON이 들어옴."""
            try:
                form = urllib.parse.parse_qs(raw.decode("utf-8"))
                payload_str = form.get("payload", [""])[0]
                if not payload_str:
                    return None
                return json.loads(payload_str)
            except (ValueError, UnicodeDecodeError):
                return None

    return Handler


def _action_id_to_kind(action_id: str) -> str | None:
    mapping = {
        "approve_button": "approve",
        "reject_button": "reject",
        "regenerate_button": "regenerate",
    }
    return mapping.get(action_id)


def serve(
    *,
    port: int | None = None,
    queue_path: Path | None = None,
    slack_signing_secret: str | None = None,
    poller_token: str | None = None,
):
    port = port or int(os.environ.get("APPROVAL_SERVER_PORT", DEFAULT_PORT))
    queue_path = queue_path or DEFAULT_QUEUE_PATH
    slack_signing_secret = slack_signing_secret or os.environ.get("SLACK_SIGNING_SECRET", "")
    poller_token = poller_token or os.environ.get("POLLER_TOKEN", "")

    if not slack_signing_secret:
        raise RuntimeError("SLACK_SIGNING_SECRET not set")
    if not poller_token:
        raise RuntimeError("POLLER_TOKEN not set")

    queue = DecisionQueue(queue_path)
    handler_cls = make_handler(
        queue=queue,
        slack_signing_secret=slack_signing_secret,
        poller_token=poller_token,
    )
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
    logger.info("approval_server listening", extra={"event": "server_start", "port": port})
    print(f"[approval_server] listening on 0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    serve()
