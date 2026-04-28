"""운영 결정의 append-only audit log.

CLAUDE.md 안전 규칙: approval 전이, allowlist 변경, patch 적용 같은 운영 결정은
data/audit.log에 한 줄씩(JSON Lines) 추가한다. 읽기 전용으로 다루고 수정·삭제하지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.clock import now_kst_iso

DEFAULT_AUDIT_PATH = Path("data/audit.log")


def audit_log(event: str, *, path: str | Path | None = None, **fields: Any) -> None:
    """audit.log에 한 줄 append. 시각은 KST.

    Args:
        event: 이벤트 이름 (예: "approval_created").
        path: 로그 파일 경로. 미지정 시 data/audit.log.
        **fields: 추가 컨텍스트 필드. JSON 직렬화 가능해야 함.
    """
    target = Path(path) if path else DEFAULT_AUDIT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    line = json.dumps(
        {"ts": now_kst_iso(), "event": event, **fields},
        ensure_ascii=False,
        sort_keys=False,
    )
    with target.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
