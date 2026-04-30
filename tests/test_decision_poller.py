"""decision_poller 통합 테스트. VM 서버를 띄우고 Mac side polling이 dispatch까지 가는지."""

from __future__ import annotations

import socket
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.approval import create_approval
from app.approval_server.queue import DecisionQueue
from app.approval_server.server import make_handler
from app.llm import PatchCandidate, PatchOperation
from app.models import UpsertStats
from app.runner.decision_poller import poll_and_dispatch
from app.storage import ApprovalRepository, init_schema, open_connection


CATCH_YAML = """site: catch
name: catch.co.kr
enabled: true
runtime:
  preferred_environment: local
collectors:
  jobs:
    type: api_jobs
    fetcher: http
    purpose: postings
    request:
      method: GET
      url: https://x.test/api
      headers: {}
      params: {}
    pagination:
      type: page
      param: curpage
      start: 1
      max_pages: 2
      stop_condition: empty_items
    mapping:
      items_path: recruitData
      fields:
        external_id: RecruitID
        title: RecruitTitle
        company: CompName
    validation:
      required_fields:
        - external_id
        - title
        - company
"""

TOKEN = "poller-token-x"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server_and_queue(tmp_path):
    port = _free_port()
    queue = DecisionQueue(tmp_path / "queue.jsonl")
    handler_cls = make_handler(
        queue=queue, slack_signing_secret="unused", poller_token=TOKEN,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"queue": queue, "url": f"http://127.0.0.1:{port}"}
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def mac_setup(tmp_path):
    """Mac 쪽: DB + configs/sites/catch.yaml + data 디렉토리."""
    configs_dir = tmp_path / "configs" / "sites"
    configs_dir.mkdir(parents=True)
    (configs_dir / "catch.yaml").write_text(CATCH_YAML, encoding="utf-8")

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    conn = open_connection(tmp_path / "scraper.db")
    init_schema(conn)
    return {"conn": conn, "configs_dir": configs_dir, "data_dir": data_dir}


def _seed_pending(conn) -> int:
    repo = ApprovalRepository(conn)
    patch = PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(
            op="replace", path="collectors.jobs.mapping.items_path",
            old="recruitData", new="data.recruitData",
        )],
        reason="test", risk="low",
    )
    return create_approval(
        repo, run_id="r1", site="catch",
        patch_json=patch.model_dump_json(), dry_run_json=None,
    )


# ---------- approve dispatch ----------

def test_polling_dispatches_approve_and_acks(server_and_queue, mac_setup):
    approval_id = _seed_pending(mac_setup["conn"])
    server_and_queue["queue"].enqueue(
        approval_id=approval_id, kind="approve", by="slack:U1",
    )

    rerun_runner = MagicMock(return_value={
        "success": True, "stats": UpsertStats(inserted=10), "error": None,
    })

    counts = poll_and_dispatch(
        server_url=server_and_queue["url"], poller_token=TOKEN,
        conn=mac_setup["conn"], configs_dir=mac_setup["configs_dir"],
        data_dir=mac_setup["data_dir"], site_runner=rerun_runner,
    )
    assert counts == {"approved": 1, "rejected": 0, "regenerated": 0, "failed": 0, "total": 1}

    # ack 됐는지 — 큐에 더 이상 pending 없음
    assert server_and_queue["queue"].list_pending() == []

    # Mac DB에 approved 반영
    row = ApprovalRepository(mac_setup["conn"]).get(approval_id)
    assert row.status == "approved"
    assert row.decided_by == "slack:U1"


def test_polling_dispatches_reject(server_and_queue, mac_setup):
    approval_id = _seed_pending(mac_setup["conn"])
    server_and_queue["queue"].enqueue(
        approval_id=approval_id, kind="reject", by="slack:U2",
        reason="rejected from slack",
    )

    counts = poll_and_dispatch(
        server_url=server_and_queue["url"], poller_token=TOKEN,
        conn=mac_setup["conn"], configs_dir=mac_setup["configs_dir"],
        data_dir=mac_setup["data_dir"], site_runner=MagicMock(),
    )
    assert counts["rejected"] == 1
    assert ApprovalRepository(mac_setup["conn"]).get(approval_id).status == "rejected"
    assert server_and_queue["queue"].list_pending() == []


def test_polling_skips_already_decided_but_acks(server_and_queue, mac_setup):
    """이미 결정된 approval에 대한 큐 이벤트는 ack만 하고 failed로 카운트."""
    approval_id = _seed_pending(mac_setup["conn"])
    # 직접 reject로 만들기 (DB에서)
    from datetime import datetime, timezone

    ApprovalRepository(mac_setup["conn"]).update_status(
        approval_id, new_status="rejected",
        decided_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        decided_by="cli:other", decision_reason="x",
    )

    server_and_queue["queue"].enqueue(
        approval_id=approval_id, kind="approve", by="slack:U",
    )

    counts = poll_and_dispatch(
        server_url=server_and_queue["url"], poller_token=TOKEN,
        conn=mac_setup["conn"], configs_dir=mac_setup["configs_dir"],
        data_dir=mac_setup["data_dir"], site_runner=MagicMock(),
    )
    assert counts["failed"] == 1
    assert server_and_queue["queue"].list_pending() == []  # ack됨


def test_polling_with_no_decisions_returns_zero(server_and_queue, mac_setup):
    counts = poll_and_dispatch(
        server_url=server_and_queue["url"], poller_token=TOKEN,
        conn=mac_setup["conn"], configs_dir=mac_setup["configs_dir"],
        data_dir=mac_setup["data_dir"], site_runner=MagicMock(),
    )
    assert counts["total"] == 0
    assert counts["approved"] == 0


def test_polling_with_unreachable_server_returns_zero(mac_setup):
    """서버 다운 시 polling은 조용히 빈 list 반환."""
    counts = poll_and_dispatch(
        server_url="http://127.0.0.1:1",  # 도달 불가 포트
        poller_token=TOKEN,
        conn=mac_setup["conn"], configs_dir=mac_setup["configs_dir"],
        data_dir=mac_setup["data_dir"], site_runner=MagicMock(),
        timeout=0.5,
    )
    assert counts["total"] == 0
