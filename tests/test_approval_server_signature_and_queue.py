"""approval_server signature + queue 단위 테스트."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from app.approval_server import (
    DecisionQueue,
    SlackSignatureError,
    verify_slack_signature,
)


# ---------- signature ----------

SECRET = "test_signing_secret"


def _sign(body: bytes, ts: int) -> str:
    base = f"v0:{ts}:".encode() + body
    digest = hmac.new(SECRET.encode(), msg=base, digestmod=hashlib.sha256).hexdigest()
    return f"v0={digest}"


def test_verify_signature_accepts_valid():
    body = b"payload=abc"
    ts = int(time.time())
    sig = _sign(body, ts)
    verify_slack_signature(
        signing_secret=SECRET, request_body=body,
        timestamp_header=str(ts), signature_header=sig,
    )


def test_verify_signature_rejects_tampered_body():
    body = b"payload=abc"
    ts = int(time.time())
    sig = _sign(body, ts)
    with pytest.raises(SlackSignatureError):
        verify_slack_signature(
            signing_secret=SECRET, request_body=b"payload=tampered",
            timestamp_header=str(ts), signature_header=sig,
        )


def test_verify_signature_rejects_wrong_secret():
    body = b"payload=abc"
    ts = int(time.time())
    sig = _sign(body, ts)
    with pytest.raises(SlackSignatureError):
        verify_slack_signature(
            signing_secret="wrong", request_body=body,
            timestamp_header=str(ts), signature_header=sig,
        )


def test_verify_signature_rejects_old_timestamp():
    body = b"payload=abc"
    old_ts = int(time.time()) - 60 * 10  # 10분 전 (5분 tolerance 초과)
    sig = _sign(body, old_ts)
    with pytest.raises(SlackSignatureError):
        verify_slack_signature(
            signing_secret=SECRET, request_body=body,
            timestamp_header=str(old_ts), signature_header=sig,
        )


def test_verify_signature_rejects_missing_headers():
    with pytest.raises(SlackSignatureError):
        verify_slack_signature(
            signing_secret=SECRET, request_body=b"x",
            timestamp_header="", signature_header="",
        )


def test_verify_signature_rejects_empty_secret():
    with pytest.raises(SlackSignatureError):
        verify_slack_signature(
            signing_secret="", request_body=b"x",
            timestamp_header="123", signature_header="v0=abc",
        )


def test_verify_signature_rejects_invalid_timestamp_format():
    with pytest.raises(SlackSignatureError):
        verify_slack_signature(
            signing_secret=SECRET, request_body=b"x",
            timestamp_header="not_a_number", signature_header="v0=abc",
        )


def test_verify_signature_uses_now_for_test():
    """now 인자로 시각 주입 가능 (테스트용)."""
    body = b"x"
    ts = 1700000000
    sig = _sign(body, ts)
    verify_slack_signature(
        signing_secret=SECRET, request_body=body,
        timestamp_header=str(ts), signature_header=sig,
        now=ts + 1,
    )


# ---------- queue ----------

@pytest.fixture
def queue(tmp_path):
    return DecisionQueue(tmp_path / "queue.jsonl")


def test_enqueue_and_list_pending(queue):
    d1 = queue.enqueue(approval_id=7, kind="approve", by="slack:U1")
    d2 = queue.enqueue(approval_id=8, kind="reject", by="slack:U2", reason="별로")

    pending = queue.list_pending()
    assert len(pending) == 2
    assert {p.queue_id for p in pending} == {d1.queue_id, d2.queue_id}
    rejected = next(p for p in pending if p.kind == "reject")
    assert rejected.reason == "별로"


def test_ack_removes_from_pending(queue):
    d1 = queue.enqueue(approval_id=7, kind="approve", by="slack:U1")
    d2 = queue.enqueue(approval_id=8, kind="regenerate", by="slack:U2")

    queue.ack(d1.queue_id)
    pending = queue.list_pending()
    assert len(pending) == 1
    assert pending[0].queue_id == d2.queue_id


def test_ack_is_idempotent(queue):
    d1 = queue.enqueue(approval_id=7, kind="approve", by="slack:U1")
    queue.ack(d1.queue_id)
    queue.ack(d1.queue_id)  # 두 번 호출해도 OK
    assert queue.list_pending() == []


def test_list_pending_on_empty_queue_returns_empty(tmp_path):
    q = DecisionQueue(tmp_path / "fresh.jsonl")
    assert q.list_pending() == []


def test_queue_persists_across_instances(tmp_path):
    """다른 프로세스(다른 인스턴스)에서도 같은 결정 목록을 본다 — 운영 시나리오 시뮬레이션."""
    path = tmp_path / "queue.jsonl"
    q1 = DecisionQueue(path)
    d = q1.enqueue(approval_id=42, kind="approve", by="slack:U")

    q2 = DecisionQueue(path)  # Mac에서 polling agent가 새로 만든 인스턴스라고 가정
    pending = q2.list_pending()
    assert len(pending) == 1
    assert pending[0].queue_id == d.queue_id
    assert pending[0].approval_id == 42

    q2.ack(d.queue_id)

    q3 = DecisionQueue(path)
    assert q3.list_pending() == []


def test_unicode_korean_reason_preserved(queue):
    d = queue.enqueue(
        approval_id=1, kind="reject", by="slack:U",
        reason="응답이 차단 페이지로 보임",
    )
    pending = queue.list_pending()
    assert pending[0].reason == "응답이 차단 페이지로 보임"
