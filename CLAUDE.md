# CLAUDE.md

이 파일은 Claude Code가 `scraper-ops` repo에서 작업할 때 따라야 할 프로젝트 규칙이다. 글로벌 CLAUDE.md와 함께 적용된다.

## Single Source of Truth

설계·정책·범위에 관한 모든 결정은 `docs/product-plan.md`에 있다. 의문이 생기면 거기를 먼저 본다. 다른 docs(`architecture.md`, `ops-strategy.md`, `roadmap.md`)는 product-plan과 충돌하면 product-plan이 우선한다.

## 3층 아키텍처

이 프로젝트는 의도적으로 3개 층으로 분리되어 있다.

- **Runner** (`app/runner/`): systemd timer가 호출하는 1회 실행 CLI. site + collector를 조립해 실행. file lock, run_id, 환경 감지 담당. DB 쓰기·Slack 알림 같은 side effect는 여기서.
- **Collector** (`app/collectors/`): 범용 수집 구현체. `ApiJobsCollector`, `StaticHtmlCollector`, `DetailHtmlCollector`. **DB 쓰기·로직 결정 금지** — fetch → parse → map → validate → return만 한다. 사이트별 동작은 YAML로 받는다.
- **YAML config** (`configs/sites/*.yaml`): 사이트별 선언. LLM이 HITL 경유로 생성·수정 가능한 유일한 영역.

**LLM 권한 경계**: LLM은 `configs/sites/*.yaml`만 수정한다. `app/`의 Python 코드는 사람만 수정한다. "LLM 자율 시도"는 YAML 필드값 dry-run 반복이지 Python 코드 생성이 아니다.

## Capability Matrix

수집 가능성은 Green / Yellow / Red Zone으로 정의되어 있다 (product-plan의 "Scrapling Capability Matrix" 섹션).

- Green Zone: `Fetcher`(HTTP, curl_cffi). MVP 기본 경로.
- Yellow Zone: `DynamicFetcher`(Playwright), `StealthyFetcher`(patchright). **사람 승인 기반**으로만 활성화.
- Red Zone: CAPTCHA, 엔터프라이즈 anti-bot, 로그인 강제, 검색엔진 SERP 등. **수집 시도 자체를 하지 않는다.**

LLM이 Capability Matrix 범위 밖의 해결책(CAPTCHA 우회, proxy rotation, user-agent 랜덤화 등)을 제안하면 자동 거부.

## Execution Lifecycle

- **Phase 1 Setup**: 사람이 fetcher·URL 결정, agent가 config 자율 생성 + dry-run, 사람이 샘플 확인 후 승인 (Step E)
- **Phase 2 Operation**: agent 자율 (scheduled run, 증분, evidence 보관)
- **Phase 3 Change/Failure**: 기존 작동 config 변경은 반드시 HITL

## 디렉토리 구조

```
app/
  runner/             # 1회 실행 CLI 진입점
  collectors/
    base.py           # BaseFetcher, FetchResult, Collector, CollectorResult
    fetchers/
      http.py         # Scrapling Fetcher wrapper (HttpFetcher) — MVP 기본
      dynamic.py      # DynamicFetcher wrapper (lazy import)
      stealthy.py     # StealthyFetcher wrapper (lazy import)
    api_jobs.py       # ApiJobsCollector
    static_html.py    # StaticHtmlCollector
    detail_html.py    # DetailHtmlCollector
  config/             # pydantic schema, YAML loader
  storage/            # SQLite (WAL, busy_timeout 5s)
  validators/         # 결과 0건, 필드 누락, 수집량 급감, schema 변경
  diagnosis/          # 실패 범주화
  healing/            # patch candidate 생성 (LLM 호출)
  patching/           # 승인된 YAML patch 적용 + rerun
  integrations/
    slack.py
  llm/                # LLM 호출, prompt 빌드 (Capability Matrix 주입)
  models.py           # 도메인 model (JobPosting 등)
  env.py              # .env 로드
  locking.py          # RunLock
  runtime.py          # allowed sites, preferred_environment 판정
configs/sites/*.yaml  # LLM이 HITL 경유로 수정 가능
data/                 # 런타임 산출물 (gitignore)
  snapshots/{site}/{run_id}/...
  reports/{site}/{run_id}/report.json
  audit.log
docs/product-plan.md  # SSoT
tests/
```

## 개발 환경

- Python 3.13
- 의존성 관리: `uv` (`uv sync`, `uv add`)
- Scrapling: `scrapling[fetchers]>=0.4.7,<0.5`
- MVP 운영 중에는 `scrapling install`(Chromium 다운로드) **실행하지 않음**. Yellow Zone 사이트 승인 시에만 실행.

```bash
uv sync
uv run pytest

# 로컬에서 catch (yaml은 preferred_environment: vm 이라 환경 위장 필요)
SCRAPER_ENVIRONMENT=vm SCRAPER_ALLOWED_SITES=catch \
  uv run python -m app.runner --site catch --environment vm --notify always
```

## 운영 (VM 배포)

자세한 절차는 `docs/deployment.md` 참조. 핵심:

- VM Ubuntu 22.04+ + Python 3.13 + uv
- `/opt/scraper-ops/`에 git clone, `uv sync`
- `/etc/scraper-ops/secrets.env`에 시크릿
- `ops/systemd/scraper.service`, `scraper.timer`를 `/etc/systemd/system/`로 복사
- `sudo systemctl enable --now scraper.timer`로 매시간 자동 실행
- 로그: `journalctl -u scraper.service -f -o cat | jq .`

## 코딩 스타일

- Python 3.13, 4-space indent, PEP 8
- 사이트별 동작은 가능한 한 YAML로. Python 코드에 사이트 이름 박지 않기.
- 작은 모듈 우선. 한 파일에 책임 여러 개 넣지 않기.
- 타입 힌트는 public 인터페이스에 필수.
- 모든 외부 호출(HTTP, 파일, DB)은 mock 가능한 형태로.

## 테스트

- pytest. 파일명 `test_<module>.py`.
- 네트워크 호출은 기본 mock. 실제 스크래핑 검증은 수동 또는 명시적 마크.
- 테스트는 `app.*` import만 한다 (third-party 직접 import 지양).

## 안전 규칙

- **시크릿 commit 금지**: Slack token, OpenAI key, cookie, 사이트 인증 헤더
- 시크릿은 `.env` (로컬), `/etc/scraper-ops/secrets.env` (VM), CI는 플랫폼 시크릿
- LLM 호출 시 evidence·prompt는 자동 마스킹: `Authorization`, `Cookie`, `X-Api-Key` 등 → `***`
- 로그·Slack 메시지에도 동일 마스킹 적용
- **LLM은 Python 코드 수정 금지**, YAML만 수정 가능
- 운영 결정(approval 전이, allowlist 변경, patch 적용)은 `data/audit.log`에 append-only

## 작업 원칙 (글로벌 CLAUDE.md 보강)

- 코드 변경 전 product-plan과의 일관성 점검
- 새 collector 타입 추가는 사람이 Python으로 작성 + PR. LLM은 YAML만 수정 (LLM 권한 경계)
- **마일스톤·서브마일스톤 단위로 commit·push**. 한 단위(예: M6.1, M6.2 등)가 끝나면 테스트 통과 확인 후 commit 메시지 제안 → 사용자 push → 다음 단위 진입. 큰 변경을 하나의 commit에 누적하지 않는다.
