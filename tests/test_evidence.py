import json

from app.collectors.base import ValidationIssue
from app.diagnosis import FailureCategory, classify_failure
from app.evidence import EvidenceStore


def test_write_api_sample_and_promote_prev(tmp_path):
    store = EvidenceStore(tmp_path)
    sample = {"recruitData": [{"RecruitID": 1}]}
    p = store.write_api_sample("catch", "run-1", sample)
    assert p.exists()
    assert json.loads(p.read_text(encoding="utf-8")) == sample

    prev = store.promote_prev_sample("catch", "run-1")
    assert prev is not None and prev.exists()


def test_write_report_serializes_dataclasses(tmp_path):
    store = EvidenceStore(tmp_path)
    issues = [ValidationIssue(code="empty_results", message="no items")]
    diagnosis = classify_failure(issues)
    p = store.write_report(
        "catch",
        "run-1",
        status="failed",
        records_count=0,
        issues=issues,
        diagnosis=diagnosis,
    )
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["status"] == "failed"
    assert data["issues"][0]["code"] == "empty_results"
    assert data["diagnosis"]["category"] == FailureCategory.EMPTY_RESULTS.value


def test_run_id_with_colon_makes_safe_dir(tmp_path):
    store = EvidenceStore(tmp_path)
    store.write_api_sample("catch", "abc:catch", {"x": 1})
    # ":"가 디렉토리 이름에 들어가면 안 됨 (windows·sqlite path 안전)
    snap_root = tmp_path / "snapshots" / "catch"
    children = [p.name for p in snap_root.iterdir()]
    assert any("_" in name and "abc" in name for name in children)


def test_report_generated_at_is_kst(tmp_path):
    store = EvidenceStore(tmp_path)
    p = store.write_report(
        "catch", "run-1", status="ok", records_count=0, issues=[],
    )
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["generated_at"].endswith("+09:00")
