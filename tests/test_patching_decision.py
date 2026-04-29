"""patching.decision 테스트. rerun callback은 mock으로 주입."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from app.approval import create_approval
from app.llm import PatchCandidate, PatchOperation
from app.models import UpsertStats
from app.patching import (
    ApprovalNotPending,
    approve_and_apply,
    reject_decision,
)
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


@pytest.fixture
def configs_dir(tmp_path):
    d = tmp_path / "configs" / "sites"
    d.mkdir(parents=True)
    (d / "catch.yaml").write_text(CATCH_YAML, encoding="utf-8")
    return d


@pytest.fixture
def conn(tmp_path):
    c = open_connection(tmp_path / "test.db")
    init_schema(c)
    return c


@pytest.fixture
def slack():
    s = MagicMock()
    s.notify_apply_result.return_value = {"ok": True}
    s.notify_decision_result.return_value = {"ok": True}
    return s


def _patch():
    return PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(
            op="replace",
            path="collectors.jobs.mapping.items_path",
            old="recruitData",
            new="data.recruitData",
        )],
        reason="응답 wrapper 변경",
        risk="low",
    )


def _create_pending(conn) -> int:
    repo = ApprovalRepository(conn)
    return create_approval(
        repo, run_id="r1", site="catch",
        patch_json=_patch().model_dump_json(),
        dry_run_json=None,
    )


# ---------- approve_and_apply 정상 흐름 ----------

def test_approve_apply_rerun_success(conn, configs_dir, slack, tmp_path):
    approval_id = _create_pending(conn)

    rerun = MagicMock(return_value={
        "success": True,
        "stats": UpsertStats(inserted=47, updated=3),
        "error": None,
    })

    result = approve_and_apply(
        approval_id=approval_id, by="cli:tester",
        conn=conn, configs_dir=configs_dir, slack=slack,
        rerun_runner=rerun,
    )
    assert result["applied"] is True
    assert result["rerun_success"] is True
    assert result["rolled_back"] is False
    assert rerun.call_count == 1

    # yaml 변경 반영됨
    yaml_text = (configs_dir / "catch.yaml").read_text(encoding="utf-8")
    assert "data.recruitData" in yaml.safe_load(yaml_text)["collectors"]["jobs"]["mapping"]["items_path"]

    # approval row 상태
    row = ApprovalRepository(conn).get(approval_id)
    assert row.status == "approved"
    assert row.decided_by == "cli:tester"

    # slack thread reply 호출
    assert slack.notify_apply_result.call_count == 1
    kwargs = slack.notify_apply_result.call_args.kwargs
    assert kwargs["success"] is True
    assert kwargs["rerun_inserted"] == 47


def test_approve_apply_rerun_fail_rolls_back(conn, configs_dir, slack):
    """rerun 실패 시 yaml은 자동 롤백 (B-1)."""
    approval_id = _create_pending(conn)
    original = (configs_dir / "catch.yaml").read_text(encoding="utf-8")

    rerun = MagicMock(return_value={
        "success": False, "stats": None, "error": "사이트 응답 차단",
    })

    result = approve_and_apply(
        approval_id=approval_id, by="cli:tester",
        conn=conn, configs_dir=configs_dir, slack=slack,
        rerun_runner=rerun,
    )
    assert result["applied"] is True
    assert result["rerun_success"] is False
    assert result["rolled_back"] is True

    # 파일이 원래대로 복구
    restored = (configs_dir / "catch.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(restored)["collectors"]["jobs"]["mapping"]["items_path"] == "recruitData"

    # slack: success=False 메시지
    kwargs = slack.notify_apply_result.call_args.kwargs
    assert kwargs["success"] is False


def test_approve_yaml_validation_failure_no_rerun(conn, configs_dir, slack):
    """patch가 yaml 스키마 위반이면 적용 자체가 실패. rerun 호출 안 됨."""
    repo = ApprovalRepository(conn)
    bad_patch = PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(
            op="replace", path="collectors.jobs.type",
            old="api_jobs", new="weird_type",
        )],
        reason="x", risk="low",
    )
    approval_id = create_approval(
        repo, run_id="r1", site="catch",
        patch_json=bad_patch.model_dump_json(), dry_run_json=None,
    )

    rerun = MagicMock()
    result = approve_and_apply(
        approval_id=approval_id, by="cli:tester",
        conn=conn, configs_dir=configs_dir, slack=slack,
        rerun_runner=rerun,
    )
    assert result["applied"] is False
    assert rerun.call_count == 0
    # yaml 안 바뀜
    assert "weird_type" not in (configs_dir / "catch.yaml").read_text(encoding="utf-8")


# ---------- 잘못된 호출 ----------

def test_approve_unknown_id(conn, configs_dir, slack):
    with pytest.raises(LookupError):
        approve_and_apply(
            approval_id=99999, by="cli:x", conn=conn,
            configs_dir=configs_dir, slack=slack,
            rerun_runner=lambda *a, **k: {"success": True, "stats": UpsertStats(), "error": None},
        )


def test_approve_already_decided(conn, configs_dir, slack):
    approval_id = _create_pending(conn)
    rerun = MagicMock(return_value={"success": True, "stats": UpsertStats(inserted=1), "error": None})
    approve_and_apply(
        approval_id=approval_id, by="cli:t", conn=conn,
        configs_dir=configs_dir, slack=slack, rerun_runner=rerun,
    )
    # 두 번째 시도
    with pytest.raises(ApprovalNotPending):
        approve_and_apply(
            approval_id=approval_id, by="cli:t", conn=conn,
            configs_dir=configs_dir, slack=slack, rerun_runner=rerun,
        )


# ---------- reject_decision ----------

def test_reject_marks_rejected_and_replies(conn, configs_dir, slack):
    repo = ApprovalRepository(conn)
    approval_id = _create_pending(conn)
    repo.attach_slack(approval_id, channel="C123", thread_ts="1.234")

    reject_decision(
        approval_id=approval_id, by="cli:tester",
        reason="처방 미덥지 않음",
        conn=conn, slack=slack,
    )
    row = repo.get(approval_id)
    assert row.status == "rejected"
    assert row.decision_reason == "처방 미덥지 않음"

    assert slack.notify_decision_result.call_count == 1


def test_reject_already_decided_raises(conn, configs_dir, slack):
    approval_id = _create_pending(conn)
    reject_decision(
        approval_id=approval_id, by="cli:t", reason="x",
        conn=conn, slack=slack,
    )
    with pytest.raises(ApprovalNotPending):
        reject_decision(
            approval_id=approval_id, by="cli:t", reason="y",
            conn=conn, slack=slack,
        )


# ---------- D-1 가드 회귀 ----------

def test_recent_rejected_is_listable(conn):
    """list_recent_rejected가 reject 후 결과를 잡는다."""
    from datetime import datetime, timedelta, timezone

    repo = ApprovalRepository(conn)
    approval_id = _create_pending(conn)
    reject_decision(
        approval_id=approval_id, by="cli:t", reason="x",
        conn=conn, slack=MagicMock(),
    )

    since_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    found = repo.list_recent_rejected(site="catch", since_iso=since_iso)
    assert len(found) == 1
    assert found[0].id == approval_id
