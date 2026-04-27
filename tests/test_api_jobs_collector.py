"""ApiJobsCollector 단위 테스트. 실제 Scrapling은 호출하지 않고 FakeFetcher 주입."""

from __future__ import annotations

from typing import Any

from app.collectors.api_jobs import ApiJobsCollector
from app.collectors.base import FetchResult
from app.config.schema import (
    CollectorConfig,
    MappingConfig,
    PaginationConfig,
    RequestConfig,
)


class FakeFetcher:
    def __init__(self, pages: list[dict[str, Any]]):
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def fetch(self, url: str, *, method: str = "GET", **kwargs: Any) -> FetchResult:
        self.calls.append({"url": url, "method": method, **kwargs})
        idx = len(self.calls) - 1
        if idx >= len(self._pages):
            return FetchResult(status=200, headers={}, json={"recruitData": []})
        return FetchResult(status=200, headers={}, json=self._pages[idx])


def make_config() -> CollectorConfig:
    return CollectorConfig(
        type="api_jobs",
        fetcher="http",
        purpose="postings",
        request=RequestConfig(
            method="GET",
            url="https://api.example.test/jobs",
            params={"onRecruitYN": "Y"},
        ),
        pagination=PaginationConfig(
            type="page",
            param="curpage",
            start=1,
            max_pages=5,
            stop_condition="empty_items",
        ),
        mapping=MappingConfig(
            items_path="recruitData",
            link_template="https://example.test/job/{RecruitID}",
            fields={
                "external_id": "RecruitID",
                "title": "RecruitTitle",
                "company": "CompName",
                "deadline": "ApplyEndDatetime",
            },
        ),
    )


def test_collects_and_paginates_until_empty():
    pages = [
        {"recruitData": [
            {"RecruitID": "1", "RecruitTitle": "Backend", "CompName": "Acme", "ApplyEndDatetime": "2026-12-31"},
            {"RecruitID": "2", "RecruitTitle": "Frontend", "CompName": "Acme", "ApplyEndDatetime": "2026-12-31"},
        ]},
        {"recruitData": [
            {"RecruitID": "3", "RecruitTitle": "Data", "CompName": "Bee", "ApplyEndDatetime": "2026-11-30"},
        ]},
        {"recruitData": []},  # empty → stop
    ]
    fetcher = FakeFetcher(pages)
    result = ApiJobsCollector().run(make_config(), site="catch", fetcher=fetcher)

    assert len(result.records) == 3
    assert [r.external_id for r in result.records] == ["1", "2", "3"]
    assert result.records[0].link == "https://example.test/job/1"
    assert result.records[0].title == "Backend"
    assert result.records[0].site == "catch"
    assert len(fetcher.calls) == 3
    assert fetcher.calls[0]["params"]["curpage"] == 1
    assert fetcher.calls[1]["params"]["curpage"] == 2
    assert fetcher.calls[2]["params"]["curpage"] == 3
    assert not result.issues


def test_stops_on_blocked_response():
    pages = [{"recruitData": [{"RecruitID": "1", "RecruitTitle": "x", "CompName": "y", "ApplyEndDatetime": "z"}]}]

    class BlockingFetcher:
        def __init__(self):
            self.n = 0

        def fetch(self, url, *, method="GET", **kwargs):
            self.n += 1
            if self.n == 1:
                return FetchResult(status=200, headers={}, json=pages[0])
            return FetchResult(status=403, headers={}, blocked=True)

    fetcher = BlockingFetcher()
    result = ApiJobsCollector().run(make_config(), site="catch", fetcher=fetcher)
    assert len(result.records) == 1
    assert any(i.code == "fetch_failed" for i in result.issues)


def test_missing_external_id_becomes_issue():
    pages = [{"recruitData": [{"RecruitTitle": "no id"}]}, {"recruitData": []}]
    fetcher = FakeFetcher(pages)
    result = ApiJobsCollector().run(make_config(), site="catch", fetcher=fetcher)
    assert result.records == []
    assert any(i.code == "missing_external_id" for i in result.issues)


def test_max_pages_respected():
    cfg = make_config()
    cfg.pagination.max_pages = 2
    cfg.pagination.stop_condition = "fixed_pages"
    pages = [
        {"recruitData": [{"RecruitID": "1", "RecruitTitle": "a", "CompName": "x", "ApplyEndDatetime": "z"}]},
        {"recruitData": [{"RecruitID": "2", "RecruitTitle": "b", "CompName": "x", "ApplyEndDatetime": "z"}]},
        {"recruitData": [{"RecruitID": "3", "RecruitTitle": "c", "CompName": "x", "ApplyEndDatetime": "z"}]},
    ]
    fetcher = FakeFetcher(pages)
    result = ApiJobsCollector().run(cfg, site="catch", fetcher=fetcher)
    assert len(result.records) == 2
    assert len(fetcher.calls) == 2
