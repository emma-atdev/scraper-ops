"""approved patch를 실제 yaml 파일에 적용하고, 필요 시 롤백.

이 모듈은 LLM 권한 경계의 진짜 실행 지점이다. 책임 한정:
- yaml 파일 읽기 → patch 적용 → SiteConfig 스키마 검증 → 백업 → 덮어쓰기
- 백업 파일에서 원복

approval status 검증·audit·slack 회신은 호출자(decision.py) 책임.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.clock import KST
from app.config.schema import SiteConfig
from app.healing.dry_run import PatchApplyError, apply_patch
from app.llm.schemas import PatchCandidate

logger = logging.getLogger("scraper.patching")

DEFAULT_BACKUP_ROOT = Path("data/yaml-backups")


class PatchApplyValidationError(RuntimeError):
    """patch 적용 후 yaml이 SiteConfig 스키마를 통과하지 못함."""


@dataclass
class ApplyResult:
    yaml_path: Path
    backup_path: Path
    applied_at_iso: str  # KST


def apply_patch_to_file(
    *,
    yaml_path: Path,
    patch: PatchCandidate,
    site: str,
    backup_root: Path | None = None,
) -> ApplyResult:
    """yaml 파일에 patch를 적용한다.

    1. yaml 읽기 → dict
    2. patch 적용 (in-memory)
    3. SiteConfig 검증
    4. 원본을 backup_root/{site}/{timestamp}.yaml로 복사
    5. 새 yaml 덮어쓰기

    실패 케이스:
    - PatchApplyError: path가 yaml 안에 없음 (호출자가 잡아 처리)
    - PatchApplyValidationError: 적용 후 스키마 위반
    """
    yaml_path = Path(yaml_path)
    backup_root = Path(backup_root) if backup_root else DEFAULT_BACKUP_ROOT

    original_text = yaml_path.read_text(encoding="utf-8")
    base_dict = yaml.safe_load(original_text) or {}

    # M6.3 apply_patch는 dict 받아 deep copy 후 변경
    patched_dict = apply_patch(base_dict, patch)

    try:
        SiteConfig.model_validate(patched_dict)
    except ValidationError as e:
        raise PatchApplyValidationError(str(e)) from e

    # 백업 (timestamp는 KST)
    now_kst = datetime.now(KST)
    timestamp = now_kst.strftime("%Y%m%d-%H%M%S")
    backup_dir = backup_root / site
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{timestamp}.yaml"
    shutil.copy2(yaml_path, backup_path)

    # 덮어쓰기
    new_text = yaml.safe_dump(
        patched_dict, allow_unicode=True, sort_keys=False, default_flow_style=False,
    )
    yaml_path.write_text(new_text, encoding="utf-8")

    logger.info(
        "patch applied to file",
        extra={
            "event": "patch_applied",
            "site": site,
            "yaml_path": str(yaml_path),
            "backup_path": str(backup_path),
        },
    )
    return ApplyResult(
        yaml_path=yaml_path,
        backup_path=backup_path,
        applied_at_iso=now_kst.isoformat(timespec="seconds"),
    )


def rollback_from_backup(*, yaml_path: Path, backup_path: Path) -> None:
    """yaml_path를 backup_path 내용으로 되돌린다."""
    yaml_path = Path(yaml_path)
    backup_path = Path(backup_path)
    if not backup_path.exists():
        raise FileNotFoundError(f"backup not found: {backup_path}")
    shutil.copy2(backup_path, yaml_path)
    logger.info(
        "yaml rolled back from backup",
        extra={"event": "yaml_rollback", "yaml_path": str(yaml_path), "backup_path": str(backup_path)},
    )
