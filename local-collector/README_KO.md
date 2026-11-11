# AQE 로컬 상시 수집 실행 코드

대상: `haw-kkm/aqe-research` 커밋 `25b3623f50a6ee0306b96fb9901d90887408eca6`.
기존 `aqe collect`를 별도 프로세스로 실행하는 도구입니다. 기존 소스·YAML·모델·저장 데이터를 바꾸지 않습니다.

## 1. 파일 배치

ZIP을 풀고 폴더 이름을 `local-collector`로 해서 프로젝트 아래에 둡니다.

- `C:\vscode\aqe\local-collector\start_collect.ps1`
- `C:\vscode\aqe\local-collector\collect_local.py`
- `C:\vscode\aqe\local-collector\README_KO.md`

기존에 실행한 `aqe collect`나 `supervise.ps1 -Command collect`가 있다면 먼저 그 창에서 Ctrl+C로 중지합니다.
이 도구는 자신의 중복 실행을 OS 잠금으로 차단하고, 기존 수집기의 최근 상태 파일도 검사합니다.
예전 수집 명령은 이 잠금을 사용하지 않으므로 이 도구와 동시에 실행하지 마세요.

## 2. PC 시간 동기화

Windows 설정 → 시간 및 언어 → 날짜 및 시간 → 자동으로 시간 설정 → 지금 동기화.
시계 차이가 기존 설정 `clock.max_abs_skew_ms`(현재 1,000ms)를 넘으면 시작을 거부합니다.
이 도구가 시간 허용 범위를 늘리거나 시스템 시각을 자동으로 바꾸지는 않습니다.

## 3. 실행 전 확인

일반 PowerShell에서 다음을 실행합니다. 기존 `.venv`의 Python 3.12와 설치된 AQE 의존성이 필요합니다.
이 도구 자체를 위해 추가로 설치할 패키지는 없습니다. TradingAgents의 `.venv-ta`는 필요하지 않습니다.

```powershell
cd C:\vscode\aqe
powershell -ExecutionPolicy Bypass -File .\local-collector\start_collect.ps1 -Check
```

`data_root`가 `E:\aqe-v2\data`인지 확인합니다. 실제 경로는 `configs\local.yaml`에서 읽습니다.
데이터 폴더에 임시 파일을 쓰고 fsync 후 지우며, 디스크 여유와 Binance 공개 서버 시각을 확인합니다.
모델 폴더·과거 데이터는 변경하지 않습니다. 첫 네트워크 확인에 실패하면 연결 상태를 확인한 뒤 다시 실행합니다.

## 4. 최초에는 2분만 시험

```powershell
powershell -ExecutionPolicy Bypass -File .\local-collector\start_collect.ps1 -DurationSeconds 120
```

120초 수집 후 버퍼를 저장하고 종료합니다. 이후 상태 조회의 `STOPPED`는 정상입니다.
2분 동안에는 5분봉 마감 시각이 없을 수 있어 `5m_live` 파일이 아직 없을 수 있습니다.
호가 복원, 실제 파일 기록, 데이터 형식 검증이 학습 가능성을 뜻하지는 않습니다.

## 5. 계속 수집

```powershell
powershell -ExecutionPolicy Bypass -File .\local-collector\start_collect.ps1
```

이 창을 열어 둡니다. 기본 BTCUSDT 한 종목만 수집합니다. 다른 종목은 YAML에 등록 후 `-Instrument ETHUSDT`처럼 지정합니다.
30초마다 `HEALTH`를 출력하고, 기존 수집기의 자동 재연결을 사용합니다. 수집 프로세스가 종료되면 5~300초 간격으로 재시작합니다.
시작 검사 실패, 사용자 Ctrl+C, 디스크 부족은 계속 재시작하지 않고 종료합니다.
Windows 자동 절전 방지를 요청하고 종료 시 해제합니다. 화면은 꺼져도 됩니다. PC 종료·수동 절전·노트북 덮개 닫기는 별개입니다.
재부팅 후에는 이 명령을 다시 실행해야 합니다. 부팅 자동 시작을 등록하는 코드는 포함하지 않았습니다.

## 6. 다른 PowerShell 창에서 상태 확인

```powershell
cd C:\vscode\aqe
powershell -ExecutionPolicy Bypass -File .\local-collector\start_collect.ps1 -Status
# 계속 관찰하려면:
powershell -ExecutionPolicy Bypass -File .\local-collector\start_collect.ps1 -Status -Watch
```

| 항목 | 확인할 내용 |
|---|---|
| `state: OK` | 상태 파일·양쪽 피드가 최근이고, 호가 SYNCED, 호가·체결 수신 및 활성 원본 파일 기록이 확인됨 |
| `book: SYNCED` | REST 스냅샷과 WebSocket 업데이트가 연결돼 로컬 호가를 복원함 |
| `message_counts.depth`, `aggTrade` | 같은 실행 ID에서 시간이 지나며 증가함 |
| `raw_active_files` | 실제 E드라이브 파일 경로·크기 확인. 초기에는 압축 버퍼로 증가가 늦을 수 있음 |
| `run_id` | 재시작 때 바뀜. 수신 카운터도 새 실행 기준으로 시작함 |
| `disk_free_gib` | 실제 수집 디스크 여유 |
| `SYNCING`, `FEED_LAG`, `STALE` | 초기 동기화·피드 지연·오래된 상태. 지속되면 로그와 인터넷 확인 |
| `STOPPED` | 수집기가 버퍼 저장 후 종료함 |

활성 원본 파일은 회전하면 크기 합계가 줄어듭니다. 전체 누적 용량으로 해석하지 마세요.
`OK`는 수집 상태 진단이며 장기간 데이터 무결성, 학습 성능 또는 수익성 인증은 아닙니다.

## 7. 저장되는 것

설정이 현재와 같다면 다음 위치에 저장됩니다.

| 데이터 | 경로 |
|---|---|
| 호가 변경 원본 | `E:\aqe-v2\data\raw\binance\ws\public` |
| 체결·mark 가격·kline 원본 | `E:\aqe-v2\data\raw\binance\ws\market` |
| 호가 스냅샷·현재 OI 원본 | `E:\aqe-v2\data\raw\binance\ws\rest` |
| 흐름·호가 요약 | `E:\aqe-v2\data\normalized\flow_summaries` |
| 현재 OI 관측 | `E:\aqe-v2\data\normalized\oi_observations` |
| mark 가격·예상 funding | `E:\aqe-v2\data\normalized\mark_funding_estimates` |
| 실시간 확정 봉 | `E:\aqe-v2\data\normalized` 아래 기존 `5m_live` 파티션 |
| 수집 상태·체크포인트 | `E:\aqe-v2\data\status` |
| 실행별 stdout·오류 로그 | `E:\aqe-v2\data\logs\local_collect` |

기존 원본 저장·회전·체크포인트·호가 재동기화 로직을 그대로 사용합니다.
연결이 끊긴 동안 놓친 과거 호가가 자동 복구되는 것은 아닙니다. 재연결 시 현재 스냅샷으로 재동기화합니다.
예상 funding과 확정 funding 이력은 다릅니다. 이 도구는 기존 backfill이나 확정 funding 이력 수집 명령을 추가로 실행하지 않습니다.

## 8. 중지·디스크 여유

실행 창에서 **Ctrl+C 한 번**을 누르고 종료를 기다립니다.
Windows에서는 자식 프로세스에 Ctrl+Break를 보내 KeyboardInterrupt로 처리하게 하여 기존 수집기의 finally 저장 경로를 실행합니다.
30초 안에 종료되지 않으면 강제 종료하고 경고를 출력합니다. 그 경우 미저장 버퍼 손실 가능성이 있으니 다음 시작의 복구 로그를 확인합니다.
기본 여유 기준은 20GiB이며 실행 중에도 30초마다 검사합니다. 기준 미만이면 저장 후 중지합니다.
기존 파일을 자동 삭제하지 않습니다. 확인 주기 사이 급격한 사용량 증가나 장치 분리까지 완전히 방지하지는 못합니다.

```powershell
# 여유 50GiB를 남기고, 15초마다 확인하려면:
powershell -ExecutionPolicy Bypass -File .\local-collector\start_collect.ps1 -MinFreeGB 50 -PollSeconds 15
```

## 9. 학습과의 관계

이 실행기는 수집만 합니다. 공개 시장 데이터라 Binance 계정 API 키가 필요하지 않고, LLM 호출·주문·학습·평가 명령을 실행하지 않습니다.
기존 LightGBM·TCN은 다시 학습하지 않습니다. 수집된 호가를 모델 입력으로 쓰려면 앞으로 `flow_v1` 피처·라벨·학습 및 시간 순서 평가가 별도로 필요합니다.
데이터를 쌓는 것과 G01(실시간 봉 병합·shadow 성숙 라벨 채점) 구현은 병행할 수 있습니다.

## 검증 범위

제공한 `tests/test_collect_local.py`는 임시 폴더와 별도 자식 프로세스를 사용합니다.
시계 오류 거부, 디스크 부족, 파일 잠금, 오래된 피드 판정, 종료 신호와 저장 경로를 검증합니다. 실제 API 비용은 발생하지 않습니다.
추가 회귀 시험 **15개**가 Linux Python 3.12에서 통과했습니다. Windows 전용 API는 mock으로 확인했습니다.
사용자 Windows PC에서의 실제 Binance 연결·E드라이브 저장은 위 2분 시험으로 확인해야 합니다.
GitHub 원격 저장소를 수정하거나 push하지 않았습니다.
