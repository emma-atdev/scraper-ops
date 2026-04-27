"""ValidationIssue들을 사람이 이해할 수 있는 실패 범주로 변환한다."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.collectors.base import ValidationIssue


class FailureCategory(str, Enum):
    NONE = "none"
    NETWORK_BLOCKED = "network_blocked"
    EMPTY_RESULTS = "empty_results"
    SCHEMA_CHANGE = "schema_change"
    VOLUME_DROP = "volume_drop"
    MISSING_FIELDS = "missing_fields"
    UNKNOWN = "unknown"


@dataclass
class Diagnosis:
    category: FailureCategory
    summary: str
    issue_codes: list[str]


_CATEGORY_BY_CODE = {
    "fetch_failed": FailureCategory.NETWORK_BLOCKED,
    "empty_results": FailureCategory.EMPTY_RESULTS,
    "items_path_not_list": FailureCategory.SCHEMA_CHANGE,
    "missing_external_id": FailureCategory.SCHEMA_CHANGE,
    "missing_required_field": FailureCategory.MISSING_FIELDS,
    "volume_drop": FailureCategory.VOLUME_DROP,
}


def classify_failure(issues: list[ValidationIssue]) -> Diagnosis:
    if not issues:
        return Diagnosis(category=FailureCategory.NONE, summary="no issues", issue_codes=[])

    codes = [i.code for i in issues]
    # 우선순위: network > schema > fields > volume > empty > unknown
    priority = [
        FailureCategory.NETWORK_BLOCKED,
        FailureCategory.SCHEMA_CHANGE,
        FailureCategory.MISSING_FIELDS,
        FailureCategory.VOLUME_DROP,
        FailureCategory.EMPTY_RESULTS,
    ]
    seen = {_CATEGORY_BY_CODE.get(c, FailureCategory.UNKNOWN) for c in codes}
    for cat in priority:
        if cat in seen:
            summary = ", ".join(sorted({c for c in codes if _CATEGORY_BY_CODE.get(c) == cat})[:5])
            return Diagnosis(category=cat, summary=summary, issue_codes=codes)
    return Diagnosis(category=FailureCategory.UNKNOWN, summary=", ".join(codes[:5]), issue_codes=codes)
