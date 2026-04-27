"""도메인 model. MVP는 채용공고(JobPosting)를 normalized view로 둔다.

장기적으로 records/record_details domain-neutral 모델로 확장 검토. (product-plan 참조)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class JobPosting:
    external_id: str
    site: str
    title: str | None = None
    company: str | None = None
    deadline: str | None = None
    link: str | None = None
    raw: dict[str, Any] | None = None

    def content_hash(self, fields: list[str] | None = None) -> str:
        """변경 감지용 SHA-256 hash. fields 미지정 시 기본 핵심 필드.

        product-plan: 핵심 필드(제목·마감일·상태·회사명)를 이어붙인 문자열의 SHA-256.
        """
        fields = fields or ["title", "company", "deadline"]
        parts = [str(getattr(self, f, "") or "") for f in fields]
        joined = "".join(parts)  # unit separator로 안전 join
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def raw_json(self) -> str:
        return json.dumps(self.raw or {}, ensure_ascii=False, sort_keys=True)


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
