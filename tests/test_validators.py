from app.config.schema import ValidationConfig
from app.models import JobPosting
from app.validators import validate_postings


def _p(**kw):
    base = dict(external_id="1", site="catch", title="t", company="c", deadline="d", link="l")
    base.update(kw)
    return JobPosting(**base)


def test_empty_results_flagged():
    out = validate_postings([], ValidationConfig(required_fields=["external_id"]))
    assert not out.ok
    assert any(i.code == "empty_results" for i in out.issues)


def test_missing_required_field_flagged():
    cfg = ValidationConfig(required_fields=["title", "company"])
    out = validate_postings([_p(title=None)], cfg)
    assert not out.ok
    assert any(i.code == "missing_required_field" for i in out.issues)


def test_volume_drop_flagged():
    cfg = ValidationConfig(required_fields=["external_id"], max_volume_drop_ratio=0.5)
    out = validate_postings([_p()], cfg, previous_count=20)
    assert any(i.code == "volume_drop" for i in out.issues)


def test_no_issues_when_healthy():
    cfg = ValidationConfig(required_fields=["external_id", "title"])
    out = validate_postings([_p(), _p(external_id="2")], cfg, previous_count=2)
    assert out.ok
    assert out.issues == []
