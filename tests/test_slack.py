import json
import os

import pytest

from app.collectors.base import ValidationIssue
from app.diagnosis import classify_failure
from app.healing import DryRunResult
from app.integrations import SlackConfig, SlackNotifier
from app.llm import PatchCandidate, PatchOperation
from app.models import UpsertStats


def test_slack_config_from_env_returns_none_when_missing(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    assert SlackConfig.from_env() is None


def test_slack_config_from_env(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-x")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "C123")
    cfg = SlackConfig.from_env()
    assert cfg.bot_token == "xoxb-x"
    assert cfg.channel_id == "C123"


def test_notifier_disabled_is_noop():
    notifier = SlackNotifier(None)
    diagnosis = classify_failure([])
    out = notifier.notify_run_result(
        site="catch", run_id="r", status="success",
        stats=UpsertStats(inserted=10), issues=[], diagnosis=diagnosis,
    )
    assert out is None
    assert not notifier.enabled


def test_message_blocks_include_status_stats_and_issues():
    cfg = SlackConfig(bot_token="t", channel_id="C")
    notifier = SlackNotifier(cfg)
    issues = [ValidationIssue(code="missing_required_field", message="title missing")]
    diagnosis = classify_failure(issues)

    text, blocks = notifier._build_message(
        site="catch",
        run_id="run-1",
        status="failed",
        stats=UpsertStats(inserted=0, updated=0, unchanged=0),
        issues=issues,
        diagnosis=diagnosis,
        report_path="data/reports/catch/run-1/report.json",
    )
    rendered = json.dumps({"text": text, "blocks": blocks}, ensure_ascii=False)
    assert "catch" in rendered
    assert "수집 실패" in rendered
    assert "missing_required_field" in rendered
    assert "data/reports/catch/run-1/report.json" in rendered
    assert "필수 필드 누락" in rendered  # 한글 진단 라벨


def test_success_with_change_uses_korean_format():
    cfg = SlackConfig(bot_token="t", channel_id="C")
    notifier = SlackNotifier(cfg)
    diagnosis = classify_failure([])

    text, _ = notifier._build_message(
        site="catch", run_id="r", status="success",
        stats=UpsertStats(inserted=58, updated=2, unchanged=2319),
        issues=[], diagnosis=diagnosis, report_path=None,
    )
    assert "수집 완료" in text
    assert "신규 58건" in text
    assert "변경 2건" in text


def test_success_no_change_text():
    cfg = SlackConfig(bot_token="t", channel_id="C")
    notifier = SlackNotifier(cfg)
    diagnosis = classify_failure([])

    text, _ = notifier._build_message(
        site="catch", run_id="r", status="success",
        stats=UpsertStats(inserted=0, updated=0, unchanged=2400),
        issues=[], diagnosis=diagnosis, report_path=None,
    )
    assert "변화 없음" in text


def test_daily_summary_message_contains_per_site_lines():
    cfg = SlackConfig(bot_token="t", channel_id="C")
    notifier = SlackNotifier(cfg)
    summary = {
        "since": "2026-04-26T09:00:00+00:00",
        "until": "2026-04-27T09:00:00+00:00",
        "by_site": {
            "catch": {
                "runs": 9, "success": 9, "failed": 0,
                "inserted": 73, "updated": 4, "unchanged": 21000,
                "last_status": "success", "last_finished_at": "2026-04-27T08:00:00+00:00",
            }
        },
    }
    text, blocks = notifier._build_summary_message(summary, target_date="2026-04-26")
    rendered = json.dumps({"text": text, "blocks": blocks}, ensure_ascii=False)
    assert "catch" in rendered
    assert "73" in rendered
    assert "실행 9회" in rendered
    assert "일일 요약" in rendered
    assert "2026-04-26" in rendered


def test_daily_summary_no_runs_message():
    cfg = SlackConfig(bot_token="t", channel_id="C")
    notifier = SlackNotifier(cfg)
    text, blocks = notifier._build_summary_message(
        {"since": "x", "until": "y", "by_site": {}}, target_date="2026-04-27"
    )
    assert "실행 기록 없음" in text
    assert "2026-04-27" in text


def test_post_calls_chat_postmessage(monkeypatch):
    cfg = SlackConfig(bot_token="xoxb-test", channel_id="C123")
    notifier = SlackNotifier(cfg)
    captured = {}

    class FakeResp:
        def __init__(self, body):
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._body

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data.decode("utf-8")
        return FakeResp(b'{"ok": true, "ts": "1.2"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    diagnosis = classify_failure([])
    out = notifier.notify_run_result(
        site="catch", run_id="r", status="success",
        stats=UpsertStats(inserted=5), issues=[], diagnosis=diagnosis,
    )
    assert out == {"ok": True, "ts": "1.2"}
    assert captured["url"] == "https://slack.com/api/chat.postMessage"
    assert any(k.lower() == "authorization" and v == "Bearer xoxb-test" for k, v in captured["headers"].items())
    payload = json.loads(captured["body"])
    assert payload["channel"] == "C123"
    assert "blocks" in payload



# ---------- M6.5: approval request 메시지 ----------


def _patch(changes=None, *, reason="응답 wrapper가 한 단계 깊어짐", risk="low"):
    return PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(**c) for c in (changes or [
            {"op": "replace", "path": "collectors.jobs.mapping.items_path",
             "old": "recruitData", "new": "data.recruitData"},
        ])],
        reason=reason,
        risk=risk,
    )


def _dry_run(verdict="improved", before=0, after=47, after_missing=0):
    return DryRunResult(
        verdict=verdict,
        before_count=before,
        after_count=after,
        before_missing_required=0,
        after_missing_required=after_missing,
        before_issues=[],
        after_issues=[],
        sample_records=[
            {"external_id": "1234", "title": "Backend Senior", "company": "Acme",
             "deadline": "2026-12-31", "link": "https://x"},
            {"external_id": "1235", "title": "Data Engineer", "company": "Beta",
             "deadline": "2026-11-30", "link": "https://y"},
            {"external_id": "1236", "title": "Frontend", "company": "Gamma",
             "deadline": None, "link": None},
        ],
    )


def test_approval_message_improved_includes_diff_and_samples():
    cfg = SlackConfig(bot_token="t", channel_id="C")
    notifier = SlackNotifier(cfg)
    diagnosis = classify_failure([ValidationIssue(code="items_path_not_list", message="dict")])

    text, blocks = notifier._build_approval_message(
        approval_id=7, site="catch", run_id="r1",
        diagnosis=diagnosis, patch=_patch(), dry_run=_dry_run(),
        expires_at_kst="2026-04-29 12:31",
    )
    rendered = json.dumps({"text": text, "blocks": blocks}, ensure_ascii=False)
    assert "✅" in rendered
    assert "개선됨" in rendered
    assert "#7" in rendered
    assert "items_path" in rendered  # diff
    assert "data.recruitData" in rendered
    assert "Backend Senior" in rendered  # sample
    assert "approve" in rendered  # 명령 안내
    assert "2026-04-29 12:31" in rendered


def test_approval_message_regressed_marks_warning():
    cfg = SlackConfig(bot_token="t", channel_id="C")
    notifier = SlackNotifier(cfg)
    diagnosis = classify_failure([])
    text, blocks = notifier._build_approval_message(
        approval_id=8, site="catch", run_id="r2",
        diagnosis=diagnosis, patch=_patch(),
        dry_run=_dry_run(verdict="regressed", before=30, after=0),
        expires_at_kst="2026-04-30 09:00",
    )
    rendered = json.dumps({"text": text, "blocks": blocks}, ensure_ascii=False)
    assert "⚠️" in rendered
    assert "악화됨" in rendered
    assert "Reject 권장" in rendered


def test_approval_message_unchanged_marks_neutral():
    cfg = SlackConfig(bot_token="t", channel_id="C")
    notifier = SlackNotifier(cfg)
    diagnosis = classify_failure([])
    text, blocks = notifier._build_approval_message(
        approval_id=9, site="catch", run_id="r3",
        diagnosis=diagnosis, patch=_patch(),
        dry_run=_dry_run(verdict="unchanged", before=30, after=30),
        expires_at_kst="2026-04-30 09:00",
    )
    rendered = json.dumps({"text": text, "blocks": blocks}, ensure_ascii=False)
    assert "📊" in rendered
    assert "변화 없음" in rendered


def test_notify_approval_request_disabled_is_noop():
    notifier = SlackNotifier(None)
    diagnosis = classify_failure([])
    out = notifier.notify_approval_request(
        approval_id=1, site="catch", run_id="r",
        diagnosis=diagnosis, patch=_patch(), dry_run=_dry_run(),
        expires_at_kst="2026-04-30 09:00",
    )
    assert out is None


def test_notify_healing_unavailable_includes_reason(monkeypatch):
    cfg = SlackConfig(bot_token="t", channel_id="C")
    notifier = SlackNotifier(cfg)
    captured = {}

    class FakeResp:
        def __init__(self, body):
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._body

    def fake_urlopen(req, timeout=None):
        captured["body"] = req.data.decode("utf-8")
        return FakeResp(b'{"ok": true, "ts": "1.2"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = notifier.notify_healing_unavailable(
        site="catch", run_id="r1",
        reason_label="LLM이 처방 불가 판정",
        detail="Red Zone 가능성, 사람 직접 점검 필요.",
    )

    assert out == {"ok": True, "ts": "1.2"}
    payload = json.loads(captured["body"])
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "자동 처방 불가" in rendered
    assert "LLM이 처방 불가 판정" in rendered
    assert "Red Zone" in rendered


def test_notify_healing_unavailable_disabled_is_noop():
    notifier = SlackNotifier(None)
    out = notifier.notify_healing_unavailable(
        site="catch", run_id="r1", reason_label="x",
    )
    assert out is None
