"""patching.apply 단위 테스트. 실 디스크 IO 사용 (tmp_path)."""

from __future__ import annotations

import pytest
import yaml

from app.healing.dry_run import PatchApplyError
from app.llm import PatchCandidate, PatchOperation
from app.patching import (
    PatchApplyValidationError,
    apply_patch_to_file,
    rollback_from_backup,
)


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


def _patch_replace_items_path() -> PatchCandidate:
    return PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(
            op="replace",
            path="collectors.jobs.mapping.items_path",
            old="recruitData",
            new="data.recruitData",
        )],
        reason="응답 wrapper가 한 단계 깊어짐",
        risk="low",
    )


# ---------- apply_patch_to_file ----------

def test_apply_creates_backup_and_writes_new_yaml(yaml_file, tmp_path):
    backup_root = tmp_path / "backups"
    result = apply_patch_to_file(
        yaml_path=yaml_file, patch=_patch_replace_items_path(),
        site="catch", backup_root=backup_root,
    )

    # 새 yaml 파일이 patch 반영됨
    new_dict = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    assert new_dict["collectors"]["jobs"]["mapping"]["items_path"] == "data.recruitData"

    # 백업 파일은 원본 보존
    assert result.backup_path.exists()
    backup_dict = yaml.safe_load(result.backup_path.read_text(encoding="utf-8"))
    assert backup_dict["collectors"]["jobs"]["mapping"]["items_path"] == "recruitData"

    # 백업 위치는 site 하위 + 타임스탬프 이름
    assert result.backup_path.parent == backup_root / "catch"
    assert result.backup_path.suffix == ".yaml"


def test_apply_raises_on_schema_violation(yaml_file, tmp_path):
    """patch가 SiteConfig 스키마 위반이면 PatchApplyValidationError + 파일 안 바뀜."""
    bad_patch = PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(
            op="replace", path="collectors.jobs.type",
            old="api_jobs", new="weird_type",
        )],
        reason="x", risk="low",
    )
    original = yaml_file.read_text(encoding="utf-8")
    with pytest.raises(PatchApplyValidationError):
        apply_patch_to_file(
            yaml_path=yaml_file, patch=bad_patch, site="catch",
            backup_root=tmp_path / "backups",
        )
    # 파일 안 바뀜
    assert yaml_file.read_text(encoding="utf-8") == original
    # 백업도 안 만들어짐
    assert not (tmp_path / "backups").exists()


def test_apply_raises_on_path_not_found(yaml_file, tmp_path):
    bad_patch = PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(
            op="replace", path="collectors.jobs.headers.UserAgent",  # headers는 request 아래에
            old="x", new="y",
        )],
        reason="x", risk="low",
    )
    with pytest.raises(PatchApplyError):
        apply_patch_to_file(
            yaml_path=yaml_file, patch=bad_patch, site="catch",
            backup_root=tmp_path / "backups",
        )


def test_apply_preserves_unicode_korean(yaml_file, tmp_path):
    """한국어 값이 들어있는 yaml도 제대로 유지."""
    yaml_file.write_text(
        CATCH_YAML.replace("catch.co.kr", "캐치 사이트"), encoding="utf-8",
    )
    apply_patch_to_file(
        yaml_path=yaml_file, patch=_patch_replace_items_path(),
        site="catch", backup_root=tmp_path / "backups",
    )
    text = yaml_file.read_text(encoding="utf-8")
    assert "캐치 사이트" in text


# ---------- rollback_from_backup ----------

def test_rollback_restores_yaml(yaml_file, tmp_path):
    result = apply_patch_to_file(
        yaml_path=yaml_file, patch=_patch_replace_items_path(),
        site="catch", backup_root=tmp_path / "backups",
    )
    # 적용 후 새 값 확인
    assert "data.recruitData" in yaml_file.read_text(encoding="utf-8")

    rollback_from_backup(yaml_path=yaml_file, backup_path=result.backup_path)
    restored = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    assert restored["collectors"]["jobs"]["mapping"]["items_path"] == "recruitData"


def test_rollback_missing_backup_raises(tmp_path):
    yaml_path = tmp_path / "x.yaml"
    yaml_path.write_text("site: x\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        rollback_from_backup(
            yaml_path=yaml_path, backup_path=tmp_path / "no.yaml",
        )
