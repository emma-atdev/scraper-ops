"""구조화 로그 설정. JSON lines, run_id/site/event 키를 기본 보유.

기획서 "로그 전략" 섹션 적용. 시크릿 마스킹 진입점은 추후 확장.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


_SECRET_HEADER_KEYS = {"authorization", "cookie", "x-api-key", "x-auth-token"}


def mask_headers(headers: dict[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    masked = {}
    for k, v in headers.items():
        if k.lower() in _SECRET_HEADER_KEYS:
            masked[k] = "***"
        else:
            masked[k] = v
    return masked


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("run_id", "site", "collector", "phase", "event"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str | None = None) -> None:
    lvl_name = (level or os.environ.get("SCRAPER_LOG_LEVEL") or "INFO").upper()
    lvl = getattr(logging, lvl_name, logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLineFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(lvl)
