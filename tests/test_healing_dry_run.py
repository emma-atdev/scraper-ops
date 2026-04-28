"""healing.dry_run 단위 테스트. 네트워크·LLM 호출 없음 (FakeFetcher만 사용)."""

from __future__ import annotations

import pytest
import yaml

from app.healing.dry_run import (
    FakeFetcher,
    PatchApplyError,
    apply_patch,
    run_dry_run,
)
from app.llm import PatchCandidate, PatchOperation


# ---------- 공통 fixture ----------

CATCH_YAML = """
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
      url: https://www.catch.co.kr/api/v1.0/recruit/information/getRecruitList
      headers: {}
      params:
        pageSize: 30
    pagination:
      type: page
      param: curpage
      start: 1
      max_pages: 3
      stop_condition: empty_items
    mapping:
      items_path: recruitData
      link_template: https://www.catch.co.kr/NCS/RecruitInfoDetails/{RecruitID}
      fields:
        external_id: RecruitID
        title: RecruitTitle
        company: CompName
    validation:
      required_fields:
        - external_id
        - title
        - company
      min_items_per_page: 0
"""


def _patch(changes: list[dict]) -> PatchCandidate:
    return PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(**c) for c in changes],
        reason="test",
        risk="low",
    )


def _sample_with_wrapper(n: int = 3) -> dict:
    """현재 운영 wrapper. data.recruitData 안에 items."""
    return {
        "data": {
            "recruitData": [
                {
                    "RecruitID": 1000 + i,
                    "RecruitTitle": f"채용공고 {i}",
                    "CompName": f"회사 {i}",
                }
                for i in range(n)
            ]
        }
    }


def _sample_flat(n: int = 3) -> dict:
    """이전 wrapper. 최상위 recruitData."""
    return {
        "recruitData": [
            {
                "RecruitID": 2000 + i,
                "RecruitTitle": f"공고 {i}",
                "CompName": f"회사 {i}",
            }
            for i in range(n)
        ]
    }


# ---------- apply_patch ----------

def test_apply_patch_replace_nested():
    base = yaml.safe_load(CATCH_YAML)
    patch = _patch([{
        "op": "replace",
        "path": "collectors.jobs.mapping.items_path",
        "old": "recruitData",
        "new": "data.recruitData",
    }])

    new = apply_patch(base, patch)

    assert new["collectors"]["jobs"]["mapping"]["items_path"] == "data.recruitData"
    # 원본 보존
    assert base["collectors"]["jobs"]["mapping"]["items_path"] == "recruitData"


def test_apply_patch_add_new_key():
    base = {"a": {"b": {}}}
    patch = _patch([{"op": "add", "path": "a.b.c", "new": "hello"}])
    new = apply_patch(base, patch)
    assert new == {"a": {"b": {"c": "hello"}}}
    assert base == {"a": {"b": {}}}


def test_apply_patch_remove_key():
    base = {"a": {"b": "x", "c": "y"}}
    patch = _patch([{"op": "remove", "path": "a.b"}])
    new = apply_patch(base, patch)
    assert new == {"a": {"c": "y"}}


def test_apply_patch_path_not_found():
    base = {"a": {"b": "x"}}
    patch = _patch([{"op": "replace", "path": "a.nope.deeper", "new": "z"}])
    with pytest.raises(PatchApplyError):
        apply_patch(base, patch)


def test_apply_patch_replace_target_missing():
    base = {"a": {"b": "x"}}
    patch = _patch([{"op": "replace", "path": "a.c", "new": "z"}])
    with pytest.raises(PatchApplyError):
        apply_patch(base, patch)


def test_apply_patch_remove_missing_key():
    base = {"a": {}}
    patch = _patch([{"op": "remove", "path": "a.b"}])
    with pytest.raises(PatchApplyError):
        apply_patch(base, patch)


def test_apply_patch_empty_changes_returns_copy():
    base = {"a": 1}
    patch = _patch([])
    new = apply_patch(base, patch)
    assert new == base
    assert new is not base  # deep copy


# ---------- FakeFetcher ----------

def test_fake_fetcher_returns_pages_in_order():
    pages = [{"page": 1}, {"page": 2}]
    f = FakeFetcher(pages)
    r1 = f.fetch("https://x", method="GET", params={"curpage": 1})
    r2 = f.fetch("https://x", method="GET", params={"curpage": 2})
    assert r1.json == {"page": 1}
    assert r2.json == {"page": 2}
    assert r1.status == 200
    assert len(f.calls) == 2


def test_fake_fetcher_exhausts_to_empty():
    f = FakeFetcher([{"page": 1}])
    f.fetch("https://x")
    r2 = f.fetch("https://x")
    assert r2.json == {}
    assert r2.status == 200


# ---------- run_dry_run: 정상 케이스 ----------

def test_run_dry_run_improved_zero_to_n():
    """현재 yaml은 items_path=recruitData. 응답은 wrapper가 한 단계 깊어짐.
    → 현재 yaml로는 0건. patch(items_path=data.recruitData)로 N건."""
    patch = _patch([{
        "op": "replace",
        "path": "collectors.jobs.mapping.items_path",
        "old": "recruitData",
        "new": "data.recruitData",
    }])
    result = run_dry_run(
        site="catch",
        yaml_text=CATCH_YAML,
        patch=patch,
        api_sample=_sample_with_wrapper(n=3),
    )
    assert result.verdict == "improved"
    assert result.before_count == 0
    assert result.after_count == 3
    assert result.after_missing_required == 0
    assert len(result.sample_records) == 3
    assert result.sample_records[0]["external_id"] == "1000"
    assert result.sample_records[0]["title"] == "채용공고 0"
    # link_template이 적용됐는지
    assert "1000" in (result.sample_records[0]["link"] or "")


def test_run_dry_run_unchanged():
    """patch가 사실상 변화 없음 (link_template 미세 조정 같은 시나리오)."""
    patch = _patch([{
        "op": "replace",
        "path": "collectors.jobs.mapping.link_template",
        "old": "https://www.catch.co.kr/NCS/RecruitInfoDetails/{RecruitID}",
        "new": "https://www.catch.co.kr/NCS/RecruitInfoDetails/{RecruitID}",
    }])
    result = run_dry_run(
        site="catch",
        yaml_text=CATCH_YAML,
        patch=patch,
        api_sample=_sample_flat(n=3),
    )
    assert result.verdict == "unchanged"
    assert result.before_count == 3
    assert result.after_count == 3


def test_run_dry_run_regressed():
    """현재 yaml은 정상 추출 중인데 patch가 path를 잘못된 곳으로 옮김."""
    patch = _patch([{
        "op": "replace",
        "path": "collectors.jobs.mapping.items_path",
        "old": "recruitData",
        "new": "data.recruitData",  # 응답은 flat인데 wrapper 경로로 변경 → 0건
    }])
    result = run_dry_run(
        site="catch",
        yaml_text=CATCH_YAML,
        patch=patch,
        api_sample=_sample_flat(n=5),
    )
    assert result.verdict == "regressed"
    assert result.before_count == 5
    assert result.after_count == 0


# ---------- run_dry_run: 실패 케이스 ----------

def test_run_dry_run_patch_apply_failed():
    """patch path가 yaml 안에 존재하지 않음."""
    patch = _patch([{
        "op": "replace",
        "path": "collectors.jobs.headers.UserAgent",  # headers는 request 아래에 있어야 함
        "new": "x",
    }])
    result = run_dry_run(
        site="catch",
        yaml_text=CATCH_YAML,
        patch=patch,
        api_sample=_sample_flat(),
    )
    assert result.verdict == "patch_apply_failed"
    assert result.patch_apply_failed_reason
    assert result.before_count == 0  # 실행조차 안 함
    assert result.after_count == 0


def test_run_dry_run_patch_invalid():
    """patch 자체는 적용되지만 SiteConfig 스키마 위반."""
    patch = _patch([{
        "op": "replace",
        "path": "collectors.jobs.type",
        "old": "api_jobs",
        "new": "weird_unknown_type",
    }])
    result = run_dry_run(
        site="catch",
        yaml_text=CATCH_YAML,
        patch=patch,
        api_sample=_sample_flat(),
    )
    assert result.verdict == "patch_invalid"
    assert result.patch_invalid_reason
    assert "weird_unknown_type" in result.patch_invalid_reason or "type" in result.patch_invalid_reason


# ---------- run_dry_run: list sample 입력 ----------

def test_run_dry_run_with_list_sample():
    """api_sample이 page list로 들어왔을 때 페이지 순서대로 처리."""
    patch = _patch([])  # 변화 없음
    pages = [_sample_flat(n=3), _sample_flat(n=2)]
    result = run_dry_run(
        site="catch",
        yaml_text=CATCH_YAML,
        patch=patch,
        api_sample=pages,
    )
    # 빈 patch라 before == after
    assert result.before_count == result.after_count
    # 두 페이지 합쳐 5건
    assert result.after_count == 5


# ---------- missing_required tie-break ----------

def test_run_dry_run_improved_by_missing_reduction():
    """추출 건수는 같지만 patch 후 필수 필드 누락이 감소."""
    # 현재 yaml은 title 필드를 RecruitTitle에서 뽑는다. 응답에 RecruitTitle이 없고
    # JobName만 있는 sample을 넘기면 title이 다 None이 된다.
    sample = {
        "recruitData": [
            {"RecruitID": i, "JobName": f"공고 {i}", "CompName": f"C{i}"}
            for i in range(3)
        ]
    }
    patch = _patch([{
        "op": "replace",
        "path": "collectors.jobs.mapping.fields.title",
        "old": "RecruitTitle",
        "new": "JobName",
    }])
    result = run_dry_run(
        site="catch",
        yaml_text=CATCH_YAML,
        patch=patch,
        api_sample=sample,
    )
    assert result.before_count == 3
    assert result.after_count == 3
    assert result.before_missing_required == 3  # title 다 None
    assert result.after_missing_required == 0
    assert result.verdict == "improved"
