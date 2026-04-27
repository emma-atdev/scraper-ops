import json
import os

import pytest

from app.collectors.base import ValidationIssue
from app.diagnosis import classify_failure
from app.integrations import SlackConfig, SlackNotifier
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
    text, blocks = notifier._build_summary_message(summary, hours=24)
    rendered = json.dumps({"text": text, "blocks": blocks}, ensure_ascii=False)
    assert "catch" in rendered
    assert "73" in rendered
    assert "실행 9회" in rendered
    assert "일일 요약" in rendered


def test_daily_summary_no_runs_message():
    cfg = SlackConfig(bot_token="t", channel_id="C")
    notifier = SlackNotifier(cfg)
    text, blocks = notifier._build_summary_message(
        {"since": "x", "until": "y", "by_site": {}}, hours=24
    )
    assert "실행 기록 없음" in text


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
