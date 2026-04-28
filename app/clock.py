"""사람이 보는 시각 표기는 KST로 통일한다.

- DB 저장값(repository.py)은 UTC를 유지한다 (기존 데이터와 일관성, 비교 안전성).
- 사람이 읽는 곳(evidence report, JSON 로그)은 KST.

이 모듈은 그 KST 표기 한 곳에 두기 위한 헬퍼다.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9), name="KST")


def now_kst_iso() -> str:
    """현재 KST 시각을 ISO 8601 문자열(seconds 단위)로 반환. 예: 2026-04-28T15:04:00+09:00."""
    return datetime.now(KST).isoformat(timespec="seconds")


def yesterday_kst_window(
    *, now: datetime | None = None
) -> tuple[datetime, datetime, date]:
    """KST 어제 캘린더 일자의 [since, until) 윈도우와 그 날짜를 반환.

    daily summary용. SQL 비교는 timezone-aware datetime이면 UTC 변환이 자동.
    반환값:
      - since: 어제 00:00:00 KST (inclusive)
      - until: 오늘 00:00:00 KST (exclusive)
      - target_date: 어제 KST 날짜 (헤더 표기용)

    Args:
        now: 테스트용 시각 주입. None이면 datetime.now(KST).
    """
    now_kst = (now or datetime.now(KST)).astimezone(KST)
    today_start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    since = today_start - timedelta(days=1)
    until = today_start
    return since, until, since.date()
