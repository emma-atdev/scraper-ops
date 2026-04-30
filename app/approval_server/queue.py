"""슬랙 버튼 클릭으로 들어온 결정을 큐에 쌓고, Mac이 polling으로 가져가는 구조.

저장소: data/approval_queue.jsonl (JSON Lines, append-only)
- POST 들어오면 한 줄 append
- Mac이 GET /decisions/pending으로 미처리 결정 list 받음
- 처리 완료 후 Mac이 POST /decisions/{queue_id}/ack로 ack
- ack는 별도 ack 파일(data/approval_queue.ack)에 queue_id를 append하는 식

이 구조의 장점: append-only로 race-safe, 큐 파일 자체는 영구 보존(감사 추적), Mac↔VM은 순수 HTTP.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger("scraper.approval_server.queue")

DecisionKind = Literal["approve", "reject", "regenerate"]


@dataclass
class QueuedDecision:
    queue_id: str            # 큐 고유 id (uuid)
    approval_id: int         # approval_request DB row id
    kind: DecisionKind
    by: str                  # "slack:U123ABC"
    reason: str | None       # reject 사유 등
    received_at_iso: str     # UTC ISO


class DecisionQueue:
    """JSON Lines 기반 결정 큐.

    동시성: append는 OS의 O_APPEND로 atomic. 단 같은 프로세스 내 race를 막기 위해
    process-local lock을 둔다 (http.server는 ThreadingHTTPServer라 thread 동시 호출 가능).
    """

    def __init__(self, queue_path: str | Path, ack_path: str | Path | None = None):
        self.queue_path = Path(queue_path)
        self.ack_path = Path(ack_path) if ack_path else self.queue_path.with_suffix(".ack")
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def enqueue(
        self,
        *,
        approval_id: int,
        kind: DecisionKind,
        by: str,
        reason: str | None = None,
    ) -> QueuedDecision:
        decision = QueuedDecision(
            queue_id=uuid.uuid4().hex,
            approval_id=approval_id,
            kind=kind,
            by=by,
            reason=reason,
            received_at_iso=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        line = json.dumps(asdict(decision), ensure_ascii=False) + "\n"
        with self._lock:
            with self.queue_path.open("a", encoding="utf-8") as f:
                f.write(line)
        logger.info(
            "decision enqueued",
            extra={"event": "decision_enqueued", "queue_id": decision.queue_id,
                   "approval_id": approval_id, "kind": kind},
        )
        return decision

    def list_pending(self) -> list[QueuedDecision]:
        """ack되지 않은 결정 list. 호출 시점에 한 번 스캔."""
        acked = self._load_acked_ids()
        out: list[QueuedDecision] = []
        if not self.queue_path.exists():
            return out
        with self.queue_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                qid = data.get("queue_id")
                if qid and qid not in acked:
                    out.append(QueuedDecision(**data))
        return out

    def ack(self, queue_id: str) -> bool:
        """queue_id를 ack 파일에 추가. 이미 ack된 경우에도 idempotent (True 반환)."""
        with self._lock:
            self.ack_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ack_path.open("a", encoding="utf-8") as f:
                f.write(queue_id + "\n")
        logger.info(
            "decision acked",
            extra={"event": "decision_acked", "queue_id": queue_id},
        )
        return True

    # -------- internal --------

    def _load_acked_ids(self) -> set[str]:
        if not self.ack_path.exists():
            return set()
        with self.ack_path.open("r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
