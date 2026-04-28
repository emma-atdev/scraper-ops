import io
import json
import logging

from app.logging_setup import JsonLineFormatter, mask_headers


def test_mask_headers_strips_secrets():
    headers = {"Authorization": "Bearer x", "X-Api-Key": "k", "User-Agent": "ua"}
    masked = mask_headers(headers)
    assert masked["Authorization"] == "***"
    assert masked["X-Api-Key"] == "***"
    assert masked["User-Agent"] == "ua"


def test_json_formatter_emits_extras():
    fmt = JsonLineFormatter()
    record = logging.LogRecord(
        "scraper.test", logging.INFO, "p", 1, "hello %s", ("world",), None,
    )
    record.run_id = "abc"
    record.site = "catch"
    record.event = "test_event"
    out = fmt.format(record)
    parsed = json.loads(out)
    assert parsed["msg"] == "hello world"
    assert parsed["run_id"] == "abc"
    assert parsed["site"] == "catch"
    assert parsed["event"] == "test_event"
    assert parsed["level"] == "INFO"


def test_json_formatter_ts_is_kst():
    fmt = JsonLineFormatter()
    record = logging.LogRecord(
        "scraper.test", logging.INFO, "p", 1, "hi", (), None,
    )
    parsed = json.loads(fmt.format(record))
    # KST는 +09:00 offset으로 끝난다
    assert parsed["ts"].endswith("+09:00")
