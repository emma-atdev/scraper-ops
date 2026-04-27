"""Fetcher implementations.

MVP는 HttpFetcher만 eager import한다. DynamicFetcher, StealthyFetcher는 Yellow Zone
사이트 승인 시점에 lazy import 진입점으로 추가한다.
"""

from app.collectors.fetchers.http import HttpFetcher

__all__ = ["HttpFetcher"]
