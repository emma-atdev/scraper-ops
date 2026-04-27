"""Runtime 정책 판정. 사이트가 현재 환경에서 실행 가능한지 결정한다."""

from __future__ import annotations

import os

from app.config.schema import SiteConfig


def parse_allowed_sites(value: str | None) -> set[str]:
    """SCRAPER_ALLOWED_SITES 형식(comma-separated)을 set으로 파싱한다."""
    if not value:
        return set()
    return {s.strip() for s in value.split(",") if s.strip()}


def current_environment() -> str:
    return os.environ.get("SCRAPER_ENVIRONMENT", "local")


def should_run_site(
    config: SiteConfig,
    *,
    environment: str | None = None,
    allowed_sites: set[str] | None = None,
) -> bool:
    """주어진 환경에서 이 사이트를 실행해도 되는지 판정.

    실행 가능 조건:
    - config.enabled == True
    - config.runtime.preferred_environment == environment
    - allowed_sites가 주어졌고 (VM 실행), config.site가 그 안에 있어야 함
    """
    if not config.enabled:
        return False

    env = environment or current_environment()
    if config.runtime.preferred_environment != env:
        return False

    if env == "vm":
        if allowed_sites is None:
            allowed_sites = parse_allowed_sites(os.environ.get("SCRAPER_ALLOWED_SITES"))
        if config.site not in allowed_sites:
            return False

    return True
