# 운영 배포 가이드

`scraper-ops`의 운영 형태는 사이트 IP 정책에 따라 둘로 나뉜다 (product-plan의 "운영 모델" 참조).

- **형태 A. Mac `launchd`** — 한국 IP가 필요한 사이트 (예: catch.co.kr). 사용자 노트북에서 정해진 시각에 자동 실행.
- **형태 B. VM `systemd timer`** — 외국 IP 무관한 사이트, Slack approval server, LLM 호출 노드.

각 형태별 절차는 아래 별도 섹션. 운영 명령(7번)·트러블슈팅(10번) 등 공통 항목은 양쪽에 적용된다.

---

## A. Mac launchd 배포

### 사전 요구사항

- macOS 13+ 권장 (`launchd` 일반 동작)
- Python 3.13 + `uv` 설치
- 사용자 한국 ISP에 연결 (catch 같은 한국 IP 요구 사이트 운영용)
- Mac이 운영 시간대(예: 평일 09:30~18:30) 동안 켜져 있고 sleep 안 들어가는 정책

### A-1. 의존성 설치

```bash
cd /Users/JeongminChoi/scraper-ops
uv sync
.venv/bin/python -c "from app.runner.cli import main; print('ok')"
```

### A-2. `.env` 작성

`.env.example` 참고해 시크릿 채움:

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=C...
SCRAPER_LOG_LEVEL=INFO
```

`SCRAPER_ALLOWED_SITES`는 launchd 운영에선 불필요 (allowlist는 VM 게이트). yaml의 `preferred_environment: local`이 게이트 역할.

### A-3. catch.yaml 환경 설정

```yaml
runtime:
  preferred_environment: local
```

### A-4. launchd plist 작성

`~/Library/LaunchAgents/com.scraper-ops.catch.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.scraper-ops.catch</string>
  <key>WorkingDirectory</key>
  <string>/Users/JeongminChoi/scraper-ops</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/JeongminChoi/scraper-ops/.venv/bin/python</string>
    <string>-m</string><string>app.runner</string>
    <string>--site</string><string>catch</string>
    <string>--environment</string><string>local</string>
    <string>--notify</string><string>failure</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SCRAPER_ENVIRONMENT</key><string>local</string>
    <key>SCRAPER_LOG_LEVEL</key><string>INFO</string>
  </dict>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>11</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>13</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>14</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>15</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>16</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>17</integer><key>Minute</key><integer>0</integer></dict>
    <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>0</integer></dict>
  </array>
  <key>StandardOutPath</key>
  <string>/Users/JeongminChoi/scraper-ops/data/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/JeongminChoi/scraper-ops/data/launchd.err.log</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
```

### A-5. 등록 + 검증

```bash
launchctl load ~/Library/LaunchAgents/com.scraper-ops.catch.plist
launchctl list | grep scraper-ops             # 등록 확인
launchctl start com.scraper-ops.catch         # 즉시 1회 실행
tail -f /Users/JeongminChoi/scraper-ops/data/launchd.out.log
# event:"site_done" + inserted 숫자 보이면 OK
```

### A-6. Mac sleep 정책

```bash
# 전원 연결 시 시스템 sleep 안 들어가게 (디스플레이만 꺼짐)
sudo pmset -c sleep 0
sudo pmset -c displaysleep 10
```

### A-7. 운영 명령

```bash
# 잠시 멈추기
launchctl unload ~/Library/LaunchAgents/com.scraper-ops.catch.plist

# 다시 켜기
launchctl load ~/Library/LaunchAgents/com.scraper-ops.catch.plist

# 즉시 1회 실행
launchctl start com.scraper-ops.catch

# 다음 실행 시각 확인
launchctl print "gui/$UID/com.scraper-ops.catch" | grep -A2 "next fire"

# DB 결과
sqlite3 /Users/JeongminChoi/scraper-ops/data/scraper.db "select run_id, status, inserted, updated, unchanged from runs order by started_at desc limit 5"

# 로그 보기 (구조화 JSON)
tail -200 /Users/JeongminChoi/scraper-ops/data/launchd.out.log | jq .
```

### A-8. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `launchctl list`에 안 보임 | plist XML 문법 오류. `plutil ~/Library/LaunchAgents/com.scraper-ops.catch.plist` |
| 정해진 시각에 실행 안 됨 | Mac이 sleep이거나 꺼짐. `pmset` 정책 확인 |
| 실행은 됐는데 `skip catch` | catch.yaml `preferred_environment` 가 `local` 아님 |
| Slack 알림 안 옴 | `.env`의 `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_ID` 누락. `data/launchd.err.log` 확인 |
| Lock 에러 | `data/locks/scraper-ops.lock` 잔재. 제거 후 재시도 |

---

## B. VM systemd timer 배포

### 사전 요구사항

- Ubuntu 22.04 LTS (Python 3.10) 또는 24.04 LTS (Python 3.12) 권장. Python 3.13이 시스템에 없으면 `deadsnakes` PPA로 설치.
- SSH 접근 권한
- 인터넷 외부 통신 가능 (Slack API 등)
- 디스크 1GB 이상 (Scrapling Yellow Zone 활성화 시 추가 수백 MB)
- **catch.co.kr 같은 한국 IP 필수 사이트는 VM에서 동작 보장 안 됨** — 외국 region VM은 IP 차단 가능. 그런 사이트는 형태 A로 운영.

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

## 11. 운영 형태별 차이 한눈에

| 항목 | A. Mac `launchd` (한국 IP 필요 사이트) | B. VM `systemd` (외국 IP OK 사이트) |
|---|---|---|
| 호출자 | `launchd`가 정해진 시각마다 | `systemd timer`가 매시간 |
| 환경변수 | `.env` 파일 | `/etc/scraper-ops/secrets.env` |
| `SCRAPER_ENVIRONMENT` | `local` | `vm` |
| 사이트 yaml `preferred_environment` | `local` | `vm` |
| 로그 위치 | `data/launchd.out.log` (또는 stdout) | systemd journal (`journalctl`) |
| 알림 정책 | `--notify failure_or_change` (기본) | `--notify failure_or_change` (기본) |
| 24/7 가동 | ❌ Mac 켜진 시간만 | ✅ |
| 비용 | 0 (보유 Mac) | VM 호스팅 비용 |
| 적합 사이트 | catch.co.kr 등 한국 IP 차단 회피 필요 | 글로벌 사이트, M6 approval server, LLM 노드 |
