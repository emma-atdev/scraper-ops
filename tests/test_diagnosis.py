from app.collectors.base import ValidationIssue
from app.diagnosis import FailureCategory, classify_failure


def test_none_when_no_issues():
    d = classify_failure([])
    assert d.category == FailureCategory.NONE


def test_network_blocked_takes_priority():
    issues = [
        ValidationIssue(code="missing_required_field", message=""),
        ValidationIssue(code="fetch_failed", message=""),
    ]
    d = classify_failure(issues)
    assert d.category == FailureCategory.NETWORK_BLOCKED


def test_schema_change_for_missing_external_id():
    issues = [ValidationIssue(code="missing_external_id", message="")]
    d = classify_failure(issues)
    assert d.category == FailureCategory.SCHEMA_CHANGE


def test_volume_drop_classified():
    issues = [ValidationIssue(code="volume_drop", message="")]
    d = classify_failure(issues)
    assert d.category == FailureCategory.VOLUME_DROP


def test_unknown_fallback():
    issues = [ValidationIssue(code="weird_unknown_code", message="")]
    d = classify_failure(issues)
    assert d.category == FailureCategory.UNKNOWN
