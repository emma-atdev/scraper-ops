"""LLM system prompt 구성. Capability Matrix를 product-plan에서 단일 소스로 주입한다.

product-plan.md의 "## Scrapling Capability Matrix" 섹션 본문을 그대로 system prompt에
포함시켜, LLM이 Green/Yellow/Red Zone 정의와 금지 제안 목록을 매 호출마다 context로 받게 한다.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_PRODUCT_PLAN = Path("docs/product-plan.md")
CAPABILITY_HEADING = "## Scrapling Capability Matrix"


def extract_capability_matrix(plan_path: str | Path = DEFAULT_PRODUCT_PLAN) -> str:
    """product-plan.md에서 Capability Matrix 섹션 본문(헤딩 포함)을 추출한다.

    다음 ## 헤딩 직전까지를 한 섹션으로 본다.
    """
    text = Path(plan_path).read_text(encoding="utf-8")
    start = text.find(CAPABILITY_HEADING)
    if start == -1:
        raise ValueError(f"section '{CAPABILITY_HEADING}' not found in {plan_path}")

    # 다음 ## (h2) 헤딩의 위치를 찾는다. 시작 헤딩 자기 자신은 제외.
    after = text[start + len(CAPABILITY_HEADING):]
    next_h2 = after.find("\n## ")
    end = start + len(CAPABILITY_HEADING) + (next_h2 if next_h2 != -1 else len(after))
    return text[start:end].strip()


SYSTEM_PROMPT_TEMPLATE = """\
당신은 scraper-ops의 config assistant이다. 사용자가 제공한 evidence를 분석하고,
필요 시 `configs/sites/*.yaml`에 적용할 patch candidate를 생성한다.

## 절대 규칙

1. Python 소스 코드를 수정하거나 제안하지 않는다. 변경 범위는 YAML config 한정이다.
2. secret 값(token, password, cookie 등)을 생성·추출·저장하지 않는다.
3. 아래 Capability Matrix의 정의를 따른다. Red Zone 사이트에 대해서는 patch를 만들지
   않고 "수집 불가" 결론을 명시적으로 알린다.
4. Capability Matrix의 "금지 제안" 항목을 어떤 형태로도 제안하지 않는다.

## Capability Matrix (single source of truth)

{capability_matrix}

## 출력 형식

응답은 항상 지정된 JSON schema에 맞는 구조화된 형태여야 한다. 자유 텍스트 답변은
하지 않는다. 추론 과정은 응답 객체의 `reason` 필드에 한국어로 간결히 기술한다.
"""


def build_system_prompt(*, plan_path: str | Path = DEFAULT_PRODUCT_PLAN) -> str:
    matrix = extract_capability_matrix(plan_path)
    return SYSTEM_PROMPT_TEMPLATE.format(capability_matrix=matrix)
