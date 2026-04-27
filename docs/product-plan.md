# 제품 기획

## 제품 정의

`scraper-ops`는 반복 목록과 상세 페이지를 수집하는 Slack 기반 HITL Self-Healing Scraper다. 첫 유스케이스는 채용공고 수집이지만, 목표는 채용공고 전용 크롤러가 아니라 수집 실패를 감지하고, 실패 증거를 모으고, LLM이 설정 수정 후보를 만들고, 사람이 승인한 변경만 적용하는 운영형 스크래퍼 플랫폼을 만드는 것이다.

## 핵심 원칙

- 반복 목록과 상세 데이터 수집은 자동화한다.
- 장애 판단과 설정 변경은 사람이 승인한다.
- LLM은 운영 코드를 직접 수정하지 않는다.
- 자동 패치 범위는 `configs/sites/*.yaml`로 제한한다.
- Python 소스 변경은 개발자가 직접 리뷰하고 배포한다.
- proxy rotation, CAPTCHA 우회, stealth fallback의 자동 활성화는 기본 범위에 포함하지 않는다. Yellow Zone fetcher는 사람 승인 기반으로만 활성화한다.
- 기본 HTTP 수집 경로는 stdlib `urllib.request` 기반 `HttpFetcher`다. 가벼운 의존성과 일반적인 TLS 시그니처로 다수 사이트에서 통과율이 높다. catch.co.kr 등 일부 사이트는 curl_cffi TLS를 도리어 차단하는 사례가 있어 stdlib을 기본으로 둔다.
- Scrapling(https://github.com/D4Vinci/Scrapling, BSD-3-Clause)은 Yellow Zone에서 활용한다. `DynamicFetcher`(Playwright)와 `StealthyFetcher`(patchright)는 사람 승인과 사이트별 정책을 통과할 때만 활성화하며, 상시 수집 용도로는 사용하지 않는다. Scrapling `Fetcher`(HTTP, curl_cffi)도 stdlib으로 통과되지 않는 사이트의 fallback으로만 검토한다.
- 시스템의 수집 능력·한계는 "Scrapling Capability Matrix" 섹션에서 단일하게 정의하며, LLM이 patch candidate를 생성할 때 이 정의를 context로 주입받는다.
- 신규 사이트는 agent-guided HITL workflow로 온보딩한다.

## 첫 대상: catch.co.kr

첫 운영 검증 대상은 catch.co.kr이다. catch.co.kr은 API-only 대상이 아니라 hybrid target으로 다룬다.

- static HTML collector는 페이지 상태, 상단 영역, 차단 신호를 확인하는 진단용 collector다.
- API jobs collector는 채용공고 목록 수집과 증분 적재의 핵심 경로다.
- detail HTML collector는 상세 정보 보강용 선택 경로다. 신규 또는 변경 공고에만 제한적으로 실행한다.
- Playwright checker (Yellow-1, `DynamicFetcher`)는 상시 수집용이 아니라 API 장애 또는 렌더링 이슈 진단용 fallback이다.

## MVP 범위

MVP의 운영 검증 대상은 catch.co.kr 하나로 제한한다. 단, 설정 schema, collector 인터페이스, evidence 모델, HITL 승인 흐름은 이후 여러 사이트와 여러 데이터 도메인으로 확장 가능하게 설계한다.

포함:

- catch API jobs collector
- static HTML diagnostics collector
- SQLite 저장소
- 최초 전체 적재
- 이후 신규/변경 공고 증분 저장
- 기본 검증: 결과 0건, 필수 필드 누락, 수집량 급감, 응답 구조 변경
- 실패 보고서 생성
- Slack 실패 알림
- VM용 `systemd timer`
- 실행 환경 정책: `local`, `vm`, `ci`, `container`
- VM allowlist: `SCRAPER_ALLOWED_SITES`
- 중복 실행 방지 file lock

제외:

- 다중 사이트 운영. 단, 신규 사이트 온보딩과 다중 사이트 실행을 막지 않는 구조로 설계한다.
- 채용공고 외 도메인의 운영 수집. 단, 모델과 설정은 목록/상세형 데이터로 확장 가능하게 둔다.
- 완전 자율 코드 수정
- CAPTCHA 우회 및 stealth fallback의 자동 활성화
- 로그인 쿠키 기반 수집
- Yellow Zone fetcher(`DynamicFetcher`/`StealthyFetcher`) 상시 수집
- PostgreSQL 운영
- 개인 비서 통합

## Scrapling Capability Matrix

수집 가능성을 Green / Yellow / Red 3개 Zone으로 정의한다. 이 matrix는 LLM이 온보딩 config 초안과 self-healing patch candidate를 생성할 때 context로 주입되며, LLM은 이 범위 밖의 해결책을 제안해서는 안 된다.

### Green Zone — MVP 기본 수집 경로 (stdlib HTTP)

**사용 도구**: `app.collectors.fetchers.HttpFetcher` (stdlib `urllib.request` 기반). Scrapling `Fetcher`(curl_cffi)는 stdlib으로 통과되지 않는 사이트에 한해 fallback으로만 검토한다.

**커버**:
- 공개 JSON API (REST, GraphQL)
- 정적 HTML (서버 사이드 렌더링)
- HTTP 헤더·쿠키 수동 지정
- 간단한 User-Agent 기반 봇 탐지 회피
- 경험적으로 catch.co.kr 같은 사이트도 통과 (단순 stdlib TLS가 오히려 통과율 높은 사례)

**감지 신호 (이 Zone이라고 판정하는 근거)**:
- `Cmd+U`로 연 HTML 원본에서 필요한 데이터 확인 가능
- 또는 Network XHR 탭에서 JSON API 확인 가능
- HTTP 200 + 정상 응답
- `cf-ray` 등 Cloudflare 헤더 없음

**적합 예시**: catch.co.kr, 대부분의 기업 채용 공고 사이트, 공공 OpenAPI, RSS

### Yellow Zone — 승인 기반 활성화 (`DynamicFetcher`, `StealthyFetcher`)

#### Yellow-1: JS 동적 사이트 (`DynamicFetcher`)

**사용 도구**: `scrapling.fetchers.DynamicFetcher` (Playwright + Chromium)

**커버**:
- JavaScript 실행 (SPA, React/Vue/Angular)
- 동적 로딩 (scroll, click으로 로드)
- DOM 최종 상태 파싱

**감지 신호**:
- `Cmd+U`의 HTML 원본이 빈 컨테이너
- Network XHR에도 사용 가능한 JSON API 없음
- `Fetcher`로 가져오면 빈 HTML

**활성화 조건**: 사람 승인 + `DynamicCollector` 추가 승인

#### Yellow-2: 기본 anti-bot 보호 사이트 (`StealthyFetcher`)

**사용 도구**: `scrapling.fetchers.StealthyFetcher` (patchright, 수정된 Chromium)

**커버**:
- 브라우저 자동화 흔적 제거
- Cloudflare 챌린지 자동 풀이 (`solve_cloudflare=True`)
- 기본 수준의 브라우저 핑거프린팅 우회

**감지 신호**:
- 응답 헤더에 `cf-ray` 존재
- "Just a moment..." 또는 503 응답
- 일반 브라우저에선 접근되지만 Python 클라이언트는 차단

**활성화 조건**: 사람 승인 + `preferred_environment`와 allowlist 정책 통과

### Red Zone — 시스템 범위 밖 (수집 포기)

**기술적 한계 (Scrapling으로도 불가)**:
- CAPTCHA (reCAPTCHA, hCAPTCHA, Cloudflare Turnstile) — 사람이 풀어야 하는 종류
- 엔터프라이즈 anti-bot (Akamai Bot Manager, DataDome, Kasada, Imperva)
- OAuth/SAML/2FA 로그인 체인
- 검색엔진 SERP (Google, Bing, Naver 검색 결과)
- 모바일 앱 전용 API
- WebSocket / Server-Sent Events 실시간 스트림

**정책적 제외 (기술적으론 가능하나 기획서가 제외)**:
- Proxy rotation (자동 로테이션)
- 로그인 세션·쿠키 기반 수집 (MVP 제외)
- Stealth 상시 수집 (진단·예외 fallback만 허용)
- CAPTCHA 외부 풀이 서비스 연동

**감지 신호**:
- CAPTCHA iframe 또는 챌린지 HTML 발견
- Akamai `_abck` 쿠키, DataDome `datadome` 쿠키 등 탐지
- 로그인 페이지로 리다이렉트
- IP 차단(connection refused, 빈 응답 반복)

**Red Zone 판정 시 동작**:
- agent가 "이 사이트는 scraper-ops 범위에서 수집 불가"로 보고
- 기술적/정책적 이유를 명시
- 수집 시도를 하지 않음
- YAML config를 생성하지 않음

### LLM이 따라야 할 규칙

LLM이 YAML config 초안 또는 self-healing patch를 생성할 때:

1. **Green Zone**: `Fetcher` 기반 config 자율 생성 가능
2. **Yellow Zone**: `DynamicFetcher`/`StealthyFetcher` 사용 제안은 가능하나 반드시 "사람 승인 필요" 플래그를 patch에 포함
3. **Red Zone**: YAML config 생성하지 않음. "수집 불가" evidence report 생성
4. **금지 제안** (LLM이 절대 제안하지 말 것):
   - CAPTCHA 외부 풀이 서비스(2Captcha 등) 연동
   - residential proxy rotation
   - 로그인 자동화 (ID/PW 하드코딩)
   - user-agent 랜덤 로테이션으로 anti-bot 회피 시도
   - 요청 간격을 줄여 빠르게 돌리는 제안
   - Scrapling 외 별도 anti-detection 라이브러리 도입 제안

### Matrix 갱신 정책

이 matrix는 Scrapling 버전 업데이트 또는 운영 중 발견된 새 anti-bot 패턴에 따라 사람이 갱신한다. LLM은 matrix를 수정하지 않으며, 현재 matrix가 실제 상황과 맞지 않다고 판단되면 evidence와 함께 사람에게 보고한다.

## LLM의 역할

LLM은 scraper config assistant로 둔다.

사용 모델: OpenAI `gpt-4.1`. evidence 분석, YAML patch 생성, Slack 스레드 대화 응답 모두 이 모델을 사용한다. instruction following과 구조화된 출력 품질이 이 용도에 적합하다. 운영 중 모델 교체가 필요하면 config에서 model ID만 바꿀 수 있도록 하드코딩하지 않는다.

LLM 호출 정책:

- 자동 self-healing 단계에서는 장애 1건당 기본 2회를 사용한다.
  - evidence 분석 1회
  - YAML patch draft 생성 1회
- 운영자가 다른 후보를 요청하면 patch draft를 추가 생성할 수 있다.
- Slack approval thread의 자유 대화는 자동 호출 한도에 포함하지 않는다. 운영자가 근거 확인, 대안 요청, dry-run 결과 설명을 위해 여러 번 질문할 수 있어야 하기 때문이다.
- MVP에서는 Slack 대화 횟수를 엄격히 제한하지 않는다. 대신 approval thread가 만료되면 대화도 종료한다.
- 운영 비용이나 오남용이 문제가 되면 thread 단위 budget을 설정할 수 있게 한다. 예: approval thread당 최대 20회, 생성 후 48시간 또는 승인 만료 시 종료.
- 동일 질문 반복, 대량 payload 요청, secret 추출 요청, CAPTCHA/proxy 우회 요청은 LLM 호출 없이 거절한다.

### LLM 출력 검증과 retry

LLM이 생성한 YAML patch 또는 config는 다음 검증을 거친다.

- **Structured output 강제**: OpenAI structured output(JSON schema)을 사용해 patch 형식을 강제한다. 자유 텍스트로 patch를 생성하지 않는다.
- **스키마 검증**: 생성된 YAML이 `configs/sites/*.yaml`의 pydantic 스키마를 통과해야 한다. 통과하지 못하면 LLM 출력으로 간주하지 않는다.
- **재시도 한도**: 같은 단계(evidence 분석, patch draft 등)에서 스키마 검증 실패 시 최대 2회 재시도한다.
- **재시도 전부 실패**: "patch candidate 생성 실패" 상태로 Slack 보고. 운영자에게 원시 evidence와 LLM 응답 원문을 첨부한다.
- **Capability Matrix 위반 검출**: 생성된 patch가 Capability Matrix의 "금지 제안" 목록에 포함되면 자동 거부하고 재시도한다. 2회 거부되면 위 "생성 실패" 흐름으로 들어간다.

LLM이 하는 일:

- HTML/API evidence 분석
- API response schema diff 해석
- selector 후보 생성
- `items_path`, `field mapping`, `link_template` 후보 생성
- site YAML 초안 생성
- 실패 원인 후보와 위험도 설명
- YAML patch candidate 생성
- dry-run 결과를 사람이 이해할 수 있게 요약

LLM이 하지 않는 일:

- Python 소스 자동 수정
- secret 생성, 추출, 저장
- VM allowlist 자동 변경
- CAPTCHA 우회 제안
- proxy rotation 자동 설정
- Yellow Zone fetcher(`DynamicFetcher`/`StealthyFetcher`)와 proxy 기반 fallback의 자동 활성화
- 사람 승인 없는 patch 적용

## Execution Lifecycle Policy

scraper-ops의 모든 사이트 운영은 3개 Phase로 나뉘며, 각 Phase에서 사람과 agent의 자율성 범위가 다르다. 이 정책은 신규 사이트 온보딩, 정기 수집 운영, 장애 대응 모두에 일관되게 적용된다.

### Phase 1. Setup — Human-led, Agent-assisted

신규 사이트 또는 새 collector를 처음 만드는 단계.

- **사람이 결정**: URL·힌트 입력(Step A), 수집 가능성 Zone 판정 결과 검토(Step A2), fetcher 선택 및 접근 방식 확정(Step B), 최종 operate 투입 승인(Step E)
- **Agent 자율**: Capability Matrix 기반 Zone 판정(Step A2), config 자동 생성과 자율 dry-run(Step C, 최대 5회 재시도), 판정 기준 통과 확인(Step D)
- **HITL 포인트**: Step E (사람이 추출 샘플 3~5건 확인 후 승인)
- 자세한 흐름은 "신규 사이트 온보딩" 섹션 참조
- "새 collector 추가"도 Phase 1으로 취급한다 (기존 잘 동작하는 collector는 안 건드리고 새로 추가하는 케이스)

### Phase 2. Operation — Agent-autonomous

config가 승인되어 정상 수집 중인 단계.

- **Agent 자율**: scheduled run 실행, 증분 수집, evidence 수집·보관 정책 적용, rate limit 대응 backoff
- **사람 개입 없음** (정상 운영 중)
- 검증 실패 또는 schema diff 등 이상 신호가 감지되면 자동으로 Phase 3로 전환

### Phase 3. Change / Failure — HITL

기존 잘 동작하던 config의 수정이 필요한 단계.

- **Agent 자율**: 실패·변화 감지, evidence 수집, 실패 범주화, patch candidate 생성, dry-run
- **사람이 결정**: patch 승인 또는 거절 (Slack approval thread)
- **원칙**: 기존에 작동하던 config의 변경은 반드시 HITL을 거친다. Agent는 patch를 자동 적용하지 않는다.
- 자세한 흐름은 "Self-Healing 흐름" 섹션 참조

### HITL 적용 범위

YAML config 변경 중 HITL이 필요한 필드와 자율 가능한 필드를 구분한다.

**HITL 필수 (수집·해석 필드)**:
- `fetcher` (http/dynamic/stealthy 변경)
- `url`, `method`, `headers`, `pagination`
- `items_path`, `mapping`
- `validation` 규칙 강화 또는 완화

**자율 가능 (운영 메타데이터)**:
- `retention_days`, `alert_channel`
- `snapshot_compression`, `preferred_environment` 등 운영 설정

MVP에서는 보수적으로 모든 config 변경을 HITL로 시작하고, 운영 경험이 쌓이면 메타데이터 자율 변경을 단계적으로 허용한다.

### Phase 간 전이 트리거

- Phase 1 → Phase 2: Step E 승인 후 첫 scheduled run 시작
- Phase 2 → Phase 3: validation 실패, schema diff 임계 초과, 수집량 급감 등 이상 신호
- Phase 3 → Phase 2: patch 승인 + 적용 + rerun_success
- Phase 3 → 종료: rerun_failed 또는 운영자가 "수동 처리 선언"으로 닫음

## 신규 사이트 온보딩

새 사이트·새 경로·새 데이터 도메인의 scraper 생성은 agent-guided HITL workflow로 처리한다. Phase 1의 Setup은 사람 주도 + agent 보조이며, Phase 2의 Operation은 agent 자율, Phase 3의 Change/Failure는 HITL이다.

### Phase 1 Setup 흐름

**Step A. URL 및 힌트 입력 (사람)**
- 대상 URL 제공
- 예상 수집 건수 힌트 (예: "공고 50~100건/일")
- 도메인 분류 (채용·상품·공지·문서 등)

**Step A2. 수집 가능성 판정 (Agent 자동)**
- 기본 HTTP 요청으로 응답 코드와 헤더 확인
- Capability Matrix의 Green/Yellow/Red Zone 신호 감지
  - Green: 정상 HTML 또는 JSON API
  - Yellow-1: JS 동적 필요 신호
  - Yellow-2: Cloudflare `cf-ray` 등 anti-bot 신호
  - Red: CAPTCHA, Akamai, 로그인 강제, 검색엔진 등
- Zone 판정 결과를 사람에게 보고
- Red Zone인 경우: "이 사이트는 현재 범위에서 수집 불가" 메시지와 구체 이유를 Slack으로 보고. 온보딩을 중단한다.

**Step B. Fetcher 선택 및 접근 방식 확정 (사람, Agent 가이드)**
- Green Zone: `Fetcher` 사용 확정, API URL·headers·sample을 사람이 제공
- Yellow-1: `DynamicFetcher` 사용 확정, 사용 승인 요청
- Yellow-2: `StealthyFetcher` 사용 확정, 사용 승인 요청
- Agent는 day07 노트의 판단 플로우를 대화형으로 안내 (Cmd+U로 소스 확인 요청, Network 탭 확인 요청 등)

**Step C. Config 자동 생성 + 자율 dry-run (Agent 자율, 최대 5회 재시도)**
- `items_path`, `field mapping`, `pagination` 후보를 LLM이 생성
- 저장된 sample payload로 dry-run 실행
- 실패 시 다른 후보로 자동 재시도
- 5회 실패 시 "사람 개입 필요" 상태로 Slack 보고 + 증거 첨부

**Step D. 판정 기준 통과 확인 (자동)**
- items 추출 건수가 Step A에서 받은 힌트 범위 내
- 필수 필드 값이 그럴듯함 (예: title이 자연어, id가 고유값)
- 샘플 3~5건 생성

**Step E. Operate 투입 승인 (HITL)**
- Agent가 Slack에 다음을 전송:
  - YAML config 전체 diff
  - 추출 샘플 3~5건
  - dry-run 결과 (items 건수, 필수 필드 누락 0건 확인)
  - 사용 fetcher 및 Yellow Zone인 경우 승인 플래그
- 사람이 샘플을 눈으로 확인 후 `Approve` / `Reject`
- 승인 시: `configs/sites/*.yaml` 추가, 첫 scheduled run 실행
- VM allowlist 반영은 SSH 운영 절차로 별도 처리

### 사람이 제공하면 좋은 정보 (Step A, B에서)

- 목록 페이지 URL
- Network 탭에서 확인한 API URL
- method, query params, 필요한 headers
- pagination 방식
- response sample
- items path (찾을 수 있으면)
- 필드 후보: ID, 제목, 이름, 날짜, 가격, 상태, 링크 등 도메인별 핵심 필드
- 상세 페이지 필요 여부
- 로그인 또는 쿠키 필요 여부 (필요하면 MVP 범위 밖)
- VM 실행 가능성에 대한 판단

## Self-Healing 흐름

장애 발생 시 시스템은 바로 고치지 않고 증거를 모은다.

```text
scheduled run 실패
→ validation issue 생성
→ static diagnostics, API sample, HTML snapshot, schema diff 수집
→ diagnosis가 실패 범주화
→ LLM이 YAML patch candidate 생성
→ Slack에 원인, 증거, diff, 위험도 표시
→ 사람이 승인 또는 거절
→ 승인 시 YAML patch 적용
→ 실패 job rerun
→ 최종 성공/실패 Slack 요약
```

patch candidate는 다음 형식을 지향한다.

```yaml
file: configs/sites/catch.yaml
changes:
  - op: replace
    path: collectors.jobs.mapping.items_path
    old: recruitData
    new: data.recruitData
reason: API response wrapper changed.
risk: low
```

### dry-run 정의

dry-run은 실제 API를 호출하지 않고, **저장된 sample payload**에 patch된 YAML을 적용해 추출 결과를 확인하는 로컬 시뮬레이션이다. DB에 아무것도 쓰지 않는다.

dry-run 순서:

1. patch YAML 스키마 검증 (필수 필드 존재, 타입 올바른지)
2. 변경 전 YAML로 sample payload 파싱 → 추출 결과 A
3. 변경 후 YAML로 sample payload 파싱 → 추출 결과 B
4. A와 B를 비교해 개선/악화/변화 없음 판정
5. 결과 요약을 Slack 메시지에 포함

Slack에 포함되는 dry-run 요약 예시:

```
dry-run 결과
- 변경 전: items 추출 0건 (items_path 미매칭)
- 변경 후: items 추출 47건, 필수 필드 누락 0건
- 샘플: [{"id": "12345", "title": "백엔드 엔지니어", ...}, ...]
판정: 개선됨
```

sample payload가 없으면 dry-run을 건너뛰고 "sample 없음, dry-run 생략"으로 표시한다. 이 경우 운영자는 Approve 전에 직접 evidence를 확인해야 한다.

live API를 호출하는 test-run은 사람이 승인하고 별도 명령으로 실행한다. 기존 사이트의 self-healing에서는 dry-run 통과를 test-run 실행의 전제 조건으로 둔다. 신규 사이트 온보딩처럼 저장된 sample이 아직 없는 경우에는 사람이 승인한 live test-run을 sample 생성 목적으로 먼저 실행할 수 있다.

### Self-Healing 레이턴시 기대치

자동화 구간과 사람 구간을 분리해서 이해해야 한다.

자동화 구간 (시스템이 처리):

- 실패 감지 → evidence 수집: 1분 이내
- LLM evidence 분석 + patch draft 생성: 30초 이내
- dry-run 실행: 10초 이내
- Slack 알림 발송: 즉시
- 합계: 실패 발생 후 약 2분 이내에 Slack 알림 도착

사람 구간 (운영자 의존):

- 운영자가 Slack 확인 후 승인/거절: 수분 ~ 수시간
- 이 구간은 시스템이 단축할 수 없다. SLA가 필요하다면 Slack 알림 외에 추가 채널(문자, PagerDuty 등)을 검토하지만 MVP 범위 밖이다.

후속 자동화 구간:

- 승인 수신 → YAML patch 적용 → rerun 트리거: 1분 이내
- rerun 실행 + 결과 Slack 요약: run 소요 시간 + 1분

systemd timer 기본 주기는 1시간으로 시작한다. 장애 발생 후 다음 정상 run까지 최악의 경우 수 시간이 걸리며, 이는 설계상 허용 범위다. 실시간 복구가 필요한 경우는 MVP 범위 밖이다.

## Slack 승인 서버

Slack은 단순 approve/reject 채널이 아니라 운영자와 시스템이 대화할 수 있는 인터페이스다. 버튼으로 빠른 결정을 내리고, 스레드 대화로 맥락을 확인하거나 추가 지시를 줄 수 있어야 한다.

### 메시지 구조

실패 알림 메시지는 다음을 포함한다.

- 실패 요약: 사이트, 실패 유형, 감지 시각
- LLM이 분석한 원인 후보와 위험도
- YAML patch diff (변경 전/후 비교)
- dry-run 결과 요약 (추출 성공 건수, 샘플)
- **Approve** / **Reject** 버튼
- 스레드 링크: "자세한 evidence는 스레드를 확인하세요"

스레드에는 수집된 evidence 원문(API sample, schema diff, traceback)을 게시한다. 메인 메시지를 읽기 쉽게 유지하면서 필요한 사람은 스레드에서 상세 내용을 확인할 수 있다.

### 대화 기능

운영자는 approval thread에서 봇에게 자유 텍스트로 질문하거나 지시할 수 있다. 봇은 LLM을 호출해 evidence 컨텍스트를 기반으로 응답한다.

지원하는 대화 유형:

- 질문: "왜 이 path가 바뀐 거야?", "dry-run에서 추출된 샘플 보여줘", "위험도가 low인 근거가 뭐야?"
- 수정 요청: "items_path 말고 data.jobs로 시도해봐", "다른 patch 후보 있어?"
- 수동 처리 선언: "내가 직접 고칠게, 이 건은 닫아줘"
- 추가 진단 요청: "Playwright로 현재 페이지 상태 확인해줘"

봇이 응답할 때는 항상 스레드에 게시하며, 메인 채널을 오염시키지 않는다. 버튼 상태는 대화 중에도 유지되며, 대화 후 최종 결정은 여전히 버튼 또는 명시적 텍스트 명령(`approve`, `reject`)으로 처리한다.

### 서버 구조

- approval server는 MVP에서 scraper runner와 같은 VM에서 실행하며, 포트는 분리한다
- Slack Events API로 메시지 이벤트 수신 (버튼 클릭은 `/slack/actions`, 대화는 `/slack/events`)
- ngrok 또는 VM 공인 IP로 Slack webhook endpoint를 노출한다
- approval request는 Slack 메시지 발송 전에 DB에 `pending` 상태로 저장한다
- 버튼 클릭이나 명시적 텍스트 명령은 기존 approval request의 상태를 변경한다
- 승인 수신 후 patch 적용과 rerun은 같은 approval request의 상태 전이를 기록한다

approval request 상태:

```text
pending
→ approved | rejected | expired
→ applied
→ rerun_success | rerun_failed
```

상태 전이 원칙:

- `pending` 상태에서만 approve/reject를 받을 수 있다.
- 만료된 approval request에는 patch를 적용하지 않는다.
- 같은 approval request에 대해 patch 적용과 rerun은 한 번만 실행한다.
- approval server와 runner가 같은 SQLite를 사용할 수 있으므로 상태 업데이트는 transaction으로 처리한다.

SQLite 동시성 정책:

- **WAL(Write-Ahead Logging) 모드 활성화**: 읽기와 쓰기가 동시에 일어나도 lock 경쟁이 줄어든다.
- **busy_timeout 5초**: 쓰기 충돌 시 최대 5초까지 대기 후 재시도.
- **Write 단일 진입점**: approval_request 상태 전이는 approval server, records 적재는 runner로 책임을 분리한다. 같은 row를 두 프로세스가 동시 갱신하지 않는다.
- **트랜잭션 단위**: 상태 전이 1건 + 관련 audit log 1건을 같은 transaction으로 묶는다.
- **재시도 정책**: `SQLITE_BUSY` 발생 시 최대 3회 exponential backoff 재시도 후 실패 처리.

### 장애 대응

- approval server 다운 시 버튼 클릭은 Slack이 최대 3회 retry
- retry 후에도 실패한 클릭 이벤트는 시스템이 수신하지 못하므로 DB에 기록할 수 없다
- 서버 재시작 시 DB에 남아 있는 `pending` approval request를 확인하고 "미처리 승인 대기 건 있음" 알림 발송
- 서버가 완전히 접근 불가인 경우 운영자는 CLI(`scraper approve <run_id>`)로 우회 처리

### 승인 만료 정책

- 알림 발송 후 48시간 내 응답 없으면 자동 만료
- 만료 시 스레드에 "승인 만료됨, 다음 실패 시 재진단" 메시지 게시
- 만료된 patch는 재적용하지 않으며, 다음 scheduled run 재실패 시 처음부터 다시 진단

## Evidence 전략

LLM과 사람이 판단하려면 실패 증거가 필요하다.

수집할 evidence:

- run metadata
- validation issues
- exception traceback
- 현재 API sample payload
- 이전 정상 API sample payload
- API schema diff
- static HTML diagnostics
- HTML snapshot path
- detail HTML snapshot path
- HTTP status, headers
- selector별 추출 결과

Evidence는 `data/reports`와 `data/snapshots`에 저장한다.

디렉토리 구조:

```
data/
  reports/
    {site}/{run_id}/report.json          # 실패 요약, validation issues, LLM 분석 결과
  snapshots/
    {site}/{run_id}/api_sample.json      # 현재 API 응답 샘플
    {site}/{run_id}/api_sample_prev.json # 직전 정상 API 응답 샘플
    {site}/{run_id}/schema_diff.json     # 두 샘플 간 schema diff
    {site}/{run_id}/static.html          # static HTML snapshot
    {site}/{run_id}/detail_{id}.html     # 상세 페이지 snapshot
```

보관 정책:

- reports: 최근 90일 또는 최대 1,000건, 초과 시 오래된 것부터 삭제
- snapshots: 최근 30일 또는 최대 500MB, 초과 시 오래된 것부터 삭제
- 정상 run의 snapshot: 7일 후 삭제 (진단 불필요)
- 실패 run의 snapshot: 30일 유지 (LLM 재분석, 운영자 검토 가능)
- 정리는 매 run 시작 시점에 실행하며, 정리 실패는 경고 로그만 남기고 수집을 계속한다

`api_sample_prev.json`은 마지막 정상 run의 sample을 별도로 보관한다. 현재 sample과 schema diff를 만드는 데 사용하며, 정상 run마다 갱신한다.

## 봇 탐지 및 접근 전략

수집 대상 사이트에 대한 접근 전략은 "Scrapling Capability Matrix"의 Green/Yellow/Red Zone에 따라 결정한다.

### Green Zone 사이트 (MVP 기본)

- `Fetcher`(HTTP)로 수집
- 사이트별 YAML에 `fetcher: http` 지정
- 요청 간 `delay_seconds` 준수 (기본 2초, robots.txt `Crawl-delay` 있으면 그 값)
- `User-Agent`는 정체를 드러내는 형태 유지 (예: `scraper-ops/0.1`)
- 403·429·503 응답은 evidence로 저장하고 해당 run 중단

### Yellow Zone 사이트 (승인 기반)

#### JS 동적 사이트 (`DynamicFetcher`)

사용 기준:
- Green Zone 도구로 필요한 데이터를 확보할 수 없음 (API 없음, 정적 HTML 빔)
- 운영자가 `DynamicCollector` 사용을 Slack에서 승인함
- 해당 사이트의 `runtime.preferred_environment`와 allowlist 정책 통과

활성화 방법:
- VM에서 `scrapling install` 실행으로 Chromium 바이너리 다운로드
- YAML에 `fetcher: dynamic` 지정
- `requires_approval: true` 플래그 유지

#### 기본 anti-bot 사이트 (`StealthyFetcher`)

사용 기준:
- Green Zone과 `DynamicFetcher` 모두 차단 또는 비정상 응답
- 응답 헤더에 `cf-ray` 등 Cloudflare 시그널 존재
- Playwright checker로도 렌더링 상태 판단이 애매함
- 운영자가 Slack에서 `StealthyFetcher` 사용을 승인함
- IP 차단 가능성이 높다고 판단되면 VM에서 즉시 반복 재시도하지 않고 실행을 멈춘다

활성화 방법:
- YAML에 `fetcher: stealthy`, `solve_cloudflare: true` 지정
- `requires_approval: true` 유지
- 운영 수집 경로 승격 전에 diagnostics 모드로 먼저 검증

제한:
- 상시 수집 경로가 아님
- CAPTCHA 우회, proxy rotation, stealth 자동 활성화는 금지
- VM allowlist 변경은 HITL patch 흐름이 아니라 SSH 운영 절차
- IP 차단 대응으로 실행 위치를 바꾸거나 local worker로 전환하는 경우 runtime 정책 변경을 사람이 승인

### Red Zone 사이트 (수집 포기)

Red Zone 신호가 감지되면 수집을 시도하지 않고 "수집 불가" evidence report를 생성한다. Capability Matrix의 Red Zone 정의를 참조.

### 설정 방향

```yaml
collectors:
  jobs:
    fetcher: http              # green | dynamic | stealthy
    # fetcher: dynamic         # Yellow-1
    # fetcher: stealthy        # Yellow-2
    #   solve_cloudflare: true
    #   requires_approval: true
```

## 상세 페이지 수집 전략

상세 내용은 API jobs collector와 분리한다.

원칙:

- 목록 수집은 API jobs collector가 담당한다.
- 상세 수집은 optional enrichment다.
- 상세 HTML은 실제 샘플을 저장하고 selector를 확정한 뒤 활성화한다.
- 신규 또는 변경 공고만 상세 수집한다.
- 변경 감지 기준: API가 `updated_at` 또는 `modified_at` 필드를 제공하면 이를 우선 사용한다. 없으면 핵심 필드(제목, 마감일, 상태, 회사명)를 이어붙인 문자열의 SHA-256 hash를 DB에 저장하고 이전 hash와 비교한다. hash가 바뀐 경우에만 상세 수집 대상으로 표시한다. hash에 포함할 필드는 사이트별 YAML에서 `change_detection_fields`로 지정한다.
- 요청 간 delay와 run당 최대 상세 수집 개수를 둔다.
- 상세 수집 실패는 jobs 수집 실패와 분리해서 진단한다.

DB는 상세 필드를 고정 컬럼으로 늘리기보다 `details_json` 같은 JSON 필드를 우선 검토한다.

## 도메인 확장 전략

첫 데이터 모델은 채용공고에 맞춰 `job_postings`를 사용한다. 하지만 장기적으로는 채용공고 외에도 공지사항, 상품 목록, 부동산 매물, 입찰 공고, 이벤트, 문서 목록 같은 반복 목록/상세형 데이터를 수집할 수 있어야 한다.

확장 방향:

- collector 설정은 도메인 중립적으로 유지한다: request, pagination, mapping, validation, runtime.
- 공통 실행 단위는 site와 collector다.
- 공통 저장 단위는 장기적으로 `records`와 `record_details` 같은 domain-neutral 모델로 확장할 수 있다.
- 도메인별 필드는 고정 컬럼보다 `raw_json`, `details_json`, normalized view를 조합한다.
- 채용공고 전용 필드는 첫 유스케이스의 normalized view로 취급한다.

### 수집 모드 확장 (recursive crawl)

MVP는 목록·상세형 데이터만 다룬다. 장기에는 recursive crawl 모드(예: docs 사이트 전체, 특정 path 하위 모든 페이지)를 별도 collector type(예: `RecursiveCrawlCollector`)으로 추가하는 형태로 도입한다. 기존 `ApiJobsCollector`/`StaticHtmlCollector`/`DetailHtmlCollector`의 인터페이스와 데이터 모델은 변경하지 않으며, 새 collector type과 새 storage 테이블(예: `pages`)을 추가하는 방식으로 확장한다.

설계 방향 (구현은 MVP 이후):

- 기본 scope: path-prefix (`example.com/info`를 받으면 `/info/*` 하위만)
- 필수 scope rule: depth 상한, URL include/exclude 패턴, 외부 링크 정책(기본 not-follow), 재방문 정책(content hash 기반)
- 데이터 모델: `pages` 테이블을 별도로 둔다. 기존 `records`/`record_details`는 영향 없음.
- robots.txt, Crawl-delay, 사이트 ToS는 반드시 존중한다.

## 의존성과 설치 정책

### Python 요구사항

- Python 3.10 이상 (Scrapling의 요구사항)
- Ubuntu 22.04 LTS (Python 3.10) 또는 24.04 LTS (Python 3.12) 권장
- 로컬 개발 환경과 VM 모두 동일한 Python minor 버전 사용

### 의존성 관리

- `pyproject.toml` + lock file (`uv.lock` 권장)
- `uv sync`로 환경 재현
- Scrapling은 `scrapling[fetchers]>=0.4.7,<0.5`로 고정
- minor 버전 승격은 사람이 수동 검토 후 PR

### Scrapling 설치 단계

**단계 1: Python wheel 설치 (MVP부터 기본)**

```bash
uv sync
# 또는 pip install "scrapling[fetchers]>=0.4.7,<0.5"
```

이 단계에서는 Scrapling과 `curl_cffi`, `playwright`, `patchright` 등의 Python wheel이 설치되지만, Chromium 브라우저 바이너리는 설치되지 않는다. MVP에서 `Fetcher`(HTTP)만 사용하는 경우 이 단계로 충분하다.

**단계 2: Chromium 바이너리 설치 (Yellow Zone 활성화 시)**

```bash
scrapling install
```

`DynamicFetcher` 또는 `StealthyFetcher`를 실제로 사용하는 VM에서만 실행한다. 이 명령은 Chromium과 관련 바이너리를 다운로드하며, 디스크 공간을 추가로 차지한다.

### 코드 수준의 단계화

Python wheel이 설치되어 있어도 실제 사용하지 않는 fetcher는 import하지 않는다. `collectors/fetchers/__init__.py`는 HTTP fetcher만 eager import하고, dynamic/stealthy fetcher는 lazy import로 유지한다. 이를 통해 MVP 실행 시 playwright/patchright가 메모리에 로드되지 않는다.

### VM 배포 체크리스트

- Python 3.10+ 확인
- `uv`(또는 `pip`)로 `scrapling[fetchers]` 설치
- MVP 운영 중: `scrapling install` 실행하지 않음
- Yellow Zone 사이트 승인 시: 해당 VM에서 `scrapling install` 실행
- 설치 변경 후 `systemctl restart scraper.timer`

## 시크릿 관리

운영 시크릿은 코드와 분리해서 관리한다.

대상 시크릿:

- OpenAI API key
- Slack bot token, signing secret
- 사이트별 인증 헤더 (필요한 경우)

저장 위치:

- 로컬 개발: `.env` 파일 (git ignore). `.env.example`은 키 이름만 commit.
- VM: `/etc/scraper-ops/secrets.env` (root:scraper 0640). systemd unit의 `EnvironmentFile`로 주입한다.
- CI: 플랫폼 시크릿 저장소 (GitHub Actions secrets 등)

정책:

- 시크릿은 절대 git에 commit하지 않는다. pre-commit hook으로 `.env`, `*.key`, `*.pem` 패턴을 차단.
- LLM에게 보내는 evidence·prompt에서 시크릿 값을 자동 마스킹한다. 헤더의 `Authorization`, `Cookie`, `X-Api-Key` 등은 `***`로 치환.
- 로그에도 동일한 마스킹 규칙을 적용한다.
- Slack 메시지에 evidence를 게시할 때도 마스킹을 통과해야 한다.
- LLM은 시크릿을 생성·추출·저장하지 않는다 (LLM의 역할 섹션 참조).

## 로그 전략

운영 가시성과 디버깅을 위해 일관된 로그 정책을 둔다. evidence(실패 증거)와 로그(실행 기록)는 다른 자산이다.

위치:

- VM: `/var/log/scraper-ops/scraper.log` (또는 systemd journal)
- 로컬 개발: stdout

레벨:

- `DEBUG`: collector 내부 fetch·parse 단계, dry-run 세부
- `INFO`: run 시작·종료, 수집 건수, Phase 전이, approval 상태 변경
- `WARNING`: rate limit, retry, evidence 보관 정리 실패
- `ERROR`: validation 실패, exception, LLM 출력 검증 실패
- `CRITICAL`: approval server 다운, file lock 획득 실패

기본 레벨은 운영 환경 `INFO`, 로컬·CI는 `DEBUG`. 환경변수 `SCRAPER_LOG_LEVEL`로 override 가능.

포맷:

- 구조화 로그 (JSON lines). `run_id`, `site`, `collector`, `phase`, `event` 키 포함.
- 사람이 읽기 위해 `journalctl -u scraper -f` + `jq` 조합 권장.

Rotation:

- VM: `logrotate` 또는 systemd journal. 일별 회전, 14일 보관, gzip 압축.
- 디스크 1GB 초과 시 가장 오래된 파일부터 삭제.

마스킹:

- "시크릿 관리"의 마스킹 규칙을 로그에도 적용. 시크릿 값이 로그에 남지 않게 한다.

audit log 분리:

- approval 상태 전이, VM allowlist 변경, patch 적용 같은 운영 결정은 일반 로그와 별도로 `data/audit.log`에 append-only 기록.
- audit log는 rotation하지 않고 보관(MVP에서 1년).

## 운영 모델

MVP 운영은 단일 VM과 SQLite를 기본으로 한다.

- runner는 1회 실행 CLI다.
- VM에서는 `systemd timer`가 runner를 반복 실행한다.
- SQLite는 단일 runner와 낮은 쓰기 빈도에서 사용한다.
- file lock으로 중복 실행을 방지한다.
- VM은 repo 전체를 pull 받을 수 있지만 allowlist에 있는 사이트만 실행한다.
- 차단 민감 사이트는 `preferred_environment: local` 또는 별도 worker로 분리한다.

`SCRAPER_ALLOWED_SITES`와 `preferred_environment`는 목적이 다르며 분리해서 관리한다.

- `SCRAPER_ALLOWED_SITES`: 이 VM에서 자동 실행을 허가하는 사이트 목록. 운영 통제 게이트다. 변경 절차: SSH 접근 → 환경변수 수정 → `systemctl restart scraper.timer`. SSH 접근 자체가 인증 게이트이므로 별도 HITL 흐름은 필요 없다.
- `preferred_environment`: 사이트별 실행 위치 권장 설정. IP 차단 민감도를 표현한다. allowlist에 포함되어 있어도 `preferred_environment: local`인 사이트는 VM에서 실행하지 않는다.

IP 차단 우려가 있는 사이트는 allowlist 제외가 아니라 `preferred_environment: local`로 처리한다. 두 개념을 allowlist 하나로 묶으면 "허가됐는데 왜 안 돌아가나"라는 혼란이 생긴다.

PostgreSQL 전환은 여러 worker, 여러 실행 위치, Slack approval server 분리, 운영 UI 또는 분석 쿼리 증가가 생긴 뒤 검토한다.

## 성공 기준

MVP 성공 기준:

- catch API jobs collector가 전체 공고를 수집한다.
- 이후 실행에서 신규/변경 공고만 저장한다.
- API schema 변경 또는 빈 결과를 검증 실패로 감지한다.
- static HTML diagnostics가 차단/비정상 상태 판단에 포함된다.
- 실패 보고서와 Slack 알림이 충분한 맥락을 제공한다.
- VM에서 `systemd timer`로 안정적으로 실행된다.
- allowlist 정책으로 VM에서 허용된 사이트만 실행된다.

확장 성공 기준:

- 신규 사이트 온보딩에 필요한 사람의 작업이 URL/API 샘플/HAR 제공과 승인으로 줄어든다.
- LLM이 YAML config 초안과 patch candidate를 만들 수 있다.
- 승인된 YAML patch만 적용되고, rerun 결과가 추적된다.
