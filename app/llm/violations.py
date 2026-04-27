"""Capability Matrix 금지 제안 detector.

LLM이 structured output으로 patch를 만들어도, 그 안의 텍스트(reason, change 메모 등)에
금지 키워드가 들어있으면 거부하고 재시도한다. 1차 방어선 — keyword match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 금지 패턴은 한·영 모두 대응. 정확도보다 회수율 위주.
FORBIDDEN_PATTERNS: list[tuple[str, str]] = [
    (r"\b2captcha\b|\bcapsolver\b|\banti[- ]?captcha\b", "CAPTCHA 외부 풀이 서비스"),
    (r"captcha\s*(우회|풀이|bypass|solve)", "CAPTCHA 우회"),
    (r"residential\s+proxy|프록시\s*(로테이션|rotation)|proxy\s+rotation", "proxy rotation"),
    (r"(login|로그인)\s*(자동화|automate)|아이디.*비밀번호\s*(저장|하드코딩)", "로그인 자동화"),
    (r"user[- ]?agent\s*(랜덤|rotate|rotation)|UA\s*랜덤", "user-agent 랜덤화"),
    (r"(요청|request)\s*간격.*(줄이|감소)|increase\s+request\s+rate", "요청 간격 단축"),
    (r"(playwright|selenium|requests-html|cloudscraper)\s*(도입|추가)\s*제안", "별도 anti-detection 라이브러리 도입"),
]


@dataclass
class Violation:
    pattern: str
    label: str
    matched_text: str


def detect_violations(text: str) -> list[Violation]:
    """주어진 문자열에서 금지 패턴 매치 목록을 반환."""
    if not text:
        return []
    found: list[Violation] = []
    for pattern, label in FORBIDDEN_PATTERNS:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            found.append(Violation(pattern=pattern, label=label, matched_text=m.group(0)))
    return found
