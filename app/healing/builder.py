"""LLM user prompt 빌더. evidence dict + 현재 yaml을 사람이 읽기 쉬운 한국어 prompt로 정리한다.

큰 JSON sample은 truncate해서 토큰 비용·context window를 보호한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_SAMPLE_CHARS = 4000


def _truncate(text: str, limit: int = MAX_SAMPLE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (이하 {len(text) - limit}자 생략)"


def _format_json(value: Any, limit: int = MAX_SAMPLE_CHARS) -> str:
    try:
        return _truncate(json.dumps(value, ensure_ascii=False, indent=2), limit)
    except Exception:
        return _truncate(str(value), limit)


def build_user_prompt(
    *,
    site: str,
    yaml_text: str,
    evidence: dict[str, Any],
) -> str:
    """healing prompt 본문. system prompt는 build_system_prompt()가 별도 담당."""
    report = evidence.get("report") or {}
    diagnosis = report.get("diagnosis") or {}
    issues = report.get("issues") or []

    issue_lines = "\n".join(
        f"  - {i.get('code')}: {i.get('message', '')}" for i in issues[:10]
    ) or "  (이슈 없음)"

    diag_line = (
        f"{diagnosis.get('category', 'unknown')} — {diagnosis.get('summary', '')}"
        if diagnosis else "(진단 없음)"
    )

    api_sample = evidence.get("api_sample")
    api_sample_prev = evidence.get("api_sample_prev")

    sections: list[str] = [
        f"# 사이트: {site}",
        "",
        "## 현재 YAML 설정",
        "```yaml",
        yaml_text.strip(),
        "```",
        "",
        "## 가장 최근 실행 evidence",
        f"- 진단: {diag_line}",
        f"- 수집 결과: inserted={report.get('meta', {}).get('inserted', 0)}, "
        f"updated={report.get('meta', {}).get('updated', 0)}, "
        f"unchanged={report.get('meta', {}).get('unchanged', 0)}",
        f"- 이슈:",
        issue_lines,
    ]

    if api_sample is not None:
        sections += [
            "",
            "## 현재 API 응답 샘플 (truncated)",
            "```json",
            _format_json(api_sample),
            "```",
        ]
    if api_sample_prev is not None:
        sections += [
            "",
            "## 직전 정상 응답 샘플 (truncated)",
            "```json",
            _format_json(api_sample_prev),
            "```",
        ]

    sections += [
        "",
        "## 요청",
        "위 정보를 바탕으로 PatchCandidate 형식으로 응답하라.",
        "",
        "- 응답 구조 변경이 원인이라면 `changes`에 변경할 path/old/new를 채워라.",
        "  path는 dot-separated (예: `collectors.jobs.mapping.items_path`).",
        "- 수정이 불가능하거나 Red Zone(차단 등) 신호이면 `changes`를 빈 배열로 두고",
        "  `reason`에 이유와 권장 다음 행동(예: 사람이 직접 점검 필요)을 한국어로 명시하라.",
        "- `risk`는 변경 영향 범위 기준으로 low/medium/high 중 하나.",
        f"- `file`은 항상 `{evidence.get('yaml_path', f'configs/sites/{site}.yaml')}`.",
    ]

    return "\n".join(sections)


def load_yaml_text(yaml_path: str | Path) -> str:
    return Path(yaml_path).read_text(encoding="utf-8")
