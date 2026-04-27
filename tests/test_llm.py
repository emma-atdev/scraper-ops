"""LLM 모듈 단위 테스트. OpenAI API는 호출하지 않는다 (mock으로 분리)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import BaseModel, ConfigDict

from app.llm import (
    LLMClient,
    LLMConfig,
    LLMNotConfiguredError,
    LLMOutputRejectedError,
    PatchCandidate,
    build_system_prompt,
    detect_violations,
    extract_capability_matrix,
)
from app.llm.client import DEFAULT_MODEL


# ---------- prompts ----------

def test_extract_capability_matrix_returns_section():
    text = extract_capability_matrix("docs/product-plan.md")
    assert "Scrapling Capability Matrix" in text
    assert "Green Zone" in text
    assert "Yellow Zone" in text
    assert "Red Zone" in text
    assert "금지 제안" in text


def test_build_system_prompt_includes_matrix_and_rules():
    prompt = build_system_prompt()
    assert "Python 소스" in prompt
    assert "Capability Matrix" in prompt
    assert "Red Zone" in prompt


def test_extract_capability_matrix_missing_section(tmp_path):
    p = tmp_path / "noplan.md"
    p.write_text("# 다른 문서\nno matrix here\n", encoding="utf-8")
    with pytest.raises(ValueError):
        extract_capability_matrix(p)


# ---------- violations ----------

def test_detect_captcha_bypass():
    v = detect_violations("CAPTCHA 우회 서비스를 도입하면 좋겠다")
    assert any("CAPTCHA" in x.label for x in v)


def test_detect_proxy_rotation():
    v = detect_violations("residential proxy rotation으로 우회")
    assert any("proxy" in x.label.lower() for x in v)


def test_detect_user_agent_random():
    v = detect_violations("user-agent 랜덤 로테이션 적용")
    assert any("user-agent" in x.label.lower() for x in v)


def test_clean_text_no_violations():
    v = detect_violations("items_path를 data.recruitData에서 result.jobs로 변경하면 됩니다")
    assert v == []


# ---------- client ----------

def test_config_from_env_returns_none_when_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert LLMConfig.from_env() is None


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    cfg = LLMConfig.from_env()
    assert cfg.api_key == "sk-x"
    assert cfg.model == DEFAULT_MODEL


def test_client_disabled_raises_on_parse():
    client = LLMClient(None)
    assert not client.enabled
    with pytest.raises(LLMNotConfiguredError):
        client.parse(system_prompt="s", user_prompt="u", response_model=PatchCandidate)


class _MiniModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str


def test_parse_retries_on_schema_failure_then_succeeds():
    client = LLMClient(LLMConfig(api_key="sk-x", max_retries=2))
    calls = {"n": 0}

    def fake_call(messages, response_model):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"wrong_key": 1}  # schema 위반
        return {"field": "ok"}

    with patch.object(client, "_call_with_schema", side_effect=fake_call):
        result = client.parse(
            system_prompt="s", user_prompt="u", response_model=_MiniModel,
        )
    assert result.field == "ok"
    assert calls["n"] == 2


def test_parse_rejects_violation_then_succeeds():
    client = LLMClient(LLMConfig(api_key="sk-x", max_retries=2))
    calls = {"n": 0}

    def fake_call(messages, response_model):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"field": "CAPTCHA 우회 서비스를 도입하자"}
        return {"field": "items_path를 변경합니다"}

    with patch.object(client, "_call_with_schema", side_effect=fake_call):
        result = client.parse(
            system_prompt="s", user_prompt="u", response_model=_MiniModel,
        )
    assert "CAPTCHA" not in result.field
    assert calls["n"] == 2


def test_parse_exhausts_retries_with_violation():
    client = LLMClient(LLMConfig(api_key="sk-x", max_retries=1))

    def fake_call(messages, response_model):
        return {"field": "residential proxy rotation으로 가자"}

    with patch.object(client, "_call_with_schema", side_effect=fake_call):
        with pytest.raises(LLMOutputRejectedError):
            client.parse(
                system_prompt="s", user_prompt="u", response_model=_MiniModel,
            )


# ---------- patch schemas ----------

def test_patch_candidate_round_trip():
    p = PatchCandidate.model_validate(
        {
            "file": "configs/sites/catch.yaml",
            "changes": [
                {"op": "replace", "path": "collectors.jobs.mapping.items_path",
                 "old": "recruitData", "new": "data.recruitData"}
            ],
            "reason": "응답 wrapper가 한 단계 추가됨",
            "risk": "low",
        }
    )
    assert p.changes[0].op == "replace"
    assert p.risk == "low"


def test_patch_candidate_rejects_unknown_op():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        PatchCandidate.model_validate(
            {"file": "f", "changes": [{"op": "bad", "path": "p"}], "reason": "r", "risk": "low"}
        )
