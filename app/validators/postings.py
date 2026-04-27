"""수집 결과 검증.

기획서 MVP 기본 검증 항목:
- 결과 0건
- 필수 필드 누락
- 수집량 급감
- 응답 구조 변경 (M3 범위 밖, M6 self-healing에서)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.collectors.base import ValidationIssue
from app.config.schema import ValidationConfig
from app.models import JobPosting


@dataclass
class ValidationOutcome:
    ok: bool
    issues: list[ValidationIssue] = field(default_factory=list)


def validate_postings(
    postings: Iterable[JobPosting],
    config: ValidationConfig,
    *,
    previous_count: int | None = None,
) -> ValidationOutcome:
    items = list(postings)
    issues: list[ValidationIssue] = []

    if len(items) < max(config.min_items_per_page, 1) and not items:
        issues.append(
            ValidationIssue(
                code="empty_results",
                message="no items collected",
                context={"count": 0},
            )
        )

    for idx, p in enumerate(items):
        for field_name in config.required_fields:
            value = getattr(p, field_name, None)
            if not value:
                issues.append(
                    ValidationIssue(
                        code="missing_required_field",
                        message=f"required field '{field_name}' missing",
                        context={"index": idx, "external_id": p.external_id, "field": field_name},
                    )
                )
                break  # 한 record당 하나만 보고

    if previous_count is not None and previous_count > 0 and items:
        ratio = len(items) / previous_count
        if ratio < (1 - config.max_volume_drop_ratio):
            issues.append(
                ValidationIssue(
                    code="volume_drop",
                    message=f"collected {len(items)} vs previous {previous_count}",
                    context={"current": len(items), "previous": previous_count, "ratio": ratio},
                )
            )

    return ValidationOutcome(ok=not issues, issues=issues)
