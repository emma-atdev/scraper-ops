"""healing entry point. evidence + 현재 YAML → PatchCandidate.

LLM 호출, schema 검증, Capability Matrix 위반 검출 등 위험 처리는 LLMClient에 위임한다.
이 모듈은 prompt 조립과 결과 정제만 한다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.healing.builder import build_user_prompt, load_yaml_text
from app.llm import (
    LLMClient,
    LLMConfig,
    LLMNotConfiguredError,
    PatchCandidate,
    build_system_prompt,
)

logger = logging.getLogger("scraper.healing")


def generate_patch_candidate(
    *,
    site: str,
    yaml_path: str | Path,
    evidence: dict[str, Any],
    client: LLMClient | None = None,
    previous_attempts: list[dict[str, Any]] | None = None,
) -> PatchCandidate:
    """주어진 evidence를 LLM에 보내 PatchCandidate를 받아온다.

    호출자 책임:
    - evidence에는 마스킹된 값만 포함되어 있어야 한다 (시크릿 노출 금지).
    - yaml_path는 실제 파일이어야 한다.

    반환:
    - PatchCandidate. changes가 빈 배열이면 LLM이 "patch 불가능 또는 사람 개입 필요"로
      판정한 것 (reason 참조).

    예외:
    - LLMNotConfiguredError: OPENAI_API_KEY 미설정.
    - pydantic ValidationError / LLMOutputRejectedError: LLM이 retry까지 통과 못함.
    """
    client = client or LLMClient(LLMConfig.from_env())
    if not client.enabled:
        raise LLMNotConfiguredError(
            "healing requires OPENAI_API_KEY; set it in .env or VM EnvironmentFile"
        )

    yaml_text = load_yaml_text(yaml_path)
    user_prompt = build_user_prompt(
        site=site,
        yaml_text=yaml_text,
        evidence={**evidence, "yaml_path": str(yaml_path)},
        previous_attempts=previous_attempts,
    )

    candidate = client.parse(
        system_prompt=build_system_prompt(),
        user_prompt=user_prompt,
        response_model=PatchCandidate,
    )

    logger.info(
        "patch candidate generated",
        extra={
            "event": "patch_candidate_generated",
            "site": site,
            "feasible": bool(candidate.changes),
            "risk": candidate.risk,
            "change_count": len(candidate.changes),
        },
    )
    return candidate
