"""approval_request 도메인 모델.

DB row를 그대로 매핑한 dataclass. patch_json/dry_run_json은 직렬화 형태로 들고 있다가
필요 시 호출자가 PatchCandidate.model_validate_json 등으로 복원한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]


@dataclass
class ApprovalRequest:
    id: int
    run_id: str
    site: str
    status: ApprovalStatus
    patch_json: str
    dry_run_json: str | None
    slack_thread_ts: str | None
    slack_channel: str | None
    created_at: str
    expires_at: str
    decided_at: str | None
    decided_by: str | None
    decision_reason: str | None
