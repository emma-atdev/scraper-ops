# scraper-ops

**scraper-ops**는 채용공고 사이트를 매시간 자동 수집하는 크롤링 운영 시스템. 사이트 응답 구조가 바뀌면 LLM이 yaml 패치를 제안하고, dry-run으로 검증한 뒤 Slack에서 사람이 승인하면 yaml이 자동 수정되어 재수집됨. LLM은 yaml만 수정하고 Python 코드는 사람만 수정.

### 현재 상태

- catch.co.kr — Mac launchd, 평일 10~18시 매시간 수집, SQLite 증분 적재
- 매일 09:00 KST 일일 요약을 Slack에 게시
- 자가치유 루프 완성: 실패 감지 → 증거 수집 → LLM 패치 → dry-run → Slack 승인 → yaml 자동 수정 → 재실행 → 실패 시 자동 롤백

### 로드맵

- 사이트 추가 — yaml만 추가하면 운영 가능. 다음 사이트 후보 진행 예정
- 신규 사이트 자동 등록 — URL 입력 시 LLM이 yaml 초안 생성 + fetcher 단계적 시도(헤더 → TLS 위장 → 헤드리스 브라우저), 사람은 샘플 검토 후 승인
- 상세 페이지 수집 — 현재는 목록만. 자격 요건·우대 사항·연봉 등 추가 적재
- Slack 버튼 결정 (M6.7) — CLI 명령 대신 Slack 메시지 버튼 클릭으로 승인/거절/재요청

---

## 빠른 시작

clone → `uv sync` → `.env` 채움 → 한 번 돌려보기.

```bash
git clone https://github.com/emma-atdev/scraper-ops.git
cd scraper-ops
uv sync
cp .env.example .env       # SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, OPENAI_API_KEY 채움

# 한 번 돌려보기
SCRAPER_ENVIRONMENT=local SCRAPER_ALLOWED_SITES=catch \
  uv run python -m app.runner --site catch --environment local

# 테스트
uv run pytest
```

성공하면 `data/scraper.db`에 행이 쌓이고, Slack에 변화·실패 알림이 옴.

---

## 3층 구조

```
┌──────────────┐   사이트별 동작 선언
│  YAML config │   ← LLM이 HITL 경유로만 수정
└──────┬───────┘
       │
┌──────▼───────┐   범용 수집 로직 (Python)
│  Collector   │   fetch → parse → map → validate → return
└──────┬───────┘   사이트 이름 박지 않음. DB·결정 안 함
       │
┌──────▼───────┐   1회 실행 진입점
│   Runner     │   launchd가 호출. file lock, run_id, DB 쓰기, Slack 알림
└──────────────┘
```

세 층이 명확히 분리. 사이트가 바뀌면 yaml만 고치면 되고 Python 코드는 그대로.

---

## 자가치유 흐름

수집이 깨졌을 때 자동으로 도는 5단계. 사람은 마지막 결정만 함.

```
[매시간 수집]
   │
   ▼  실패 감지 (schema_change / empty_results 등)
[evidence 수집] api_sample.json + report.json
   │
   ▼
[LLM 처방 생성] PatchCandidate (어느 path를 어떤 값으로 바꿀지)
   │
   ▼
[dry-run] 가짜 환경에서 patch 적용 후 결과 비교
   │     verdict: improved / regressed / unchanged / patch_invalid / patch_apply_failed
   ▼
[approval_request 생성] DB row, status=pending
   │
   ▼
[Slack에 처방 카드 게시] ← 여기까지 자동
   │
   ▼  사람의 결정 (CLI)
   ├─ approve    → yaml 수정 + rerun. 실패 시 자동 롤백
   ├─ reject     → 닫음. 24h 동안 같은 site 자동 healing 스킵
   └─ regenerate → LLM에 다른 후보 요청 (run_id당 최대 3회)
```

자동 healing이 호출되는 조건 (모두 통과해야 함):
1. 진단 카테고리가 `schema_change` 또는 `empty_results`
2. 같은 site에 과거 성공 run 1건 이상 (신규 사이트 가드)
3. 같은 site에 pending approval 없음 (스팸 방지)
4. 같은 site에 최근 24h 내 rejected 없음 (D-1 가드)
5. api_sample 캡처돼 있음
6. `OPENAI_API_KEY` 설정됨

모든 상태 전이는 `data/audit.log`에 append-only JSON Lines로 기록.

---

## CLI 명령

| 모드            | 명령                                                              | 의미                                                              |
| --------------- | ----------------------------------------------------------------- | ----------------------------------------------------------------- |
| `collect` (기본) | `python -m app.runner --site catch`                               | 1회 수집. launchd가 매시간 호출                                   |
| `daily_summary` | `python -m app.runner --mode daily_summary`                       | 어제(KST) 운영 요약을 Slack에 게시                                |
| `approve`       | `python -m app.runner --mode approve --id 7`                      | 처방 #7 승인 → yaml 적용 + rerun + 결과 thread reply              |
| `reject`        | `python -m app.runner --mode reject --id 7 --reason "..."`        | 처방 #7 거절. 같은 thread에 회신                                  |
| `regenerate`    | `python -m app.runner --mode regenerate --id 7`                   | 처방 #7을 superseded로 마감하고 LLM에 다른 후보 요청              |

`--notify`: `failure_or_change`(기본) / `failure` / `always` / `never`.

---

## Capability Matrix — 무엇을 시도하고 무엇을 안 하는가

수집 가능 영역을 셋으로 나눔. LLM이 이 경계 밖 해법(CAPTCHA 우회·proxy rotation·UA 랜덤화 등)을 제안하면 자동 거부.

- **Green Zone** — `Fetcher`(HTTP, curl_cffi). 자동 진행
- **Yellow Zone** — `DynamicFetcher`(Playwright) / `StealthyFetcher`(patchright). 사람 승인 후에만 활성화
- **Red Zone** — CAPTCHA, 로그인 강제, 검색엔진 SERP 등. 시도 자체를 안 함

---

## 운영 배치

Mac launchd로 자동 실행. `~/Library/LaunchAgents/`에 plist 두 개:
- `com.scraper-ops.catch.plist` — 평일 10~18시 매시간
- `com.scraper-ops.daily-summary.plist` — 매일 09:00 KST

```bash
launchctl load ~/Library/LaunchAgents/com.scraper-ops.catch.plist
launchctl list | grep scraper-ops
```

---

## 디렉토리 구조

```
app/
  runner/         # 1회 실행 CLI 진입점, healing flow 통합
  collectors/     # ApiJobsCollector + Fetcher wrappers
  config/         # pydantic SiteConfig schema, YAML loader
  storage/        # SQLite (WAL) + Repository, ApprovalRepository
  validators/     # 결과 0건, 필드 누락, 수집량 급감, schema 변경
  diagnosis/      # 실패 카테고리 분류
  healing/        # patch candidate 생성, dry_run, format_patch_diff
  patching/       # 승인된 patch를 yaml에 적용 + 자동 롤백
  approval/       # approval_request 상태 머신
  integrations/   # Slack
  llm/            # LLM 호출, prompt 빌드, Capability Matrix 주입
  audit.py        # data/audit.log append-only
  clock.py        # KST 헬퍼 (사람이 보는 시각은 KST, DB 저장은 UTC)
configs/sites/    # 사이트별 yaml (LLM이 HITL 경유로 수정)
data/             # 런타임 산출물 (gitignore)
  snapshots/{site}/{run_id}/...
  reports/{site}/{run_id}/report.json
  yaml-backups/{site}/{ts}.yaml   # patch 적용 전 자동 백업
  audit.log
docs/             # product-plan, deployment, study-summary
tests/
```
