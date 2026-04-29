"""approval_request: Repository CRUD + state machine 테스트."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.approval import (
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    approve,
    create_approval,
    expire_due,
    reject,
)
from app.storage import ApprovalRepository, init_schema, open_connection


@pytest.fixture
def repo(tmp_path):
    conn = open_connection(tmp_path / "test.db")
    init_schema(conn)
    return ApprovalRepository(conn)


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "audit.log"


def _make_patch_json() -> str:
    return json.dumps({
        "file": "configs/sites/catch.yaml",
        "changes": [{"op": "replace", "path": "x", "old": "a", "new": "b"}],
        "reason": "test",
        "risk": "low",
    })


# ---------- Repository CRUD ----------

def test_insert_and_get_round_trip(repo):
    now = datetime.now(timezone.utc)
    new_id = repo.insert(
        run_id="run-1", site="catch",
        patch_json=_make_patch_json(), dry_run_json='{"verdict": "improved"}',
        created_at=now.isoformat(timespec="seconds"),
        expires_at=(now + timedelta(hours=24)).isoformat(timespec="seconds"),
    )
    fetched = repo.get(new_id)
    assert fetched is not None
    assert fetched.id == new_id
    assert fetched.site == "catch"
    assert fetched.status == "pending"
    assert "improved" in fetched.dry_run_json


def test_get_returns_none_for_unknown_id(repo):
    assert repo.get(99999) is None


def test_list_pending_filters_by_site(repo):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    later = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
    repo.insert(run_id="r1", site="catch", patch_json="{}", dry_run_json=None,
                created_at=now, expires_at=later)
    repo.insert(run_id="r2", site="other", patch_json="{}", dry_run_json=None,
                created_at=now, expires_at=later)

    assert len(repo.list_pending()) == 2
    assert len(repo.list_pending(site="catch")) == 1
    assert repo.list_pending(site="catch")[0].site == "catch"


def test_attach_slack(repo):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    later = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")
    new_id = repo.insert(run_id="r1", site="catch", patch_json="{}", dry_run_json=None,
                         created_at=now, expires_at=later)
    repo.attach_slack(new_id, channel="C123", thread_ts="1234.5678")
    row = repo.get(new_id)
    assert row.slack_channel == "C123"
    assert row.slack_thread_ts == "1234.5678"


# ---------- create + decide via state machine ----------

def test_create_approval_writes_audit(repo, audit_path):
    new_id = create_approval(
        repo, run_id="run-1", site="catch",
        patch_json=_make_patch_json(), dry_run_json='{"verdict": "improved"}',
        audit_path=audit_path,
    )
    assert new_id > 0
    row = repo.get(new_id)
    assert row.status == "pending"

    line = audit_path.read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(line)
    assert payload["event"] == "approval_created"
    assert payload["id"] == new_id


def test_approve_transitions_pending_to_approved(repo, audit_path):
    new_id = create_approval(
        repo, run_id="r1", site="catch",
        patch_json=_make_patch_json(), dry_run_json=None,
        audit_path=audit_path,
    )
    approve(repo, new_id, by="cli:tester", audit_path=audit_path)

    row = repo.get(new_id)
    assert row.status == "approved"
    assert row.decided_by == "cli:tester"
    assert row.decided_at is not None

    events = [json.loads(l)["event"] for l in audit_path.read_text(encoding="utf-8").splitlines()]
    assert events == ["approval_created", "approval_approved"]


def test_reject_requires_reason_and_records_it(repo, audit_path):
    new_id = create_approval(
        repo, run_id="r1", site="catch",
        patch_json=_make_patch_json(), dry_run_json=None,
        audit_path=audit_path,
    )
    reject(repo, new_id, by="slack:U1", reason="응답이 차단 페이지로 보임",
           audit_path=audit_path)

    row = repo.get(new_id)
    assert row.status == "rejected"
    assert row.decision_reason == "응답이 차단 페이지로 보임"


def test_double_decide_raises(repo, audit_path):
    new_id = create_approval(
        repo, run_id="r1", site="catch",
        patch_json=_make_patch_json(), dry_run_json=None,
        audit_path=audit_path,
    )
    approve(repo, new_id, by="cli:tester", audit_path=audit_path)
    with pytest.raises(ApprovalAlreadyDecided):
        approve(repo, new_id, by="cli:tester", audit_path=audit_path)
    with pytest.raises(ApprovalAlreadyDecided):
        reject(repo, new_id, by="cli:tester", reason="too late", audit_path=audit_path)


def test_decide_unknown_id_raises(repo, audit_path):
    with pytest.raises(ApprovalNotFound):
        approve(repo, 99999, by="cli:tester", audit_path=audit_path)


# ---------- A-1: 같은 run_id에 여러 pending 허용 ----------

def test_multiple_pending_for_same_run_id_allowed(repo, audit_path):
    """A-1: LLM이 같은 실패에 다른 후보 patch를 또 만든 경우, 둘 다 살아 있어야 한다."""
    id1 = create_approval(repo, run_id="r1", site="catch",
                          patch_json=_make_patch_json(), dry_run_json=None,
                          audit_path=audit_path)
    id2 = create_approval(repo, run_id="r1", site="catch",
                          patch_json=_make_patch_json(), dry_run_json=None,
                          audit_path=audit_path)
    pending = repo.list_pending(site="catch")
    assert {p.id for p in pending} == {id1, id2}


# ---------- expire_due ----------

def test_expire_due_picks_only_overdue(repo, audit_path):
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    future = (now + timedelta(hours=1)).isoformat(timespec="seconds")
    created = now.isoformat(timespec="seconds")

    id_old = repo.insert(run_id="r1", site="catch", patch_json="{}", dry_run_json=None,
                         created_at=created, expires_at=past)
    id_new = repo.insert(run_id="r2", site="catch", patch_json="{}", dry_run_json=None,
                         created_at=created, expires_at=future)

    expired = expire_due(repo, audit_path=audit_path)
    assert expired == [id_old]

    assert repo.get(id_old).status == "expired"
    assert repo.get(id_old).decided_by == "system:expiry"
    assert repo.get(id_new).status == "pending"


def test_expire_due_skips_already_decided(repo, audit_path):
    """이미 approved/rejected된 row는 expire 대상이 아님."""
    now = datetime.now(timezone.utc)
    past = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    created = now.isoformat(timespec="seconds")

    new_id = repo.insert(run_id="r1", site="catch", patch_json="{}", dry_run_json=None,
                         created_at=created, expires_at=past)
    approve(repo, new_id, by="cli:tester", audit_path=audit_path)

    expired = expire_due(repo, audit_path=audit_path)
    assert expired == []
    assert repo.get(new_id).status == "approved"  # 안 바뀜


def test_expire_due_no_pending_returns_empty(repo, audit_path):
    assert expire_due(repo, audit_path=audit_path) == []


# ---------- 24h 디폴트 만료 ----------

# ---------- M6.6b: supersede ----------

def test_supersede_transitions_to_superseded(repo, audit_path):
    from app.approval import supersede

    new_id = create_approval(
        repo, run_id="r1", site="catch",
        patch_json=_make_patch_json(), dry_run_json=None,
        audit_path=audit_path,
    )
    supersede(repo, new_id, by="cli:tester", superseded_by_id=42, audit_path=audit_path)

    row = repo.get(new_id)
    assert row.status == "superseded"
    assert row.decided_by == "cli:tester"
    assert "superseded_by=42" in (row.decision_reason or "")


def test_count_for_run(repo, audit_path):
    create_approval(repo, run_id="r1", site="catch",
                    patch_json="{}", dry_run_json=None, audit_path=audit_path)
    create_approval(repo, run_id="r1", site="catch",
                    patch_json="{}", dry_run_json=None, audit_path=audit_path)
    create_approval(repo, run_id="r2", site="catch",
                    patch_json="{}", dry_run_json=None, audit_path=audit_path)
    assert repo.count_for_run("r1") == 2
    assert repo.count_for_run("r2") == 1
    assert repo.count_for_run("nope") == 0


def test_migration_keeps_existing_pending_status(tmp_path):
    """기존 4종 status DB가 init_schema 두 번 호출돼도 데이터·status 보존."""
    from app.storage import init_schema, open_connection

    db_path = tmp_path / "test.db"
    conn = open_connection(db_path)
    init_schema(conn)

    repo_local = ApprovalRepository(conn)
    new_id = create_approval(
        repo_local, run_id="r1", site="catch",
        patch_json="{}", dry_run_json=None,
    )
    assert repo_local.get(new_id).status == "pending"

    # 두 번째 init은 no-op이어야 함 (이미 superseded 들어 있음)
    init_schema(conn)
    assert repo_local.get(new_id).status == "pending"


def test_create_approval_default_expiry_is_24h(repo, audit_path):
    new_id = create_approval(
        repo, run_id="r1", site="catch",
        patch_json="{}", dry_run_json=None,
        audit_path=audit_path,
    )
    row = repo.get(new_id)
    created = datetime.fromisoformat(row.created_at)
    expires = datetime.fromisoformat(row.expires_at)
    delta = expires - created
    # 정확히 24h ± 1초 (datetime.now 호출 두 번 사이의 미세 오차)
    assert abs(delta - timedelta(hours=24)).total_seconds() < 2
