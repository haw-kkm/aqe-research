# RESUME_GOOGLE_CLOUD — Google Cloud 단독 운영 전환 진행 기록

갱신 2026-11-13 KST. 상세 설계·명령은 `docs/CLOUD_DEPLOYMENT.md` §15, 결정은 `docs/DECISIONS.md` ADR-0016, 개발 전체 인수인계는 `docs/RESUME_DEV.md`.

## 1. 위치·상태
| 항목 | 값 |
|---|---|
| 개발 경로 / 브랜치 | `C:\vscode\aqe-dev` / `dev/cloud-deploy-update` |
| 시작 기준 커밋 | `3589893eed316f5bcafe376f2f697fa265e0b88f` (ADR-0015 클라우드 배포) |
| 이 작업의 커밋 | `3589893` 위의 "Google Cloud 단독 운영" 커밋 1개(이 문서 포함). `git log --oneline -3`으로 확인. push 안 함 |
| 원본(읽기 전용) | `C:\vscode\aqe` main `1785160` + 실행 중 수집기, `E:\aqe-v2\data`, `E:\aqe-v2\artifacts` — 쓰기·중지 0 |
| 시험용 경로 | `E:\aqe-v2-dev\collector-smoke-google`, `E:\aqe-v2-dev\cloud-sim\google-train-sync`, `E:\aqe-v2-dev\verify\` |

## 2. 목표 구성
- **Collector VM**(24시간): Ubuntu 24.04 x86-64, 초기 후보 **e2-medium(4 GB)**, 역할 `google-collector`, 수집기 전용 환경(ML 없음),
  데이터는 별도 영구 디스크 `/srv/aqe`. systemd `aqe-collector@BTCUSDT`(부팅 시 자동 시작, `Restart=always`, SIGINT 정상 종료) + `aqe-health@` 5분 timer.
- **Training VM**(필요할 때만): `google-cpu-train`/`google-gpu-train`, `sync_from_collector.sh`로 증분 pull → `train_pipeline.sh` → `--shutdown-after`.
- 두 VM은 독립: 학습 VM이 꺼져 있어도 수집 VM은 계속 수집(pull 방식, 수집 VM은 학습 VM을 모름).

## 3. 완료한 기능 (전부 이 커밋)
| 영역 | 내용 | 파일 |
|---|---|---|
| 역할 | `google-collector`, `COLLECTOR_ROLES`, 메모리 사실(`/proc/meminfo`) | `src/aqe/ops/platform.py` |
| 설정·의존성 | Oracle 수집기와 같은 수집 설정(시험 강제), x86-64 수집기 전용 고정(ARM64 파일과 동일, ML 없음) | `configs/cloud/google_collector.yaml`, `requirements/linux-x86_64-collector.txt` |
| 사전 점검 | 수집기 역할 공통(Binance 실시간·시계·단일 수집기·시간 동기화, ML import 제외), 451 지역 차단/403 WAF 문구, 메모리·스왑, 데이터 디스크 분리, `AQE_ROLE` 불일치 FAIL | `src/aqe/ops/preflight.py`, `src/aqe/ops/__main__.py` |
| 배포 | bootstrap(env 파일 역할 값 1회 생성, pull 키, 디스크·메모리 안내), install(google-collector 기본 수집기 전용, 요구 파일 CPU 일치, `AQE_ROLE` 일치), 유닛 `--role ${AQE_ROLE}` | `deploy/linux/*.sh`, `deploy/linux/aqe.env.example`, `deploy/linux/systemd/*` |
| 결함 수정 | `aqe` 계정 nologin 셸 → 모든 `ssh aqe@host` 전송 실패 → `/bin/bash`, 비밀번호 없음(키 전용) | `deploy/linux/bootstrap_host.sh` |
| 동기화 | 범용 `sync_from_collector.sh`(+ 호환 래퍼 `sync_from_oracle.sh`), `train_pipeline.sh --sync-from`(완료·충돌 0일 때만 학습), `transfer --ssh-option` | `deploy/google/*.sh`, `src/aqe/ops/transfer.py` |
| 전송 정확도 | 미러 출처 = 저장소 ID `status/data_store.json`(감독자가 1회 생성, 전송 안 됨), 합친 버전 `absorbed` → `SKIP_MERGED` | `src/aqe/ops/transfer.py`, `src/aqe/ops/merge.py`, `local-collector/collect_local.py` |
| 전환 | `cutover-approve --reference linux` | `src/aqe/ops/cutover.py` |
| 시험 | `tests/test_collector_host.py` 15개(수집기 전용 환경에서도 실행), `tests/test_cloud_ops.py` google_collector 매개변수 | `tests/` |

기존에 있던 것(재사용): SIGINT/SIGTERM 정상 종료·flush·checkpoint, `.open` 복구, OS 잠금 + 소유 표시(중복 수집기 차단), 디스크·Binance 시계 사전 점검,
품질 일지(호가 desync·끊김·재시작), 헬스체크, DRY-RUN 기본·SHA-256·충돌 보관·SQLite 복사 후 백업 전송, 모델 릴리스(build→stage→verify→`--approve` 활성화→롤백).

## 4. 마지막 시험 결과 (Windows 개발 PC, `.venv` Python 3.12.10)
- 전체 `pytest -q -p no:cacheprovider`: **266 passed**, exit 0, skip 0 (커밋 직전 트리)
- 변이 확인 5종 모두 검출 · ML 차단 수집기 시험 부분집합 54 passed · x86-64 수집기 의존성 24/24 WHEEL + pip dry-run 24개 해석
- ML 차단 실제 수집 60초+40초(google_collector 설정) exit 0, `.open` 0, final · 실데이터 미러 동기화 리허설: 1차 9 배치, 2차 5 배치·4 갱신·충돌 0
- 커밋 후 `code-release-check --run-tests` 결과는 §8에 기록

## 5. 재실행 명령
```powershell
cd C:\vscode\aqe-dev
git status --short; git log --oneline -3
.venv\Scripts\python -m pytest -q -p no:cacheprovider                                   # 전체
.venv\Scripts\python -m pytest -q -p no:cacheprovider tests\test_collector_host.py tests\test_cloud_ops.py local-collector\tests
.venv\Scripts\python -m aqe.ops code-release-check --repo . --run-tests --out E:\aqe-v2-dev\verify\release_check_google.json
.venv\Scripts\python scripts\check_wheels.py requirements\linux-x86_64-collector.txt --platform linux_x86_64
```

## 6. 남은 작업 / 다음 단계 (사용자 결정·실행)
1. push 여부 결정: `git -C C:\vscode\aqe-dev push origin dev/cloud-deploy-update`
2. 수집 리전 결정(Binance 허용 리전; 미국 리전은 451 위험) → Collector VM(e2-medium)·데이터 디스크 생성 → §15.1 설치 → 시작·정지 실측(`final: true`, `.open` 0)
3. Training VM 생성 → pull 키를 수집 VM `authorized_keys`에 등록 → `sync_from_collector.sh --accept-new-hostkey`(DRY-RUN) → `--execute`
4. Windows → Collector VM 이전(safe, DRY-RUN 먼저) → 병행 ≥ 24h → `cutover-compare` → 승인 → 사용자가 Windows 수집기 Ctrl+C → 최종 증분 → `transfer-merge`
5. 모델: Training VM에서 `release-build` → 추론할 호스트(Training VM 또는 전체 환경으로 설치한 VM)에서 `release-stage`/`release-verify` → `release-activate --approve`

## 7. 알려진 문제·미검증
- 실제 Google VM에서 미실행: bootstrap·install, x86-64 실제 pip 설치·import, systemd 시작/정지·재부팅 자동 시작, Linux SIGTERM 실제 경로, Google 리전에서 Binance, VM 간 SSH(`/bin/bash` 셸·잠긴 비밀번호 키 로그인), 디스크 마운트 판정.
- 수집 VM은 ML 없음 → shadow 분석은 이 배치에서 기본 비활성. 수집 VM에서 추론까지 하려면 `AQE_REQUIREMENTS=requirements/linux-x86_64-cpu.txt`로 전체 환경 설치(e2-medium 4 GB 기준, 미실측).
- 헬스 WARN과 수집 정지가 같은 여유 공간(`AQE_MIN_FREE_GB`)에서 일어남(사전 경고 없음) — 디스크는 여유 있게.
- TCN epoch 체크포인트 없음 → Spot VM 비권장. G01(실시간 봉 병합) 미완.
- 원본 `E:\aqe-v2\data\meta`의 `aqe_meta.sqlite-shm/-wal`(2026-11-12 생성)은 지시대로 그대로 둠.

## 8. 사용자 승인 전 금지
GitHub push, main 병합, 클라우드 자원 생성·유료 서비스 활성화, 서버 배포, 기존 Windows 수집기 중지·전환, `E:\aqe-v2` 쓰기·삭제, SQLite 원본 열기,
모델 재학습·평가 기준 변경, 실거래 활성화(`RESEARCH_ONLY`, `approval_required=true`, `execution_enabled=false` 유지), 파괴적 git 명령.

### 커밋 후 검증 기록
(커밋 후 갱신)
