import pytest
from pydantic import ValidationError

from app.config import load_site_config
from app.config.schema import SiteConfig


def test_load_minimal_api_jobs_config(tmp_path):
    yaml_path = tmp_path / "site.yaml"
    yaml_path.write_text(
        """
site: catch
name: catch.co.kr
collectors:
  jobs:
    type: api_jobs
    fetcher: http
    purpose: postings
    request:
      method: GET
      url: https://api.catch.co.kr/jobs
    mapping:
      items_path: data.recruitData
      fields:
        id: recruitIdx
        title: recruitTitle
runtime:
  preferred_environment: vm
""",
        encoding="utf-8",
    )

    cfg = load_site_config(yaml_path)
    assert isinstance(cfg, SiteConfig)
    assert cfg.site == "catch"
    assert "jobs" in cfg.collectors
    jobs = cfg.collectors["jobs"]
    assert jobs.type == "api_jobs"
    assert jobs.fetcher == "http"
    assert jobs.mapping.items_path == "data.recruitData"
    assert jobs.mapping.fields["title"] == "recruitTitle"


def test_unknown_collector_type_rejected(tmp_path):
    yaml_path = tmp_path / "site.yaml"
    yaml_path.write_text(
        """
site: x
name: x
collectors:
  jobs:
    type: unsupported_type
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_site_config(yaml_path)


def test_extra_field_rejected(tmp_path):
    yaml_path = tmp_path / "site.yaml"
    yaml_path.write_text(
        """
site: x
name: x
mystery_field: 1
collectors:
  jobs:
    type: api_jobs
""",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_site_config(yaml_path)
