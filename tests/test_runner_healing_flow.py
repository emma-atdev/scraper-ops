"""runner의 maybe_trigger_healing 트리거 조건 테스트.

LLM은 mock으로 주입. dry_run은 실제 함수 호출 (FakeFetcher).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.diagnosis import Diagnosis, FailureCategory
from app.llm import PatchCandidate, PatchOperation
from app.runner.healing_flow import maybe_trigger_healing
from app.storage import ApprovalRepository, Repository, init_schema, open_connection


YAML_TEXT = """
site: catch
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
      required_fields: [external_id, title, company]
"""


@pytest.fixture
def yaml_file(tmp_path):
    p = tmp_path / "catch.yaml"
    p.write_text(YAML_TEXT, encoding="utf-8")
    return p


@pytest.fixture
def db_conn(tmp_path):
    conn = open_connection(tmp_path / "test.db")
    init_schema(conn)
    return conn


@pytest.fixture
def repo(db_conn):
    return Repository(db_conn)


@pytest.fixture
def approval_repo(db_conn):
    return ApprovalRepository(db_conn)


@pytest.fixture
def slack():
    s = MagicMock()
    s.notify_approval_request.return_value = {"ok": True, "ts": "1.234", "channel": "C123"}
    s.notify_healing_unavailable.return_value = {"ok": True, "ts": "1.0"}
    return s


def _diag(category: FailureCategory) -> Diagnosis:
    return Diagnosis(category=category, summary="test", issue_codes=[])


def _seed_successful_history(repo: Repository):
    """과거 성공 run을 한 건 만들어서 신규 사이트 가드 통과시키기."""
    repo.start_run("catch", "past-run")
    from app.models import UpsertStats

    repo.finish_run("past-run", status="success", stats=UpsertStats(inserted=10))


def _make_patch_replace_items_path():
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


def _make_empty_patch():
    return PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[],
        reason="Red Zone 가능성, 사람 점검 필요",
        risk="high",
    )


def _make_llm_client(patch: PatchCandidate) -> MagicMock:
    """LLMClient 호환 mock: enabled=True, parse는 patch 반환."""
    client = MagicMock()
    client.enabled = True
    client.parse.return_value = patch
    return client


# ---------- 트리거 조건들 ----------

def test_skip_when_category_not_healable(yaml_file, repo, approval_repo, slack, db_conn):
    _seed_successful_history(repo)
    client = _make_llm_client(_make_patch_replace_items_path())

    maybe_trigger_healing(
        site="catch", site_run_id="r1", yaml_path=yaml_file,
        diagnosis=_diag(FailureCategory.NETWORK_BLOCKED),
        api_sample={"data": {"recruitData": [{"RecruitID": 1}]}},
        api_sample_prev=None,
        repo=repo, approval_repo=approval_repo, slack=slack,
        db_conn=db_conn, llm_client=client,
    )
    assert client.parse.call_count == 0  # LLM 안 부름
    assert slack.notify_approval_request.call_count == 0
    assert approval_repo.list_pending() == []


def test_skip_when_no_successful_history(yaml_file, repo, approval_repo, slack, db_conn):
    """신규 사이트 (성공 run 0건) → healing 스킵."""
    client = _make_llm_client(_make_patch_replace_items_path())
    maybe_trigger_healing(
        site="catch", site_run_id="r1", yaml_path=yaml_file,
        diagnosis=_diag(FailureCategory.SCHEMA_CHANGE),
        api_sample={"data": {"recruitData": [{"RecruitID": 1}]}},
        api_sample_prev=None,
        repo=repo, approval_repo=approval_repo, slack=slack,
        db_conn=db_conn, llm_client=client,
    )
    assert client.parse.call_count == 0
    assert approval_repo.list_pending() == []


def test_skip_when_pending_approval_exists(
    yaml_file, repo, approval_repo, slack, db_conn
):
    """이미 site에 pending이 있으면 새 healing 호출 안 함."""
    _seed_successful_history(repo)
    # 미리 pending 한 건 직접 생성
    from app.approval import create_approval

    create_approval(
        approval_repo, run_id="prev", site="catch",
        patch_json="{}", dry_run_json=None,
    )

    client = _make_llm_client(_make_patch_replace_items_path())
    maybe_trigger_healing(
        site="catch", site_run_id="r1", yaml_path=yaml_file,
        diagnosis=_diag(FailureCategory.SCHEMA_CHANGE),
        api_sample={"data": {"recruitData": [{"RecruitID": 1}]}},
        api_sample_prev=None,
        repo=repo, approval_repo=approval_repo, slack=slack,
        db_conn=db_conn, llm_client=client,
    )
    assert client.parse.call_count == 0
    # pending은 1건 그대로
    assert len(approval_repo.list_pending(site="catch")) == 1


def test_skip_when_no_api_sample(yaml_file, repo, approval_repo, slack, db_conn):
    _seed_successful_history(repo)
    client = _make_llm_client(_make_patch_replace_items_path())
    maybe_trigger_healing(
        site="catch", site_run_id="r1", yaml_path=yaml_file,
        diagnosis=_diag(FailureCategory.SCHEMA_CHANGE),
        api_sample=None, api_sample_prev=None,
        repo=repo, approval_repo=approval_repo, slack=slack,
        db_conn=db_conn, llm_client=client,
    )
    assert client.parse.call_count == 0


def test_skip_when_llm_disabled(yaml_file, repo, approval_repo, slack, db_conn):
    _seed_successful_history(repo)
    disabled = MagicMock()
    disabled.enabled = False

    maybe_trigger_healing(
        site="catch", site_run_id="r1", yaml_path=yaml_file,
        diagnosis=_diag(FailureCategory.SCHEMA_CHANGE),
        api_sample={"data": {"recruitData": [{"RecruitID": 1}]}},
        api_sample_prev=None,
        repo=repo, approval_repo=approval_repo, slack=slack,
        db_conn=db_conn, llm_client=disabled,
    )
    assert slack.notify_approval_request.call_count == 0


# ---------- 정상 흐름 ----------

def test_full_pipeline_creates_approval_and_posts_slack(
    yaml_file, repo, approval_repo, slack, db_conn
):
    """schema_change + 기존 성공 history + LLM patch → improved → approval 게시."""
    _seed_successful_history(repo)
    patch = _make_patch_replace_items_path()
    client = _make_llm_client(patch)

    # 응답 sample은 wrapper 깊어진 케이스 → patch가 적용되면 추출 성공
    api_sample = {"data": {"recruitData": [
        {"RecruitID": 1, "RecruitTitle": "공고1", "CompName": "회사1"},
        {"RecruitID": 2, "RecruitTitle": "공고2", "CompName": "회사2"},
    ]}}

    maybe_trigger_healing(
        site="catch", site_run_id="r1", yaml_path=yaml_file,
        diagnosis=_diag(FailureCategory.SCHEMA_CHANGE),
        api_sample=api_sample, api_sample_prev=None,
        repo=repo, approval_repo=approval_repo, slack=slack,
        db_conn=db_conn, llm_client=client,
    )

    pendings = approval_repo.list_pending(site="catch")
    assert len(pendings) == 1
    row = pendings[0]
    assert row.status == "pending"
    assert "items_path" in row.patch_json

    # slack 메시지 게시 + thread_ts 매핑 확인
    assert slack.notify_approval_request.call_count == 1
    assert row.slack_thread_ts == "1.234"
    assert row.slack_channel == "C123"


def test_empty_changes_uses_healing_unavailable_no_approval(
    yaml_file, repo, approval_repo, slack, db_conn
):
    """LLM이 빈 changes (infeasible)면 approval 안 만들고 simple 알림."""
    _seed_successful_history(repo)
    client = _make_llm_client(_make_empty_patch())

    maybe_trigger_healing(
        site="catch", site_run_id="r1", yaml_path=yaml_file,
        diagnosis=_diag(FailureCategory.SCHEMA_CHANGE),
        api_sample={"data": {"recruitData": []}},
        api_sample_prev=None,
        repo=repo, approval_repo=approval_repo, slack=slack,
        db_conn=db_conn, llm_client=client,
    )
    assert approval_repo.list_pending() == []
    assert slack.notify_approval_request.call_count == 0
    assert slack.notify_healing_unavailable.call_count == 1


def test_patch_invalid_uses_healing_unavailable(
    yaml_file, repo, approval_repo, slack, db_conn
):
    """LLM patch가 yaml 스키마 위반이면 approval 안 만듦."""
    _seed_successful_history(repo)
    bad_patch = PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(
            op="replace", path="collectors.jobs.type",
            old="api_jobs", new="weird_type",
        )],
        reason="잘못된 type",
        risk="medium",
    )
    client = _make_llm_client(bad_patch)

    maybe_trigger_healing(
        site="catch", site_run_id="r1", yaml_path=yaml_file,
        diagnosis=_diag(FailureCategory.SCHEMA_CHANGE),
        api_sample={"data": {"recruitData": []}},
        api_sample_prev=None,
        repo=repo, approval_repo=approval_repo, slack=slack,
        db_conn=db_conn, llm_client=client,
    )
    assert approval_repo.list_pending() == []
    assert slack.notify_healing_unavailable.call_count == 1


def test_pipeline_crash_does_not_propagate(
    yaml_file, repo, approval_repo, slack, db_conn
):
    """healing 내부 예외는 호출자로 안 새어야 한다 (정상 collect 흐름 보호)."""
    _seed_successful_history(repo)
    crashing_client = MagicMock()
    crashing_client.enabled = True
    crashing_client.parse.side_effect = RuntimeError("LLM exploded")

    # 예외 raise되지 않아야 함
    maybe_trigger_healing(
        site="catch", site_run_id="r1", yaml_path=yaml_file,
        diagnosis=_diag(FailureCategory.SCHEMA_CHANGE),
        api_sample={"data": {"recruitData": []}},
        api_sample_prev=None,
        repo=repo, approval_repo=approval_repo, slack=slack,
        db_conn=db_conn, llm_client=crashing_client,
    )
