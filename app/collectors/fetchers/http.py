"""HTTP fetcher (stdlib urllib 기반).

Scrapling Fetcher가 일부 사이트(catch.co.kr 등)에서 curl_cffi TLS 시그니처로
역으로 차단되는 사례가 발견되어, 기본 HTTP 경로는 stdlib으로 둔다. Scrapling 의존성은
유지하며, Yellow Zone(`DynamicFetcher`/`StealthyFetcher`)에서 활용한다.

Wrapper 책임:
- BaseFetcher 인터페이스 통일
- params 직렬화, 헤더 적용
- blocked 감지 (status, cf-ray, "200 + empty body" 패턴)
- 시크릿 마스킹 진입점 (필요 시 추가)
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from app.collectors.base import FetchResult

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SECONDS = 20


class HttpFetcher:
    BLOCKED_STATUS = {403, 503}

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        **_: Any,
    ) -> FetchResult:
        full_url = self._with_query(url, params)
        request_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json, text/html, */*"}
        if headers:
            request_headers.update({k: str(v) for k, v in headers.items()})

        req = urllib.request.Request(
            full_url,
            data=body,
            method=method.upper(),
            headers=request_headers,
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = int(resp.status)
                resp_headers = {k: v for k, v in resp.headers.items()}
                raw = resp.read()
        except urllib.error.HTTPError as e:
            status = int(e.code)
            resp_headers = {k: v for k, v in (e.headers or {}).items()}
            raw = e.read() if hasattr(e, "read") else b""
        except urllib.error.URLError as e:
            return FetchResult(status=0, headers={}, text="", json=None, blocked=True, url=full_url)

        text = raw.decode("utf-8", errors="replace")
        return FetchResult(
            status=status,
            headers=resp_headers,
            text=text,
            json=self._safe_json(text),
            blocked=self._detect_blocked(status, resp_headers, text),
            url=full_url,
        )

    @staticmethod
    def _with_query(url: str, params: dict[str, Any] | None) -> str:
        if not params:
            return url
        encoded = urllib.parse.urlencode(
            [(k, "" if v is None else str(v)) for k, v in params.items()],
            doseq=True,
        )
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{encoded}"

    @staticmethod
    def _safe_json(text: str) -> Any:
        if not text:
            return None
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return None

    @classmethod
    def _detect_blocked(cls, status: int, headers: dict[str, str], text: str) -> bool:
        if status in cls.BLOCKED_STATUS:
            return True
        # "200 + empty body" 비표준 anti-bot 패턴
        if status == 200 and not text.strip():
            return True
        lowered = text[:2000].lower()
        # 단어 단독 매칭은 false positive 위험. phrase 기준.
        if "just a moment" in lowered or "complete the captcha" in lowered:
            return True
        # cf-ray 헤더는 Cloudflare 경유 신호일 뿐 정상 응답에도 존재. 단독으로 blocked 판정 X.
        return False
