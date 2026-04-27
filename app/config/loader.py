"""YAML site config loader. SiteConfig 스키마 검증을 통과한 값만 반환한다."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.config.schema import SiteConfig


def load_site_config(path: str | Path) -> SiteConfig:
    """주어진 YAML 파일을 SiteConfig로 로드한다."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return SiteConfig.model_validate(raw)


def load_all_sites(configs_dir: str | Path) -> dict[str, SiteConfig]:
    """`configs/sites/` 디렉토리의 모든 YAML을 로드한다."""
    configs_dir = Path(configs_dir)
    result: dict[str, SiteConfig] = {}
    for yaml_path in sorted(configs_dir.glob("*.yaml")):
        config = load_site_config(yaml_path)
        result[config.site] = config
    return result
