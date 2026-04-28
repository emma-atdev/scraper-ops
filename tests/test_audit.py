"""audit.log append-only 테스트."""

from __future__ import annotations

import json

from app.audit import audit_log


def test_audit_log_creates_file_and_appends(tmp_path):
    log_path = tmp_path / "audit.log"
    audit_log("approval_created", path=log_path, id=1, site="catch")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event"] == "approval_created"
    assert payload["id"] == 1
    assert payload["site"] == "catch"
    assert payload["ts"].endswith("+09:00")  # KST


def test_audit_log_appends_without_overwriting(tmp_path):
    log_path = tmp_path / "audit.log"
    audit_log("approval_created", path=log_path, id=1)
    audit_log("approval_approved", path=log_path, id=1, by="cli:tester")
    audit_log("approval_created", path=log_path, id=2)

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    events = [json.loads(l)["event"] for l in lines]
    assert events == ["approval_created", "approval_approved", "approval_created"]


def test_audit_log_creates_parent_dir(tmp_path):
    log_path = tmp_path / "nested" / "deeper" / "audit.log"
    audit_log("test_event", path=log_path)
    assert log_path.exists()


def test_audit_log_korean_unicode_preserved(tmp_path):
    log_path = tmp_path / "audit.log"
    audit_log("approval_rejected", path=log_path, reason="응답 구조 변경 의심")
    line = log_path.read_text(encoding="utf-8")
    assert "응답 구조 변경 의심" in line  # ensure_ascii=False
