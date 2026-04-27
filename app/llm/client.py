"""OpenAI LLM client wrapper.

책임:
- API key 환경변수 로드 + 미설정 시 no-op
- structured output (JSON schema 강제)
- 스키마 검증 실패 시 최대 N회 retry
- Capability Matrix 위반 시 거부 + retry
- 시크릿 마스킹은 호출자 책임 (evidence 만들 때 처리)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.violations import Violation, detect_violations

logger = logging.getLogger("scraper.llm")

DEFAULT_MODEL = "gpt-4.1"
DEFAULT_MAX_RETRIES = 2

T = TypeVar("T", bound=BaseModel)


class LLMNotConfiguredError(RuntimeError):
    pass


class LLMOutputRejectedError(RuntimeError):
    """Capability Matrix 위반이 누적 한도까지 발견된 경우."""

    def __init__(self, violations: list[Violation]):
        self.violations = violations
        super().__init__(f"capability matrix violations: {[v.label for v in violations]}")


@dataclass
class LLMConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    max_retries: int = DEFAULT_MAX_RETRIES

    @classmethod
    def from_env(cls) -> "LLMConfig | None":
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            return None
        model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
        return cls(api_key=key, model=model)


class LLMClient:
    """OpenAI 호출 + 구조화 출력 + retry 캡슐화."""

    def __init__(self, config: LLMConfig | None):
        self.config = config
        self._client = None  # lazy import (테스트에서 openai 미설치 환경 보호)

    @property
    def enabled(self) -> bool:
        return self.config is not None

    def _ensure_client(self):
        if self._client is None:
            if self.config is None:
                raise LLMNotConfiguredError("OPENAI_API_KEY not set")
            from openai import OpenAI

            self._client = OpenAI(api_key=self.config.api_key)
        return self._client

    def parse(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        max_retries: int | None = None,
    ) -> T:
        """structured output 호출 + 스키마·violation 검증을 통과한 결과를 반환.

        실패 시 LLMNotConfiguredError(설정 없음) / LLMOutputRejectedError(매트릭스 위반)
        / pydantic ValidationError(스키마 실패)를 raise.
        """
        if not self.enabled:
            raise LLMNotConfiguredError("OPENAI_API_KEY not set")

        retries = self.config.max_retries if max_retries is None else max_retries
        last_error: Exception | None = None
        accumulated_feedback = ""

        for attempt in range(retries + 1):
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt + accumulated_feedback},
            ]
            try:
                raw = self._call_with_schema(messages, response_model)
                parsed = response_model.model_validate(raw)
            except ValidationError as e:
                last_error = e
                accumulated_feedback = (
                    f"\n\n이전 출력은 schema 검증을 실패했다: {e.errors()[:3]}. "
                    "지정된 schema를 정확히 따라 다시 작성하라."
                )
                logger.warning(
                    "schema validation failed, retrying",
                    extra={"event": "llm_retry_schema", "attempt": attempt, "error": str(e)},
                )
                continue

            # Capability Matrix violation 검사
            text_to_check = self._collect_text(parsed)
            violations = detect_violations(text_to_check)
            if violations:
                last_error = LLMOutputRejectedError(violations)
                accumulated_feedback = (
                    "\n\n이전 출력은 Capability Matrix 금지 제안을 포함했다: "
                    f"{[v.label for v in violations]}. "
                    "이 카테고리의 제안 없이 다시 작성하라."
                )
                logger.warning(
                    "capability matrix violation, retrying",
                    extra={
                        "event": "llm_retry_violation",
                        "attempt": attempt,
                        "violations": [v.label for v in violations],
                    },
                )
                continue

            logger.info(
                "llm parse ok",
                extra={"event": "llm_parse_ok", "attempts": attempt + 1, "model": self.config.model},
            )
            return parsed

        # retry 모두 실패
        assert last_error is not None
        raise last_error

    # -------- internal --------

    def _call_with_schema(self, messages: list[dict], response_model: type[T]) -> dict:
        """OpenAI chat.completions.create with json_schema response_format. 응답을 dict로 반환."""
        client = self._ensure_client()
        schema = response_model.model_json_schema()
        # OpenAI strict json_schema는 추가 제약이 있음. extra="forbid"로 model_config 둔 것을
        # 그대로 활용하되, additionalProperties 명시.
        schema["additionalProperties"] = False

        resp = client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "schema": schema,
                    "strict": True,
                },
            },
        )
        content = resp.choices[0].message.content or ""
        return json.loads(content)

    @staticmethod
    def _collect_text(model: BaseModel) -> str:
        """patch 객체에 들어간 사용자 텍스트(reason 등)를 모아 violation 검사용 문자열로."""
        try:
            return json.dumps(model.model_dump(), ensure_ascii=False)
        except Exception:
            return str(model)
