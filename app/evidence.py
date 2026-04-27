"""Evidence 저장 + report 생성 + 보관 정책.

기획서 디렉토리 구조:
  data/reports/{site}/{run_id}/report.json
  data/snapshots/{site}/{run_id}/api_sample.json
                                  api_sample_prev.json
                                  schema_diff.json
                                  static.html
                                  detail_{id}.html
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.collectors.base import ValidationIssue
from app.diagnosis.classifier import Diagnosis


def _serialize(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(x) for x in obj]
    return obj


def _safe_run_id(run_id: str) -> str:
    # run_id에 ":" 등이 들어가는 경우 디렉토리 안전 변환
    return run_id.replace(":", "_").replace("/", "_")


class EvidenceStore:
    def __init__(self, base_dir: str | Path = "data"):
        self.base = Path(base_dir)

    def _snapshot_dir(self, site: str, run_id: str) -> Path:
        d = self.base / "snapshots" / site / _safe_run_id(run_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _report_dir(self, site: str, run_id: str) -> Path:
        d = self.base / "reports" / site / _safe_run_id(run_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_api_sample(self, site: str, run_id: str, sample: Any) -> Path:
        path = self._snapshot_dir(site, run_id) / "api_sample.json"
        path.write_text(json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def promote_prev_sample(self, site: str, run_id: str) -> Path | None:
        """현재 run의 sample을 마지막 정상 sample로 복사한다.
        diagnostics 시 schema diff 비교에 사용.
        """
        cur = self._snapshot_dir(site, run_id) / "api_sample.json"
        if not cur.exists():
            return None
        prev = self.base / "snapshots" / site / "api_sample_prev.json"
        prev.parent.mkdir(parents=True, exist_ok=True)
        prev.write_text(cur.read_text(encoding="utf-8"), encoding="utf-8")
        return prev

    def write_report(
        self,
        site: str,
        run_id: str,
        *,
        status: str,
        records_count: int,
        issues: list[ValidationIssue],
        diagnosis: Diagnosis | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Path:
        report = {
            "run_id": run_id,
            "site": site,
            "status": status,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "records_count": records_count,
            "issues": [_serialize(i) for i in issues],
            "diagnosis": _serialize(diagnosis) if diagnosis else None,
            "meta": meta or {},
        }
        path = self._report_dir(site, run_id) / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ------------- 보관 정책 -------------

    def cleanup(
        self,
        *,
        snapshot_days: int = 30,
        success_snapshot_days: int = 7,
        report_days: int = 90,
        max_snapshot_bytes: int = 500 * 1024 * 1024,
    ) -> dict[str, int]:
        removed = {"snapshots": 0, "reports": 0}
        now = time.time()

        snap_root = self.base / "snapshots"
        if snap_root.exists():
            removed["snapshots"] += self._cleanup_age(snap_root, now, snapshot_days * 86400)
            removed["snapshots"] += self._cleanup_size(snap_root, max_snapshot_bytes)

        rep_root = self.base / "reports"
        if rep_root.exists():
            removed["reports"] += self._cleanup_age(rep_root, now, report_days * 86400)

        return removed

    @staticmethod
    def _cleanup_age(root: Path, now: float, max_age_seconds: float) -> int:
        removed = 0
        for site_dir in root.iterdir():
            if not site_dir.is_dir():
                continue
            for run_dir in site_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                try:
                    mtime = run_dir.stat().st_mtime
                except OSError:
                    continue
                if now - mtime > max_age_seconds:
                    _rm_tree(run_dir)
                    removed += 1
        return removed

    @staticmethod
    def _cleanup_size(root: Path, max_bytes: int) -> int:
        entries: list[tuple[float, Path, int]] = []
        total = 0
        for site_dir in root.iterdir():
            if not site_dir.is_dir():
                continue
            for run_dir in site_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                try:
                    mtime = run_dir.stat().st_mtime
                    size = sum(p.stat().st_size for p in run_dir.rglob("*") if p.is_file())
                except OSError:
                    continue
                entries.append((mtime, run_dir, size))
                total += size
        if total <= max_bytes:
            return 0
        entries.sort()  # 오래된 순
        removed = 0
        for _, run_dir, size in entries:
            if total <= max_bytes:
                break
            _rm_tree(run_dir)
            total -= size
            removed += 1
        return removed


def _rm_tree(p: Path) -> None:
    if not p.exists():
        return
    for child in p.rglob("*"):
        if child.is_file():
            try:
                child.unlink()
            except OSError:
                pass
    for child in sorted(p.rglob("*"), reverse=True):
        if child.is_dir():
            try:
                child.rmdir()
            except OSError:
                pass
    try:
        p.rmdir()
    except OSError:
        pass
