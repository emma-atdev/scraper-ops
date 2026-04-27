"""healing 모듈 단위 테스트. 실 LLM 호출 안 함 (FakeClient 주입)."""

from __future__ import annotations

import pytest

from app.healing import build_user_prompt, generate_patch_candidate
from app.llm import LLMClient, LLMNotConfiguredError, PatchCandidate, PatchOperation


# ---------- builder ----------

SAMPLE_EVIDENCE = {
    "report": {
        "diagnosis": {"category": "schema_change", "summary": "items_path_not_list"},
        "issues": [
            {"code": "items_path_not_list", "message": "items_path resolved to non-list: dict"},
        ],
        "meta": {"inserted": 0, "updated": 0, "unchanged": 0},
    },
    "api_sample": {"data": {"recruitData": [{"RecruitID": 1}]}},
    "api_sample_prev": {"recruitData": [{"RecruitID": 1}]},
}


def test_build_user_prompt_includes_yaml_and_diagnosis():
    yaml_text = "site: catch\ncollectors:\n  jobs:\n    type: api_jobs\n"
    prompt = build_user_prompt(
        site="catch",
        yaml_text=yaml_text,
        evidence=SAMPLE_EVIDENCE,
    )
    assert "사이트: catch" in prompt
    assert "type: api_jobs" in prompt
    assert "schema_change" in prompt
    assert "items_path_not_list" in prompt
    assert "현재 API 응답 샘플" in prompt
    assert "직전 정상 응답 샘플" in prompt


def test_build_user_prompt_handles_missing_samples():
    prompt = build_user_prompt(
        site="x",
        yaml_text="site: x",
        evidence={"report": {"issues": []}},
    )
    assert "이슈 없음" in prompt
    assert "현재 API 응답 샘플" not in prompt
    assert "직전 정상 응답 샘플" not in prompt


def test_build_user_prompt_truncates_huge_sample():
    huge = {"data": "x" * 100000}
    prompt = build_user_prompt(
        site="x", yaml_text="site: x",
        evidence={"report": {}, "api_sample": huge},
    )
    assert "이하" in prompt and "자 생략" in prompt


# ---------- generate_patch_candidate ----------

class _FakeClient:
    """LLMClient 호환 mock. parse만 구현."""

    def __init__(self, response):
        self._response = response
        self.calls = []
        self.enabled = True

    def parse(self, *, system_prompt, user_prompt, response_model, max_retries=None):
        self.calls.append({"system": system_prompt, "user": user_prompt, "model": response_model})
        return self._response


def test_generate_patch_candidate_passes_through(tmp_path):
    yaml_path = tmp_path / "catch.yaml"
    yaml_path.write_text("site: catch\ncollectors: {}\n", encoding="utf-8")

    fake_response = PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[PatchOperation(
            op="replace",
            path="collectors.jobs.mapping.items_path",
            old="recruitData",
            new="data.recruitData",
        )],
        reason="응답 wrapper가 한 단계 깊어졌음",
        risk="low",
    )
    client = _FakeClient(fake_response)

    result = generate_patch_candidate(
        site="catch", yaml_path=yaml_path, evidence=SAMPLE_EVIDENCE, client=client,
    )
    assert result is fake_response
    assert len(client.calls) == 1
    assert "site: catch" in client.calls[0]["user"]
    assert "Capability Matrix" in client.calls[0]["system"]
    assert client.calls[0]["model"] is PatchCandidate


def test_generate_patch_candidate_passes_infeasible(tmp_path):
    yaml_path = tmp_path / "catch.yaml"
    yaml_path.write_text("site: catch\n", encoding="utf-8")

    infeasible = PatchCandidate(
        file="configs/sites/catch.yaml",
        changes=[],  # 빈 배열 = LLM이 patch 못 만든다 판정
        reason="응답이 차단 페이지로 변경. Red Zone 가능성, 사람 점검 필요.",
        risk="high",
    )
    client = _FakeClient(infeasible)

    result = generate_patch_candidate(
        site="catch", yaml_path=yaml_path, evidence=SAMPLE_EVIDENCE, client=client,
    )
    assert result.changes == []
    assert result.risk == "high"
    assert "Red Zone" in result.reason or "사람" in result.reason


def test_generate_patch_candidate_raises_when_disabled(tmp_path):
    yaml_path = tmp_path / "catch.yaml"
    yaml_path.write_text("site: catch\n", encoding="utf-8")

    disabled_client = LLMClient(None)
    with pytest.raises(LLMNotConfiguredError):
        generate_patch_candidate(
            site="catch", yaml_path=yaml_path, evidence=SAMPLE_EVIDENCE,
            client=disabled_client,
        )
