from app.config.schema import (
    CollectorConfig,
    MappingConfig,
    RequestConfig,
    RuntimeConfig,
    SiteConfig,
)
from app.runtime import parse_allowed_sites, should_run_site


def make_site(
    site: str = "catch",
    enabled: bool = True,
    preferred_environment: str = "vm",
) -> SiteConfig:
    return SiteConfig(
        site=site,
        name=f"{site}.example",
        enabled=enabled,
        collectors={
            "jobs": CollectorConfig(
                type="api_jobs",
                fetcher="http",
                purpose="postings",
                request=RequestConfig(method="GET", url="https://example.test/jobs"),
                mapping=MappingConfig(items_path="data.items", fields={"id": "id"}),
            )
        },
        runtime=RuntimeConfig(preferred_environment=preferred_environment),
    )


def test_parse_allowed_sites_basic():
    assert parse_allowed_sites("catch, other ,") == {"catch", "other"}
    assert parse_allowed_sites(None) == set()
    assert parse_allowed_sites("") == set()


def test_should_run_skips_disabled_site():
    cfg = make_site(enabled=False)
    assert not should_run_site(cfg, environment="vm", allowed_sites={"catch"})


def test_should_run_requires_matching_environment():
    cfg = make_site(preferred_environment="local")
    assert not should_run_site(cfg, environment="vm", allowed_sites={"catch"})
    assert should_run_site(cfg, environment="local", allowed_sites=set())


def test_vm_requires_allowlist():
    cfg = make_site(preferred_environment="vm")
    assert not should_run_site(cfg, environment="vm", allowed_sites=set())
    assert should_run_site(cfg, environment="vm", allowed_sites={"catch"})


def test_local_does_not_require_allowlist():
    cfg = make_site(preferred_environment="local")
    assert should_run_site(cfg, environment="local", allowed_sites=set())
