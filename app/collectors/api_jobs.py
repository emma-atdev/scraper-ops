"""ApiJobsCollector — JSON API 반복 호출로 목록을 수집하는 generic collector.

YAML config에 따라 동작하며 사이트 이름을 알지 못한다.
fetch → items_path 추출 → field mapping → link_template 치환 → JobPosting 변환.
"""

from __future__ import annotations

import logging
from typing import Any

import jmespath

from app.collectors.base import BaseFetcher, CollectorResult, ValidationIssue
from app.collectors.fetchers import HttpFetcher
from app.config.schema import CollectorConfig
from app.models import JobPosting

logger = logging.getLogger(__name__)


class ApiJobsCollector:
    def run(
        self,
        config: CollectorConfig,
        *,
        site: str,
        fetcher: BaseFetcher | None = None,
    ) -> CollectorResult:
        if config.type != "api_jobs":
            raise ValueError(f"ApiJobsCollector cannot handle type={config.type}")
        if config.request is None or config.mapping is None:
            raise ValueError("api_jobs requires request and mapping config")

        fetcher = fetcher or HttpFetcher()

        records: list[JobPosting] = []
        issues: list[ValidationIssue] = []
        evidence: dict[str, Any] = {"pages": [], "first_page_sample": None}

        page = config.pagination.start
        pages_done = 0

        while pages_done < config.pagination.max_pages:
            params = dict(config.request.params)
            if config.pagination.type == "page" and config.pagination.param:
                params[config.pagination.param] = page

            result = fetcher.fetch(
                config.request.url,
                method=config.request.method,
                params=params,
                headers=config.request.headers or None,
            )

            evidence["pages"].append(
                {"page": page, "status": result.status, "blocked": result.blocked}
            )

            if result.blocked or result.status >= 400:
                issues.append(
                    ValidationIssue(
                        code="fetch_failed",
                        message=f"page {page} status={result.status} blocked={result.blocked}",
                        context={"page": page, "status": result.status, "blocked": result.blocked},
                    )
                )
                break

            data = result.json or {}
            if pages_done == 0:
                evidence["first_page_sample"] = data

            items = jmespath.search(config.mapping.items_path, data) or []
            if not isinstance(items, list):
                issues.append(
                    ValidationIssue(
                        code="items_path_not_list",
                        message=f"items_path resolved to non-list: {type(items).__name__}",
                        context={"items_path": config.mapping.items_path},
                    )
                )
                break

            if not items:
                if config.pagination.stop_condition == "empty_items":
                    break

            for item in items:
                posting = self._extract(item, config.mapping, site=site)
                if not posting.external_id:
                    issues.append(
                        ValidationIssue(
                            code="missing_external_id",
                            message="item has no external_id",
                            context={"page": page},
                        )
                    )
                    continue
                records.append(posting)

            pages_done += 1
            page += 1

        evidence["pages_collected"] = pages_done
        evidence["records_collected"] = len(records)

        return CollectorResult(records=records, issues=issues, evidence=evidence)  # type: ignore[arg-type]

    @staticmethod
    def _extract(item: dict[str, Any], mapping, *, site: str) -> JobPosting:
        def pick(path: str | None) -> Any:
            if not path:
                return None
            return jmespath.search(path, item)

        external_id = pick(mapping.fields.get("external_id"))
        title = pick(mapping.fields.get("title"))
        company = pick(mapping.fields.get("company"))
        deadline = pick(mapping.fields.get("deadline"))
        link = pick(mapping.fields.get("link"))

        if not link and mapping.link_template:
            try:
                link = mapping.link_template.format(**item)
            except (KeyError, IndexError, TypeError):
                link = None

        return JobPosting(
            external_id=str(external_id) if external_id is not None else "",
            site=site,
            title=str(title) if title is not None else None,
            company=str(company) if company is not None else None,
            deadline=str(deadline) if deadline is not None else None,
            link=str(link) if link is not None else None,
            raw=item if isinstance(item, dict) else None,
        )
