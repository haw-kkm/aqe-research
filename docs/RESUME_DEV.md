# RESUME_DEV — 개발 worktree 인수인계 (토론 계층 · trade plan · 클라우드 배포 · Google 단독 운영)

갱신: 2026-11-13 02:20 KST(Google 단독 운영, §2b) · 이전: 2026-11-12 23:45 KST(릴리스 정리) · 개발 폴더 전용 문서. 원본 `C:\vscode\aqe`의 BUILD_STATUS/NEXT_SESSION은 수정하지 않았다.

## 1. 위치·브랜치·격리 (현재 상태)
| 항목 | 값 |
|---|---|
| 원본(수집기 사용 중) | `C:\vscode\aqe` · `main` @ `1785160`. 이번 세션 원본 코드·설정·venv 쓰기 0건, git status 깨끗함 |
| 실행 중 수집기 | `local-collector/collect_local.py --project-root C:\vscode\aqe` (PID 6220/32640/35508/35964, 2026-11-11 13:40 KST 시작, run `run-20261111T044005Z-29fc10ca`), data_root `E:\aqe-v2\data`. 중지·재시작 없음(2026-11-13 02:20 KST에도 같은 PID 4개 확인) |
| 개발 폴더 | `C:\vscode\aqe-dev` = git worktree. 2026-11-13부터 **브랜치 `dev/cloud-deploy-update`**(사용자가 `3589893`에서 만들어 push, `origin/dev/cloud-deploy-update`와 동기화됨). 이전 브랜치 `dev/cloud-deploy`도 `3589893` |
| 커밋 | `aadae59` → **`5caee5f`** AI 토론 계층 trade plan(ADR-0014) → **`3589893`** 클라우드 배포 대응(ADR-0015) → **Google 단독 운영 커밋**(ADR-0016, 이 문서를 포함한 커밋 = 브랜치 HEAD = 배포 대상, push 안 함). 미커밋 0 |
| 분리 방법 | 작업 트리를 건드리지 않고 인덱스로만 분리: 보존 패치(`git apply --cached`) + trade plan 신규 파일 8개 → 커밋 1, 나머지 전부 → 커밋 2. 공통 파일 5개(config, debate.py, DECISIONS, OPERATIONS, RESUME_DEV)는 덩어리 단위로 확인 |
| 보존 사본 | `E:\aqe-v2-dev\preserve\aqe-dev-uncommitted-20261112T130656Z\`(커밋 1 원본: `tracked.patch`, 신규 파일 8개, `SHA256SUMS`) — 커밋 후에도 유지 |
| 개발 venv | `aqe-dev\.venv` (Python 3.12.10, torch 2.15.0+cpu), `aqe-dev\.venv-ta` |
| 개발 데이터 | `E:\aqe-v2-dev\data`/`artifacts`(스냅샷), `E:\aqe-v2-dev\cloud-sim\`(이전·릴리스 리허설 대상), `E:\aqe-v2-dev\inventory\`(인벤토리·wheel 확인 결과) |

**커밋 의존 관계**: 커밋 1은 단독으로 동작한다(아래 §3, `git archive`로 따로 풀어 전체 시험). 커밋 2는 커밋 1 위에서만 의미가 있다
(공통 파일 `config/__init__.py`·`debate.py`의 덩어리가 커밋 1 내용 위에 얹힘; 클라우드 설정은 `planning.enabled: false`로 trade plan을 끈 채 사용).
main 병합 시 debate 파일 3개 add/add 충돌 → 개발 쪽 채택.

**주의(이번 세션 사고)**: 첫 인벤토리 실행(22:28:59)이 원본 `E:\aqe-v2\data\meta\`에 `aqe_meta.sqlite-shm`(32 KB)·`aqe_meta.sqlite-wal`(0 B)를
**새로 만들었다**(읽기 전용 `mode=ro`라도 WAL DB를 열면 생긴다). DB 본체(`aqe_meta.sqlite`, 최종 수정 2026-11-11 13:03)는 그대로이며 두 파일은 데이터가 없는 보조 파일이다.
원본 폴더에서 삭제하지 않았다 — 지울지는 사용자 결정(AQE 프로세스가 그 DB를 열고 있지 않을 때). 코드는 이후 원본을 SQLite로 열지 않도록(복사 후 백업) 고쳤고 재발하지 않음을 확인했다.

## 2. 이번 세션: 클라우드 대응 (`AQE 프로젝트 — Oracle + Google Cloud 배포 대응` 프롬프트)
상세 `docs/CLOUD_DEPLOYMENT.md`, 결정 ADR-0015.
| 영역 | 구현 |
|---|---|
| 배포 버전 | `python -m aqe.ops code-release-check`(깨끗한 단일 커밋 + 시험), `deploy/linux/install_release.sh <40자리 SHA> <role>`(브랜치 거부, 대상 서버 시험·사전 점검 후 current 전환, 재시작 안 함), `rollback_release.sh` |
| 설정 | `configs/cloud/` 역할 4개 + 실험 3개(`base_config`만 다름, 시험 강제). 경로 `${AQE_DATA_ROOT:-/srv/aqe/data}` 지원(설정 로더), `configs/<그룹>/` 하위 설정의 프로젝트 루트 계산 수정 |
| Linux 호환 | `.venv-ta` Scripts↔bin 자동 대응(`aqe.ops.platform`), `scripts/run_pipeline.sh` OS 중립, `.gitattributes`(sh·unit LF), 수집기 SIGTERM→정상 종료 경로, Linux 시계 안내 |
| 의존성 | `requirements/common.txt` + `linux-arm64-cpu` / `linux-x86_64-cpu` / `linux-x86_64-cuda`, `scripts/check_wheels.py`(색인 메타데이터 확인) |
| 수집기 | 품질 이벤트(호가 desync·재동기화·끊김·재시작) 추가 전용 일지 `{data_root}/quality/…/<UTC일>.jsonl` (`ingestion/collector.py`) |
| Oracle 운영 | systemd: `aqe-collector@`(KillMode=mixed, SIGINT, Restart=always, ExecStartPre 사전 점검), `aqe-health@` 5분 timer, `aqe-alert@`, `aqe-shadow`(기본 비활성), logrotate, journald 상한, `bootstrap_host.sh` |
| 데이터 | `aqe.ops.inventory`(분류·용량·기간·manifest SHA·SQLite 사본 무결성·모델 상태), `aqe.ops.transfer`(DRY-RUN 기본, SHA-256, 재개, 덮어쓰기 없음·충돌 보관, 원격 ssh/scp, 롤백=이동), `sqlite-backup/restore` |
| 모델 | `aqe.ops.release`: build → stage → verify(무결성·누수·현 서버 추론·위험 엔진·활성 모델 비교) → `--approve` 활성화 → 레지스트리 롤백 |
| Google | `deploy/google/train_pipeline.sh`(단계 재개, 잠금 평가는 `--allow-locked-evaluate`일 때만, `--shutdown-after`), `sync_from_oracle.sh`(증분 pull) |
| 전환 | `aqe.ops.cutover`: 병행 수집 비교(커버리지·호가·체결·헬스) → GO/NO-GO → 승인 기록만(수집기를 멈추지 않음) |
| 보안 | `.gitignore`에 키·`.env.*`·자격 증명 패턴, `deploy/linux/aqe.env.example`(비밀 없음) |
| 수집기 이전 추가(같은 날 후속 요구) | 전송 `--mode mirror`(Google 복제: 받은 그대로일 때만 갱신, 이전 버전 보관) · `transfer-merge`(겹친 PC/Oracle 파티션을 수집기 키로 합침, 대상 행 우선, 백업) · 저장소 소유 표시 `status/collector_owner_<sym>.json`(다른 호스트 시작 거부) · Parquet 스키마·파티션 기간 검사, 읽을 수 없는 Parquet 제외 · 수집기 전용 의존성 `requirements/linux-arm64-collector.txt` + `install_release.sh`의 `AQE_REQUIREMENTS` · Oracle `aqe-backfill.service/.timer`(선택) · 문서 §13 |

## 2b. 2026-11-13: Google Cloud 단독 운영 (`google-collector`) — 상세 `docs/CLOUD_DEPLOYMENT.md` §15, ADR-0016
| 영역 | 구현 |
|---|---|
| 역할 | `aqe.ops.platform`: `google-collector`(Linux x86-64, Binance 필수, 여유 10 GiB, 데이터 디스크 분리, 권장 RAM 1.5 GiB), `COLLECTOR_ROLES`, `/proc/meminfo` 메모리 |
| 설정·의존성 | `configs/cloud/google_collector.yaml`(Oracle 수집기 설정과 같음, 분석 참조만 `experiment_google`), `requirements/linux-x86_64-collector.txt`(ARM64 수집기 파일과 같은 고정, ML 없음) |
| 사전 점검 | 수집기 역할 공통 처리, Binance HTTP 451 = 지역 차단 문구(403 = WAF 또는 지역 차단), 메모리·스왑, 데이터 디스크 분리(WARN), `AQE_ROLE` 불일치 FAIL, CLI 역할 누락 안내 |
| 배포 | `bootstrap_host.sh google-collector`(env 파일을 역할 값으로 한 번만 생성, 디스크·메모리 안내, 학습 VM pull 키), `install_release.sh`(google-collector 기본 = 수집기 전용 환경, 요구 파일 CPU 일치 검사, `AQE_ROLE` 일치 검사, 수집기 시험 부분집합에 `tests/test_collector_host.py`), `aqe-collector@.service`가 `--role ${AQE_ROLE}` |
| **결함 수정** | `aqe` 계정이 `/usr/sbin/nologin`이라 `ssh aqe@host <명령>` 전송이 모두 실패했을 문제 → `/bin/bash` + 비밀번호 없음(키 전용) + 빈 `authorized_keys` |
| 동기화 | `deploy/google/sync_from_collector.sh`(범용, `--accept-new-hostkey`), `sync_from_oracle.sh`는 호환 래퍼, `train_pipeline.sh --sync-from`(완료·충돌 0일 때만 학습). `transfer --ssh-option` |
| 전송 정확도 | 미러의 출처 = 저장소 ID(`status/data_store.json`, 감독자가 한 번 생성, 전송 안 됨): 같은 저장소 새 주소 = 갱신, 같은 주소 다른 저장소 = 충돌. `transfer-merge`로 합친 버전은 `absorbed`로 기록 → 다음 전송에서 `SKIP_MERGED`(종전에는 매번 다시 충돌 보관) |
| 전환 | `cutover-approve --reference linux`(Oracle→Google 수집기 교체 시 `systemctl disable --now` 안내) |

## 3. 검증 결과 (Windows 11 Pro 10.0.26200, 개발 `.venv` Python 3.12.10)
| 명령 (cwd `C:\vscode\aqe-dev`) | 결과 |
|---|---|
| `.venv\Scripts\python -m pytest -q -p no:cacheprovider` (testpaths에 `local-collector/tests` 추가) | **243 passed**, exit 0, 137.5초 (기존 195 + cloud 33 + local-collector 15), skip 0 |
| local-collector 시험 원래 상태 확인 | HEAD 버전도 개발 venv에서 2개 실패/정지(설치된 `aqe`가 시험용 가짜 패키지를 가림) → 시험 픽스처를 일반 패키지로 고쳐 15 passed |
| 변이 확인(덮어쓰기 금지·활성화 승인 제거) | 해당 시험 실패로 검출, 원복 후 통과 |
| `python -m aqe.ops inventory --data-root E:\aqe-v2\data --artifact-root E:\aqe-v2\artifacts …` | exit 0, 17초. data 373개 0.808 GiB, artifacts 110개 0.255 GiB, manifest 87/87 SHA 일치, SQLite 사본 ok, 모델 12슬롯 OK |
| `scripts/check_wheels.py` × 3 플랫폼 + TA(arm64) | 모두 exit 0: 42/42, 42/42, 42/42, 85/85 WHEEL (설치 아님) |
| 실데이터 이전 리허설: `transfer --source E:\aqe-v2\data --dest E:\aqe-v2-dev\cloud-sim\oracle\data` | DRY-RUN(대상 0건) → `--execute` 361개 852 MB 53초 complete → 재실행 361 SKIP + 1 신규. artifacts 106개 247 MB complete |
| 릴리스 리허설(운영 artifact 읽기 → cloud-sim) | build 12 artifact/37 파일 → stage → verify passed(1h·24h LGBM·TCN OK, 위험 ABSTAIN, 실행 false) → 승인 없는 활성화 exit 1 → 승인 활성화 → 롤백 exit 0 |
| `python -m aqe.ops health --config configs\local.yaml`(운영 수집기, 기록 안 함) | exit 0, OK: 상태 8.5초 전, SYNCED, 피드 지연 ~8.5초, 시계 73.6 ms |
| `preflight --role oracle-collector`(이 PC, sim 경로) | exit 0, WARN(OS/arch 다름·Windows 시계 수동), Binance REST 도달·시계 -73.7 ms PASS |
| `bash -n` 배포 스크립트 6개 | 문법 OK (실행은 Linux에서만) |
| (후속) 전체 `pytest -q -p no:cacheprovider` | **249 passed**, exit 0, 121.2초, skip 0 |
| (후속) `pytest tests/test_cloud_ops.py` | **39 passed**(미러 갱신·충돌, 합치기·보류·멱등, 소유 표시, 파티션·손상 Parquet, ML 미사용 수집 경로, Windows→Oracle 전환 순서) |
| (후속) `check_wheels.py requirements/linux-arm64-collector.txt --platform linux_aarch64` | 23/23 WHEEL, exit 0 |
| (후속) 실제 수집 75초: `collect_local.py --project-root C:\vscode\aqe-dev --config configs\cloud\oracle_collector.yaml --duration-seconds 75` (AQE_DATA_ROOT=`E:\aqe-v2-dev\collector-smoke\data`) | 감독자 exit 0, Binance 시계 -78.8 ms, 호가 SYNCED, 메시지 ~2,500, raw 3개 닫힘·`.open` 0, 품질 일지 9건, checkpoint·소유 표시·final 상태, Parquet 파티션 검사 통과 |
| (릴리스 정리) 커밋 1 단독: `git archive 5caee5f`를 `E:\aqe-v2-dev\verify\c1`에 풀고 `PYTHONPATH=…\c1\src`로 전체 `pytest` | **195 passed**, exit 0, 132.2초, skip 0 (커밋 1은 testpaths = tests) |
| (릴리스 전 검증) `pip install --dry-run --only-binary=:all: --platform manylinux_2_28_aarch64 … --python-version 3.12 --abi cp312 -r requirements/linux-arm64-{collector,cpu}.txt` | 둘 다 exit 0: 수집기 전용 24개·전체 42개를 aarch64 cp312 **wheel만으로 해결**, 버전 불일치 0(마커는 이 Windows 호스트 기준으로 평가됨 → `colorama` 고정 추가) |
| (릴리스 전 검증) ML 차단 시뮬레이션(`sitecustomize`로 torch·LightGBM·sklearn·SciPy import 차단) + 수집기 시험 묶음 | 39 passed, 차단된 import 시도 0 |
| (릴리스 전 검증) ML 차단 상태 실제 수집 60초(Oracle 설정, `E:\aqe-v2-dev\collector-smoke-noml`) | 감독자 exit 0, 차단 시도 0, final, `.open` 0, raw 3개 닫힘, 메시지 ~5,100 |
| (릴리스 전 검증) 원본 무변경 | 모델·학습·평가·위험 코드와 실험·위험 설정: aadae59 대비 diff 0. `E:\aqe-v2\artifacts` 106개, `E:\aqe-v2\data`의 manifest·보고서·context·REST 봉 105개: 오늘 인벤토리 SHA-256과 전부 동일. SQLite 보조 파일 2개 그대로 |
| (릴리스 전 검증) 병합 정확도 시험 | 키가 같은 행 = 중복(동일 5·상이 5 → Oracle 행 유지), 키가 다른 겹친 구간 행 10개만 추가, 들어온 파일 내부 중복 제거, ns/us 단위 차이 정규화, 시간대 없는 키는 자동 병합 거부, PC 원본·충돌 사본 해시 불변 |
| (2026-11-13) 전체 `pytest -q -p no:cacheprovider` | **266 passed**, exit 0, 189.4초, skip 0 (기존 250 + `tests/test_collector_host.py` 15 + 역할 설정 시험의 google_collector 1) |
| (2026-11-13) 변이 확인 5종(미러 출처를 주소로만 판정 / 합친 버전 미기록 / SSH 옵션 미전달 / google-collector를 비수집 역할로 / 451 일반 문구) | 5종 모두 해당 시험 실패로 검출, 변이 없는 대조 통과 |
| (2026-11-13) ML 차단 + install_release 수집기 시험 부분집합(`-m "not slow"`) | 54 passed. 차단 시도 3건은 같은 시험 안의 학습 역할 사전 점검이 ML import를 일부러 점검한 것(그 검사 제외 시 0건) |
| (2026-11-13) `check_wheels.py requirements/linux-x86_64-collector.txt --platform linux_x86_64` | 24/24 WHEEL, exit 0 (`E:\aqe-v2-dev\verify\check_wheels_x86_64_collector.json`) |
| (2026-11-13) `pip install --dry-run --only-binary=:all: --platform manylinux_2_28_x86_64 … --python-version 3.12 --abi cp312 -r requirements/linux-x86_64-collector.txt` | exit 0, 24개 wheel만으로 해결, 버전 불일치 0, ML 0 (`…\verify\pip_dryrun_x86_64_collector.json`) |
| (2026-11-13) ML 차단 실제 수집 60초 + 40초, `configs\cloud\google_collector.yaml`, `E:\aqe-v2-dev\collector-smoke-google` | 둘 다 감독자 exit 0, 차단 시도 0, final, `.open` 0, raw 닫힘, 호가 SYNCED, `status/data_store.json` 생성(재시작에도 같은 ID) |
| (2026-11-13) 실데이터 미러 동기화 리허설(수집 저장소 → `E:\aqe-v2-dev\cloud-sim\google-train-sync\data`) | DRY-RUN 9개(대상 미생성) → 1차 9개 배치 → 수집 재실행 후 2차: 새 파일 5 배치, 자란 파티션 4 갱신(flow·mark·OI·runs index, 이전 버전 `replaced/`), 같은 내용 5 건너뜀, 충돌 0, 수신 색인에 저장소 ID |
| (2026-11-13) `preflight --role google-collector`(이 PC, smoke 경로) | exit 0: Binance REST·시계 -90.7 ms PASS, ML import 점검 없음, WARN은 Windows OS·시간 동기화 2건. `AQE_ROLE` 불일치 exit 1 |
| (2026-11-13) `bash -n` 배포 스크립트 7개 | 문법 OK, LF 확인(실행은 Linux에서만) |
미실행: ARM64/Linux 설치·시험·추론, Google CPU·GPU, 실제 SSH 전송, systemd 동작, Linux SIGTERM 실제 프로세스 경로, 클라우드에서 Binance, Spot 중단, 실제 전환.
Google 단독 운영 미실행: Google VM에서 bootstrap·install(x86-64 수집기 환경 실제 설치·시험), Google 리전에서 Binance 접속·시계, `aqe` 키 전용 SSH 로그인과 VM 간 실제 ssh/scp, 데이터 디스크 마운트 판정, VM 메모리 여유, systemd 정지 경로.
실제 LLM 호출 0, 모델 학습 0, 시뮬레이션 0, 주문 0, 클라우드 자원 생성 0, push 0.

## 4. 남은 작업 (우선순위)
1. 사용자 결정: `dev/cloud-deploy-update`의 Google 단독 운영 커밋 push(커밋 완료, push 안 함), 원본 `E:\aqe-v2\data\meta`의 보조 파일 2개 처리(삭제하지 않았음).
1b. Google 단독 운영을 택하면: 수집 리전 결정(Binance 허용 리전, §15) → 수집 VM·데이터 디스크 생성(사용자) → `bootstrap_host.sh google-collector` → `install_release.sh <SHA> google-collector` → 시작·정지 실측 → 학습 VM pull 키 등록 → `sync_from_collector.sh` → 병행 ≥ 24h·전환.
2. Oracle 서버 생성(사용자) → `bootstrap_host.sh` → `install_release.sh <SHA> oracle-collector` → 서버에서 시험·사전 점검(ARM64 실측) → 병행 수집 ≥ 24h.
3. Windows→Oracle 실제 SSH 전송(§5 명령), Oracle에서 `release-verify`로 ARM64 추론 확인.
4. TCN epoch 체크포인트·재개(구현 전 Spot VM 비권장), G01(`5m_live`/REST 공통 조회 — shadow 분석 전제).
5. 전환 판정(`cutover-compare`) → 사용자 승인 → 사용자가 Windows 수집기 정지 → 최종 증분 이전 → `transfer-merge`(겹친 구간) — `docs/CLOUD_DEPLOYMENT.md` §13.5.
6. 이전 목록: 가설·계획 채점, 실제 LLM 선정, tick import, NEWS/CROSS_MARKET.

## 5. 수집기에 아직 적용하지 않은 변경
없음(원본 코드·설정·venv 변경 0, 수집기 중지 0). 원본에 반영되면 영향이 있는 것: `local-collector/collect_local.py`(Windows 동작 동일, POSIX SIGTERM 처리 추가,
시작 시 `status/collector_owner_BTCUSDT.json`과 저장소 ID `status/data_store.json` 생성 — 이후 다른 호스트 이름으로는 같은 저장소 수집 거부),
`ingestion/collector.py`(품질 일지 파일 추가 기록 — data_root에 `quality/` 생성), `config/__init__.py`(`${VAR:-기본값}`·하위 설정 루트), `storage/metadb.py`(aadae59 표 3개).
반영 시점은 사용자가 정한다(수집기는 재시작 때 원본 `src`를 다시 import).

## 6. 다음 명령
```powershell
cd C:\vscode\aqe-dev
git status; git log --oneline -4                       # dev/cloud-deploy-update: Google 단독 운영 커밋 <- 3589893 <- 5caee5f <- aadae59, 미커밋 0
.venv\Scripts\python -m pytest -q -p no:cacheprovider tests\test_cloud_ops.py tests\test_collector_host.py local-collector\tests
.venv\Scripts\python -m aqe.ops inventory --data-root E:\aqe-v2\data --artifact-root E:\aqe-v2\artifacts --out-md E:\aqe-v2-dev\inventory\inventory_latest.md
.venv\Scripts\python -m aqe.ops transfer --source E:\aqe-v2\data --dest E:\aqe-v2-dev\cloud-sim\oracle\data --kind data   # DRY-RUN
```

## 7. 다음 세션 재개 지시문
"C:\vscode\aqe-dev(브랜치 dev/cloud-deploy-update, 3589893 위의 Google 단독 운영 커밋 = 배포 대상)에서 docs/RESUME_DEV.md, docs/CLOUD_DEPLOYMENT.md(§15),
docs/DECISIONS.md ADR-0014·0015·0016을 읽고 '남은 작업' 1번(사용자 결정) 이후를 이어가라. 원본 C:\vscode\aqe, E:\aqe-v2\data, E:\aqe-v2\artifacts는
실행 중인 수집기가 사용하므로 쓰지 말고(조사는 읽기 전용, SQLite는 복사본으로만), 개발 venv와 E:\aqe-v2-dev로만 시험하라. 클라우드 자원 생성, push,
실제 LLM 호출, 모델 재학습, 실거래 활성화는 하지 마라."
