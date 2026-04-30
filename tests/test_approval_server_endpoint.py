"""approval_server endpoint 테스트. 실제 ThreadingHTTPServer를 띄워 HTTP 호출."""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from app.approval_server.queue import DecisionQueue
from app.approval_server.server import make_handler
from http.server import ThreadingHTTPServer


SECRET = "test_signing_secret"
TOKEN = "test_poller_token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    port = _free_port()
    queue = DecisionQueue(tmp_path / "queue.jsonl")
    handler_cls = make_handler(
        queue=queue, slack_signing_secret=SECRET, poller_token=TOKEN,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"port": port, "queue": queue, "url": f"http://127.0.0.1:{port}"}
    httpd.shutdown()
    httpd.server_close()


def _slack_post(url_root: str, payload: dict, *, ts: int | None = None,
                signing_secret: str = SECRET):
    body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode("utf-8")
    ts = ts if ts is not None else int(time.time())
    base = f"v0:{ts}:".encode() + body
    digest = hmac.new(signing_secret.encode(), msg=base, digestmod=hashlib.sha256).hexdigest()
    sig = f"v0={digest}"
    req = urllib.request.Request(
        url_root + "/slack/actions",
        data=body, method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": str(ts),
            "X-Slack-Signature": sig,
        },
    )
    return urllib.request.urlopen(req, timeout=2)


def _slack_payload(action_id: str, value: str, user_id: str = "U111") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": user_id, "name": "tester"},
        "actions": [{"action_id": action_id, "value": value}],
    }


# ---------- Slack inbound ----------

def test_slack_button_enqueues_approve(server):
    resp = _slack_post(server["url"], _slack_payload("approve_button", "7"))
    assert resp.status == 200

    pending = server["queue"].list_pending()
    assert len(pending) == 1
    assert pending[0].kind == "approve"
    assert pending[0].approval_id == 7
    assert pending[0].by == "slack:U111"


def test_slack_button_enqueues_reject_with_default_reason(server):
    _slack_post(server["url"], _slack_payload("reject_button", "8"))
    pending = server["queue"].list_pending()
    assert pending[0].kind == "reject"
    assert pending[0].reason == "rejected from slack"


def test_slack_button_enqueues_regenerate(server):
    _slack_post(server["url"], _slack_payload("regenerate_button", "9"))
    pending = server["queue"].list_pending()
    assert pending[0].kind == "regenerate"


def test_slack_signature_mismatch_returns_401(server):
    body = urllib.parse.urlencode({"payload": "{}"}).encode("utf-8")
    req = urllib.request.Request(
        server["url"] + "/slack/actions",
        data=body, method="POST",
        headers={
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=deadbeef",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=2)
    assert exc.value.code == 401


def test_unknown_action_id_returns_400(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _slack_post(server["url"], _slack_payload("weird_button", "1"))
    assert exc.value.code == 400


# ---------- polling endpoints ----------

def test_pending_requires_poller_token(server):
    req = urllib.request.Request(server["url"] + "/decisions/pending", method="GET")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=2)
    assert exc.value.code == 401


def test_pending_returns_enqueued_decisions(server):
    server["queue"].enqueue(approval_id=10, kind="approve", by="slack:U")
    server["queue"].enqueue(approval_id=11, kind="reject", by="slack:U")

    req = urllib.request.Request(
        server["url"] + "/decisions/pending", method="GET",
        headers={"X-Poller-Token": TOKEN},
    )
    resp = urllib.request.urlopen(req, timeout=2)
    body = json.loads(resp.read().decode())
    assert len(body["decisions"]) == 2


def test_ack_removes_decision_from_pending(server):
    d = server["queue"].enqueue(approval_id=10, kind="approve", by="slack:U")
    req = urllib.request.Request(
        server["url"] + f"/decisions/{d.queue_id}/ack",
        method="POST", data=b"",
        headers={"X-Poller-Token": TOKEN},
    )
    resp = urllib.request.urlopen(req, timeout=2)
    assert resp.status == 200
    assert server["queue"].list_pending() == []


def test_healthz(server):
    resp = urllib.request.urlopen(server["url"] + "/healthz", timeout=2)
    assert resp.status == 200
    body = json.loads(resp.read().decode())
    assert body == {"ok": True}


def test_unknown_path_returns_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(server["url"] + "/nope", timeout=2)
    assert exc.value.code == 404
