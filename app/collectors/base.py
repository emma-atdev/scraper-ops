"""Collector / Fetcher base abstractions.

Collector는 fetch → parse → map → validate → evidence 저장까지 수행하고
CollectorResult를 return한다. DB 쓰기, Slack 알림 같은 side effect는 Runner가 담당한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class FetchResult:
    """Fetcher가 반환하는 통일된 응답 표현."""

    status: int
    headers: dict[str, str]
    text: str = ""
    json: Any = None
    blocked: bool = False
    url: str = ""


class BaseFetcher(Protocol):
    """모든 fetcher가 따르는 인터페이스. Scrapling 등 외부 라이브러리는 wrapper로 감싼다."""

    def fetch(self, url: str, *, method: str = "GET", **kwargs: Any) -> FetchResult: ...


@dataclass
class Record:
    """Collector가 추출한 정규화 record. 도메인 중립."""

    external_id: str
    fields: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


@dataclass
class ValidationIssue:
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectorResult:
    """Collector 실행 결과. Runner가 받아서 DB 저장·알림 결정."""

    records: list[Record] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


class Collector(Protocol):
    """모든 collector가 따르는 인터페이스."""

    def run(self, config: Any, *, fetcher: BaseFetcher | None = None) -> CollectorResult: ...
