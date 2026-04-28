"""dry-run: PatchCandidate를 실제 yaml에 쓰거나 사이트에 요청을 보내지 않고
저장된 sample payload로 ApiJobsCollector를 두 번(patch 전·후) 실행해 비교한다.

호출자(M6.5 Slack message builder, M6.6 patcher)는 이 모듈의 결과만으로
"이 patch를 사람에게 승인 요청할 가치가 있는가"를 판단할 수 있다.

이 모듈은 LLM을 호출하지 않는다. PatchCandidate는 인자로만 받는다.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from app.collectors.api_jobs import ApiJobsCollector
from app.collectors.base import BaseFetcher, CollectorResult, FetchResult
from app.config.schema import SiteConfig
from app.llm.schemas import PatchCandidate, PatchOperation

logger = logging.getLogger("scraper.healing.dry_run")

Verdict = Literal[
    "improved",
    "regressed",
    "unchanged",
    "patch_invalid",
    "patch_apply_failed",
]

SAMPLE_RECORD_LIMIT = 5


class PatchApplyError(RuntimeError):
    """patch path가 yaml dict에서 찾아지지 않거나 op이 적용 불가."""


# ---------- patch 적용 ----------

def apply_patch(yaml_dict: dict[str, Any], patch: PatchCandidate) -> dict[str, Any]:
    """PatchCandidate.changes를 yaml_dict의 deep copy에 적용해 새 dict를 돌려준다.

    - changes가 비어 있으면 deep copy만 돌려준다.
    - path는 dot-separated. list index는 지원하지 않는다 (현재 모든 yaml이 dict only).
    - replace/add/remove의 의미:
      * replace: path가 가리키는 키가 이미 있어야 한다. 없으면 PatchApplyError.
      * add: path의 부모 dict까지는 존재해야 한다. 마지막 키는 없어도 된다.
      * remove: path가 가리키는 키가 있어야 한다. 없으면 PatchApplyError.
    """
    new_dict = copy.deepcopy(yaml_dict)
    for op in patch.changes:
        _apply_one(new_dict, op)
    return new_dict


def _apply_one(root: dict[str, Any], op: PatchOperation) -> None:
    parts = [p for p in op.path.split(".") if p]
    if not parts:
        raise PatchApplyError(f"empty path: {op.path!r}")

    parent = _navigate_parent(root, parts[:-1], op.path)
    last = parts[-1]

    if op.op == "replace":
        if last not in parent:
            raise PatchApplyError(f"replace target not found: {op.path!r}")
        parent[last] = op.new
    elif op.op == "add":
        parent[last] = op.new
    elif op.op == "remove":
        if last not in parent:
            raise PatchApplyError(f"remove target not found: {op.path!r}")
        del parent[last]
    else:  # pragma: no cover - PatchOperation.op이 Literal로 강제됨
        raise PatchApplyError(f"unsupported op: {op.op!r}")


def _navigate_parent(root: dict[str, Any], parts: list[str], full_path: str) -> dict[str, Any]:
    cursor: Any = root
    for key in parts:
        if not isinstance(cursor, dict) or key not in cursor:
            raise PatchApplyError(f"path not found: {full_path!r} (missing {key!r})")
        cursor = cursor[key]
    if not isinstance(cursor, dict):
        raise PatchApplyError(f"path parent is not a dict: {full_path!r}")
    return cursor


# ---------- 가짜 fetcher ----------

class FakeFetcher:
    """저장된 sample payload를 page 순서대로 돌려주는 가짜 fetcher.

    실제 HTTP·headers·params는 모두 무시한다. dry-run용.
    pages가 떨어지면 빈 dict를 돌려주므로 collector는 stop_condition=empty_items로
    자연스럽게 종료한다.
    """

    def __init__(self, pages: list[dict[str, Any]]):
        self._pages = list(pages)
        self._index = 0
        self.calls: list[dict[str, Any]] = []

    def fetch(self, url: str, *, method: str = "GET", **kwargs: Any) -> FetchResult:
        self.calls.append({"url": url, "method": method, **kwargs})
        if self._index < len(self._pages):
            payload = self._pages[self._index]
            self._index += 1
        else:
            payload = {}
        return FetchResult(
            status=200,
            headers={},
            text="",
            json=payload,
            blocked=False,
            url=url,
        )


# ---------- 결과 ----------

@dataclass
class DryRunResult:
    verdict: Verdict
    before_count: int = 0
    after_count: int = 0
    before_missing_required: int = 0
    after_missing_required: int = 0
    before_issues: list[str] = field(default_factory=list)
    after_issues: list[str] = field(default_factory=list)
    sample_records: list[dict[str, Any]] = field(default_factory=list)
    patch_invalid_reason: str | None = None
    patch_apply_failed_reason: str | None = None


# ---------- main ----------

def run_dry_run(
    *,
    site: str,
    yaml_text: str,
    patch: PatchCandidate,
    api_sample: dict[str, Any] | list[dict[str, Any]],
    collector_name: str = "jobs",
) -> DryRunResult:
    """주어진 patch를 yaml에 적용하고 sample payload로 collector를 두 번 돌려 비교한다.

    Args:
        site: 사이트 식별자 (JobPosting.site에 들어갈 값).
        yaml_text: 현재 yaml의 문자열 (load_yaml_text로 읽은 것).
        patch: LLM이 생성한 PatchCandidate.
        api_sample: 단일 page dict 또는 page list.
        collector_name: yaml 안의 collectors 키. catch는 "jobs".

    Returns:
        DryRunResult. verdict는 5종 중 하나.
    """
    pages = api_sample if isinstance(api_sample, list) else [api_sample]

    base_dict = yaml.safe_load(yaml_text) or {}
    base_config = SiteConfig.model_validate(base_dict)
    base_collector_cfg = _get_collector(base_config, collector_name)

    # 1. patch 적용 시도
    try:
        patched_dict = apply_patch(base_dict, patch)
    except PatchApplyError as e:
        logger.info(
            "dry-run patch apply failed",
            extra={"event": "dry_run_apply_failed", "site": site, "reason": str(e)},
        )
        return DryRunResult(
            verdict="patch_apply_failed",
            patch_apply_failed_reason=str(e),
        )

    # 2. patched yaml 스키마 검증
    try:
        patched_config = SiteConfig.model_validate(patched_dict)
    except ValidationError as e:
        logger.info(
            "dry-run patched yaml invalid",
            extra={"event": "dry_run_patch_invalid", "site": site},
        )
        return DryRunResult(
            verdict="patch_invalid",
            patch_invalid_reason=str(e),
        )

    patched_collector_cfg = _get_collector(patched_config, collector_name)

    # 3. collector 두 번 실행 (before, after)
    before = _run_collector(base_collector_cfg, site=site, pages=pages)
    after = _run_collector(patched_collector_cfg, site=site, pages=pages)

    before_count = len(before.records)
    after_count = len(after.records)
    before_missing = _count_missing_required(before, base_collector_cfg)
    after_missing = _count_missing_required(after, patched_collector_cfg)

    verdict = _verdict(
        before_count=before_count,
        after_count=after_count,
        before_missing=before_missing,
        after_missing=after_missing,
    )

    sample_records = [
        _record_to_dict(r) for r in after.records[:SAMPLE_RECORD_LIMIT]
    ]

    logger.info(
        "dry-run complete",
        extra={
            "event": "dry_run_complete",
            "site": site,
            "verdict": verdict,
            "before_count": before_count,
            "after_count": after_count,
            "before_missing": before_missing,
            "after_missing": after_missing,
        },
    )

    return DryRunResult(
        verdict=verdict,
        before_count=before_count,
        after_count=after_count,
        before_missing_required=before_missing,
        after_missing_required=after_missing,
        before_issues=[i.code for i in before.issues],
        after_issues=[i.code for i in after.issues],
        sample_records=sample_records,
    )


# ---------- internal ----------

def _get_collector(config: SiteConfig, name: str):
    if name not in config.collectors:
        raise KeyError(f"collector {name!r} not in yaml; available={list(config.collectors)}")
    cfg = config.collectors[name]
    if cfg.type != "api_jobs":
        raise NotImplementedError(
            f"dry-run only supports api_jobs (got {cfg.type!r}); other types are out of scope for M6.3"
        )
    return cfg


def _run_collector(cfg, *, site: str, pages: list[dict[str, Any]]) -> CollectorResult:
    fetcher: BaseFetcher = FakeFetcher(pages)
    return ApiJobsCollector().run(cfg, site=site, fetcher=fetcher)


def _count_missing_required(result: CollectorResult, cfg) -> int:
    """validation.required_fields 중 None/빈 문자열인 record 수."""
    required = cfg.validation.required_fields
    if not required:
        return 0
    missing = 0
    for r in result.records:
        if any(_is_empty(getattr(r, name, None)) for name in required):
            missing += 1
    return missing


def _is_empty(value: Any) -> bool:
    return value is None or value == ""


def _verdict(
    *, before_count: int, after_count: int, before_missing: int, after_missing: int
) -> Verdict:
    if after_count > before_count:
        return "improved"
    if after_count < before_count:
        return "regressed"
    # 건수 동률 → missing으로 tie-break
    if after_missing < before_missing:
        return "improved"
    if after_missing > before_missing:
        return "regressed"
    return "unchanged"


def _record_to_dict(record) -> dict[str, Any]:
    return {
        "external_id": record.external_id,
        "title": record.title,
        "company": record.company,
        "deadline": record.deadline,
        "link": record.link,
    }
