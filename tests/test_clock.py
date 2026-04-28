"""KST 헬퍼 단위 테스트."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from app.clock import KST, now_kst_iso, yesterday_kst_window


def test_kst_offset_is_plus_nine():
    assert KST.utcoffset(None) == timedelta(hours=9)


def test_now_kst_iso_format():
    s = now_kst_iso()
    # YYYY-MM-DDTHH:MM:SS+09:00
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00$", s)


def test_now_kst_iso_matches_utc_plus_nine():
    """KST 시각이 같은 순간 UTC + 9h와 일치하는지 (1초 오차 허용)."""
    s = now_kst_iso()
    parsed = datetime.fromisoformat(s)
    # parsed는 timezone-aware
    diff = abs((parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
    assert diff < 2


def test_yesterday_kst_window_basic():
    """2026-04-28 09:00 KST에 daily summary가 돌면, 어제 04-27 00:00 ~ 04-28 00:00."""
    now = datetime(2026, 4, 28, 9, 0, tzinfo=KST)
    since, until, target = yesterday_kst_window(now=now)
    assert since == datetime(2026, 4, 27, 0, 0, tzinfo=KST)
    assert until == datetime(2026, 4, 28, 0, 0, tzinfo=KST)
    assert target == date(2026, 4, 27)


def test_yesterday_kst_window_handles_utc_input():
    """UTC datetime을 넘겨도 KST 기준으로 자른다."""
    now_utc = datetime(2026, 4, 28, 0, 0, tzinfo=timezone.utc)  # = 04-28 09:00 KST
    since, until, target = yesterday_kst_window(now=now_utc)
    assert target == date(2026, 4, 27)
    assert since.tzinfo == KST
    assert until.tzinfo == KST


def test_yesterday_kst_window_at_kst_midnight():
    """KST 자정 정확히에 호출되면 어제는 그 직전 일자."""
    now = datetime(2026, 4, 28, 0, 0, tzinfo=KST)
    since, until, target = yesterday_kst_window(now=now)
    assert target == date(2026, 4, 27)
    assert since == datetime(2026, 4, 27, 0, 0, tzinfo=KST)
    assert until == datetime(2026, 4, 28, 0, 0, tzinfo=KST)


def test_yesterday_kst_window_default_now_uses_kst():
    """now 인자 없이 호출해도 정상 동작."""
    since, until, target = yesterday_kst_window()
    assert (until - since) == timedelta(days=1)
    assert target == since.date()
