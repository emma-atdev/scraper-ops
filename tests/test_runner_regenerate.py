"""regenerate_approval 흐름 테스트. LLM은 mock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.approval import create_approval
from app.llm import PatchCandidate, PatchOperation
from app.runner.healing_flow import (
    PreviousApprovalNotEligible,
    REGENERATE_LIMIT_PER_RUN,
    RegenerateLimitReached,
    regenerate_approval,
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
def yaml_file(tmp_path):
    p = tmp_path / "catch.yaml"
    p.write_text(CATCH_YAML, encoding="utf-8")
    return p


@pytest.fixture
def conn(tmp_path):
    c = open_connection(tmp_path / "test.db")
    init_schema(c)
    return c


@pytest.fixture
def approval_repo(conn):
    return ApprovalRepository(conn)


@pytest.fixture
def slack():
    s = MagicMock()
    s.notify_approval_request.return_value = {"ok": True, "ts": "2.345", "channel": "C123"}
    s.notify_decision_result.return_value = {"ok": True}
    s.notify_healing_unavailable.return_value = {"ok": True}
    return s


def _initial_patch():
    return PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(
            op="replace",
            path="collectors.jobs.mapping.items_path",
            old="recruitData", new="data.recruitData",
        )],
        reason="응답 wrapper가 한 단계 깊어짐 (이전 후보)",
        risk="low",
    )


def _alternative_patch():
    return PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(
            op="replace",
            path="collectors.jobs.mapping.items_path",
            old="recruitData", new="result.recruitData",
        )],
        reason="대안: result 키 사용",
        risk="low",
    )


def _seed_pending(approval_repo: ApprovalRepository, *, run_id="r1") -> int:
    return create_approval(
        approval_repo, run_id=run_id, site="catch",
        patch_json=_initial_patch().model_dump_json(),
        dry_run_json='{"verdict": "improved"}',
    )


def _make_client(patch: PatchCandidate) -> MagicMock:
    client = MagicMock()
    client.enabled = True
    client.parse.return_value = patch
    return client


# ---------- 정상 흐름 ----------

def test_regenerate_marks_prev_superseded_and_creates_new(
    yaml_file, conn, approval_repo, slack
):
    prev_id = _seed_pending(approval_repo)
    approval_repo.attach_slack(prev_id, channel="C123", thread_ts="1.111")

    client = _make_client(_alternative_patch())
    api_sample = {"data": {"recruitData": [
        {"RecruitID": 1, "RecruitTitle": "x", "CompName": "y"},
    ]}}

    new_id = regenerate_approval(
        prev_approval_id=prev_id, by="cli:tester",
        yaml_path=yaml_file,
        evidence_loader=lambda: (api_sample, None),
        approval_repo=approval_repo, slack=slack,
        db_conn=conn, llm_client=client,
    )

    assert new_id != prev_id
    prev_row = approval_repo.get(prev_id)
    new_row = approval_repo.get(new_id)
    assert prev_row.status == "superseded"
    assert prev_row.decided_by == "cli:tester"
    assert f"superseded_by={new_id}" in (prev_row.decision_reason or "")
    assert new_row.status == "pending"
    assert new_row.run_id == prev_row.run_id  # 같은 run_id 공유

    # LLM이 previous_attempts 받았는지 (호출 인자에 prompt가 있고 거기에 이전 후보 정보)
    call_kwargs = client.parse.call_args.kwargs
    assert "이전 후보" in call_kwargs["user_prompt"]
    assert "다른 접근" in call_kwargs["user_prompt"]

    # slack 메시지: 이전 thread에 supersede 안내 + 새 카드 게시
    assert slack.notify_decision_result.call_count == 1
    assert slack.notify_approval_request.call_count == 1
    assert new_row.slack_thread_ts == "2.345"


# ---------- 에러 케이스 ----------

def test_regenerate_unknown_id(yaml_file, conn, approval_repo, slack):
    with pytest.raises(PreviousApprovalNotEligible):
        regenerate_approval(
            prev_approval_id=99999, by="cli:t", yaml_path=yaml_file,
            evidence_loader=lambda: ({}, None),
            approval_repo=approval_repo, slack=slack, db_conn=conn,
            llm_client=_make_client(_alternative_patch()),
        )


def test_regenerate_already_decided(yaml_file, conn, approval_repo, slack):
    prev_id = _seed_pending(approval_repo)
    # 직접 reject로 만들기
    approval_repo.update_status(
        prev_id, new_status="rejected",
        decided_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        decided_by="cli:x", decision_reason="x",
    )
    with pytest.raises(PreviousApprovalNotEligible):
        regenerate_approval(
            prev_approval_id=prev_id, by="cli:t", yaml_path=yaml_file,
            evidence_loader=lambda: ({}, None),
            approval_repo=approval_repo, slack=slack, db_conn=conn,
            llm_client=_make_client(_alternative_patch()),
        )


def test_regenerate_limit_reached(yaml_file, conn, approval_repo, slack):
    """같은 run_id에 N개 approval 있으면 limit 도달."""
    # REGENERATE_LIMIT_PER_RUN(=3) 만큼 미리 생성
    for _ in range(REGENERATE_LIMIT_PER_RUN):
        create_approval(
            approval_repo, run_id="r1", site="catch",
            patch_json=_initial_patch().model_dump_json(), dry_run_json=None,
        )
    # 그중 마지막 하나는 pending이니까 그걸 prev로 잡고 regenerate 시도
    pendings = approval_repo.list_pending(site="catch")
    prev_id = pendings[-1].id

    with pytest.raises(RegenerateLimitReached):
        regenerate_approval(
            prev_approval_id=prev_id, by="cli:t", yaml_path=yaml_file,
            evidence_loader=lambda: ({"data": {"recruitData": []}}, None),
            approval_repo=approval_repo, slack=slack, db_conn=conn,
            llm_client=_make_client(_alternative_patch()),
        )


def test_regenerate_with_no_sample(yaml_file, conn, approval_repo, slack):
    prev_id = _seed_pending(approval_repo)
    with pytest.raises(PreviousApprovalNotEligible):
        regenerate_approval(
            prev_approval_id=prev_id, by="cli:t", yaml_path=yaml_file,
            evidence_loader=lambda: (None, None),
            approval_repo=approval_repo, slack=slack, db_conn=conn,
            llm_client=_make_client(_alternative_patch()),
        )


def test_regenerate_empty_changes_supersedes_only(
    yaml_file, conn, approval_repo, slack
):
    """LLM이 빈 changes 줘도 이전 approval은 supersede되고 새 approval은 안 만듦."""
    prev_id = _seed_pending(approval_repo)
    empty = PatchCandidate(
        file="configs/sites/catch.yaml", changes=[],
        reason="다른 접근도 어려움", risk="high",
    )
    client = _make_client(empty)

    with pytest.raises(PreviousApprovalNotEligible):
        regenerate_approval(
            prev_approval_id=prev_id, by="cli:t", yaml_path=yaml_file,
            evidence_loader=lambda: ({"data": {"recruitData": []}}, None),
            approval_repo=approval_repo, slack=slack, db_conn=conn,
            llm_client=client,
        )

    # 이전 approval은 superseded로 마감
    assert approval_repo.get(prev_id).status == "superseded"
    # 새 approval은 안 만들어짐
    assert approval_repo.list_pending(site="catch") == []
    # healing_unavailable 알림 발송
    assert slack.notify_healing_unavailable.call_count == 1
