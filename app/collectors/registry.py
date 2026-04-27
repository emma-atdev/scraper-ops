"""Collector type → 구현체 매핑.

YAML의 `type` 필드값을 보고 적절한 collector 인스턴스를 만들어 준다.
"""

from __future__ import annotations

from app.collectors.api_jobs import ApiJobsCollector

_REGISTRY = {
    "api_jobs": ApiJobsCollector,
    # M3에서 추가 예정: static_html, detail_html
}


def get_collector(collector_type: str):
    if collector_type not in _REGISTRY:
        raise KeyError(f"Unknown collector type: {collector_type}")
    return _REGISTRY[collector_type]()
