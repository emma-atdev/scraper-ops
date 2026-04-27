# VM 배포 가이드

`scraper-ops`를 VM에 올려 systemd timer로 매시간 자동 실행하기 위한 절차.

## 사전 요구사항

- Ubuntu 22.04 LTS (Python 3.10) 또는 24.04 LTS (Python 3.12) 권장. Python 3.13이 시스템에 없으면 `pyenv` 또는 `deadsnakes` PPA로 설치.
- SSH 접근 권한
- 인터넷 외부 통신 가능 (catch API, Slack API)
- 디스크 1GB 이상 (Scrapling Yellow Zone 활성화 시 추가 수백 MB)

## 1. 디렉토리 구조 만들기

```bash
sudo mkdir -p /opt/scraper-ops
sudo mkdir -p /etc/scraper-ops
sudo chown -R "$USER":"$USER" /opt/scraper-ops
```

## 2. 코드 배포

```bash
cd /opt
sudo chown -R "$USER":"$USER" /opt
git clone <repo-url> scraper-ops
cd scraper-ops
```

이후 업데이트는:

```bash
cd /opt/scraper-ops
git pull
.venv/bin/python -m pip install -U uv  # uv 자체는 pip로 1회 설치
uv sync
sudo systemctl restart scraper.timer
```

## 3. Python 환경 + 의존성

`uv`가 설치되어 있다고 가정.

```bash
cd /opt/scraper-ops
uv sync
```

설치 완료 후 검증:

```bash
.venv/bin/python -c "from app.runner.cli import main; print('ok')"
```

`scrapling[fetchers]`로 playwright/patchright wheel은 설치되지만 Chromium 바이너리는 미설치. MVP 운영(Green Zone 사이트만)은 이 상태로 충분. **`scrapling install` 명령은 실행하지 않는다.**

## 4. 시크릿 파일 작성

```bash
sudo install -m 640 -o root -g "$USER" /dev/null /etc/scraper-ops/secrets.env
sudo nano /etc/scraper-ops/secrets.env
```

내용 (`.env.example` 참조):

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=C...
SCRAPER_ALLOWED_SITES=catch
# OPENAI_API_KEY=sk-...   # M6 self-healing 활성화 시
# SLACK_SIGNING_SECRET=... # M6 approval server 활성화 시
```

권한은 `0640 root:<운영사용자>`로 두어 일반 사용자가 못 읽게 함.

## 5. systemd unit 등록

```bash
sudo cp /opt/scraper-ops/ops/systemd/scraper.service /etc/systemd/system/
sudo cp /opt/scraper-ops/ops/systemd/scraper.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now scraper.timer
```

활성화 확인:

```bash
systemctl status scraper.timer        # active (waiting)
systemctl list-timers scraper.timer   # 다음 발동 시각
```

## 6. 첫 수동 실행으로 검증

timer가 정각까지 기다리지 말고 한 번 즉시 호출:

```bash
sudo systemctl start scraper.service
journalctl -u scraper.service -f
```

JSON 로그 라인이 흘러가야 함:

```
{"event":"collector_start","site":"catch",...}
{"event":"site_done","site":"catch","inserted":N,...}
```

성공 시 SQLite 파일과 보고서 확인:

```bash
sqlite3 /opt/scraper-ops/data/scraper.db "select count(*) from job_postings"
ls /opt/scraper-ops/data/reports/catch/
```

실패 시 Slack 채널에 알림 도착 (`--notify failure` 기본값).

## 7. 운영 명령

```bash
# 상태
systemctl status scraper.timer
systemctl status scraper.service
systemctl list-timers scraper.timer

# 수동 실행
sudo systemctl start scraper.service

# 로그 (JSON)
journalctl -u scraper.service -f
journalctl -u scraper.service -n 200 -o cat | jq .
journalctl -u scraper.service --since "1 hour ago" -o cat | jq 'select(.level=="ERROR")'

# 잠시 멈추기
sudo systemctl stop scraper.timer
sudo systemctl disable scraper.timer

# 재시작 (코드 또는 unit 파일 변경 후)
sudo systemctl daemon-reload
sudo systemctl restart scraper.timer

# 시크릿 변경 후
sudo systemctl restart scraper.timer
```

## 8. 사이트 allowlist 변경

`SCRAPER_ALLOWED_SITES`를 추가/제거할 때:

```bash
sudo nano /etc/scraper-ops/secrets.env
sudo systemctl restart scraper.timer
```

이 절차는 **SSH 접근이 인증 게이트**다. Slack HITL 흐름이 아니라 SSH 운영 절차로만 처리한다 (기획서 운영 모델 섹션 참조).

## 9. 보관 정책 동작 확인

- evidence 보관: snapshots 30일, reports 90일, 정상 run snapshot 7일 후 삭제. 매 run 시작 시점에 자동 cleanup.
- 디스크 사용량 모니터링:

  ```bash
  du -sh /opt/scraper-ops/data/snapshots /opt/scraper-ops/data/reports
  ```

## 10. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `systemctl status scraper.service` Failed (exit 1) | `journalctl -u scraper.service` 로 traceback 확인 |
| 첫 실행에서 `no site configs found` | `WorkingDirectory` 또는 `configs/sites/`에 yaml 없음 |
| `runtime policy` skip | `SCRAPER_ALLOWED_SITES`에 사이트 미등록 또는 yaml `preferred_environment`가 vm 아님 |
| Slack 알림 안 옴 | `EnvironmentFile`에 `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_ID` 누락. `journalctl ... | grep slack`로 확인 |
| Lock 에러 | `data/locks/scraper-ops.lock` 잔재. `sudo rm` 후 재시도 |
| 디스크 가득 참 | snapshot/report 보관 정책 점검, 임시로 `/opt/scraper-ops/data/snapshots/{old}` 수동 삭제 |

## 11. 로컬 개발 → VM 배포 차이 한눈에

| 항목 | 로컬 (개발) | VM (운영) |
|---|---|---|
| 호출자 | 사람 (수동 `python -m app.runner`) | systemd timer (자동 매시간) |
| 환경변수 | `.env` 파일 | `/etc/scraper-ops/secrets.env` |
| `SCRAPER_ENVIRONMENT` | `local` | `vm` |
| catch.yaml `preferred_environment` | `vm` (그대로) | `vm` |
| 로그 위치 | stdout 콘솔 | systemd journal (`journalctl`) |
| 알림 정책 | `--notify always` 또는 `never` (디버깅) | `--notify failure` (기본) |
| 로컬에서 yaml `vm` 사이트 테스트 | `SCRAPER_ENVIRONMENT=vm SCRAPER_ALLOWED_SITES=catch uv run python -m app.runner --site catch --environment vm --notify always` | — |
