# AQE 클라우드 배포 — Oracle(상시 수집) + Google(필요 시 학습) · 데이터 이전

작성 2026-11-12 · 개발 브랜치 `dev/cloud-deploy`(worktree `C:\vscode\aqe-dev`; `aadae59` → `5caee5f` trade plan → 클라우드 배포 커밋) · 결정 `docs/DECISIONS.md` ADR-0015.
**이 문서의 절차는 로컬에서 만든 도구·설정·시험까지만 실행되었다.** 클라우드 서버 생성, 원격 배포, 실제 SSH 전송, ARM64·Linux·GPU 실행은
하지 않았다(각 절 "검증 상태" 참고). 실거래 기능은 없고 추가하지 않는다: `RESEARCH_ONLY`·`approval_required=true`·`execution_enabled=false` 유지.

## 1. 현재 구조
| 구성 | 위치 | 내용 |
|---|---|---|
| 운영(수집 중) | `C:\vscode\aqe` · `main` @ `1785160` · Windows · 원본 `.venv` | `local-collector/collect_local.py`가 `aqe collect`를 감독(OS 잠금·디스크·Binance 시계 사전 점검·자식 재시작·Ctrl+C 시 flush+checkpoint). data_root `E:\aqe-v2\data`, artifact_root `E:\aqe-v2\artifacts` |
| 개발 | `C:\vscode\aqe-dev` = 같은 저장소의 git worktree | `dev/debate-layer-update`(aadae59: 토론 계층·R01/R02) → `dev/cloud-deploy`(이번). 진입·손절·익절 후보(trade plan) 작업은 미커밋 상태로 함께 있음 |
| 코드 흐름 | `src/aqe/` | 수집(ingestion) → 정규화 Parquet(`normalized/`) → 피처 `ohlcv_v0` → LightGBM·TCN → 시간순 OOS → 메타 → 보정 → 위험 규칙 R1–R6 → 보고서. 토론 계층·trade plan은 연구 출력 |
| 신규 운영 도구 | `src/aqe/ops/` (`python -m aqe.ops …`) | 인벤토리, 검증 전송, SQLite 백업/복원, 모델 릴리스, 헬스체크, 역할 사전 점검, 코드 릴리스 검증, 수집기 전환 판정 |

`aqe`(main)에만 있는 기능은 없다: main의 유일한 추가 커밋 `1785160`은 토론 계층 초기 초안(JSONL 원장)이며 개발 브랜치가 대체했다. `local-collector/`는 양쪽이 동일하다.
따라서 클라우드 대응은 개발 브랜치에서만 했고, 원본 `aqe` 수정이 필요한 사항은 없다(병합 시 debate 파일 3개 add/add 충돌 → 개발 쪽 채택).

## 2. 배포 버전 선정 — "검증된 단일 커밋"
1. 개발 PC에서 변경을 커밋(사용자) → `python -m aqe.ops code-release-check --repo . --run-tests --out release_check.json`
   - 작업 트리가 깨끗해야 함(수정·추가·미추적 파일 0) · 커밋/트리 SHA·포함 브랜치·태그·의존성 잠금 해시·시험 결과 기록.
2. 원하면 태그(예 `release/2026-11-xx`)를 붙여 push(사용자).
3. 서버는 **40자리 커밋 SHA로만** 설치(`deploy/linux/install_release.sh <sha> <role>`): 브랜치 이름을 받지 않으므로 개발 브랜치의 미검증 기능이
   자동 반영되지 않는다. Oracle과 Google은 같은 SHA를 쓰고 역할(설정·서비스)만 다르다.
4. 설치 스크립트는 그 서버에서 시험(`-m "not slow"`)과 역할 사전 점검을 통과해야 `/opt/aqe/current`를 바꾼다. 서비스 재시작은 하지 않는다(운영자가 명시적으로).

## 3. 역할과 설정
| 역할 | 환경 | 설정 | 의존성 | 하는 일 |
|---|---|---|---|---|
| windows-dev | Windows, Python 3.12 | `configs/local.yaml` 등(기존 그대로) | `requirements.lock`(CUDA torch, Windows 전용) | 개발·현 수집기 |
| oracle-collector | Ubuntu ARM64 A1(2 OCPU/12GB), 24시간 | `configs/cloud/oracle_collector.yaml` | `requirements/linux-arm64-cpu.txt` | WS depth/aggTrade/markPrice/kline + OI, 원본 저장, 호가 복원, 1분 흐름 요약 |
| oracle-shadow | 같은 서버 | `configs/cloud/oracle_shadow.yaml` | 위와 같음 | 승격된 모델로 주기적 연구 분석(기본 비활성) |
| google-cpu-train | Ubuntu x86-64, 필요 시만 | `configs/cloud/google_cpu_train.yaml` + `experiment_google.yaml` | `requirements/linux-x86_64-cpu.txt` | 데이터셋·LGBM·TCN(CPU)·OOS·메타·보정·평가·시험 |
| google-gpu-train | x86-64 + NVIDIA, 필요 시만 | `configs/cloud/google_gpu_train.yaml` + `experiment_google_gpu.yaml` | `requirements/linux-x86_64-cuda.txt` | 위 + TCN CUDA |

- 경로: `data_root: "${AQE_DATA_ROOT:-/srv/aqe/data}"`, `artifact_root: "${AQE_ARTIFACT_ROOT:-/srv/aqe/artifacts}"` — 환경변수(`/etc/aqe/aqe.env`)로 변경.
  설정 로더가 `${VAR:-기본값}`을 지원하도록 확장(값이 없고 기본값도 없으면 오류: 문자 그대로의 `${...}` 경로를 만들지 않음).
- 클라우드 실험 설정 3개는 `configs/experiment.yaml`과 **`base_config`만 다르다**(시험이 강제). 같은 experiment_id·구간·하이퍼파라미터.
  경로가 바뀌었다고 artifact가 호환된다고 보지 않는다: 승격 때 식별자·피처 스키마·계보·누수를 다시 검사한다(§7).
- 서버 설정은 수집기·Binance·시계·DB 항목이 Windows와 같고(시험), LLM 비용 상한 0·context backend none·trade plan 비활성이다.
- `.venv-ta/Scripts/python.exe`(Windows)와 `.venv-ta/bin/python`(Linux)은 서로 자동 대응(`aqe.ops.platform.resolve_venv_python`).

### 의존성 호환성 (검증 상태)
`scripts/check_wheels.py`로 각 고정 버전에 cp312 wheel이 있는지 **색인 메타데이터로** 확인했다(설치·실행 아님).
| 대상 | 결과 | torch |
|---|---|---|
| linux-arm64-cpu | 42/42 WHEEL | `torch-2.15.0-cp312-cp312-manylinux_2_28_aarch64.whl`(PyPI, CPU) |
| linux-x86_64-cpu | 42/42 WHEEL | `torch-2.15.0+cpu …manylinux_2_28_x86_64.whl`(download.pytorch.org/whl/cpu) |
| linux-x86_64-cuda | 42/42 WHEEL | `torch-2.15.0+cu132 …manylinux_2_28_x86_64.whl`(…/whl/cu132, CUDA 13.2 드라이버 필요) |
| TradingAgents venv(arm64) | 85/85 WHEEL | `tradingagents`는 git 설치라 별도 확인 필요 |
manylinux_2_28 → glibc 2.28 이상(Ubuntu 22.04/24.04 충족). Python 3.12는 Ubuntu 24.04 기본. **ARM64·Linux에서 실제 설치·import·추론은 미검증.**
기존 `requirements.lock`(Windows CUDA)은 서버에 설치하지 않는다.

## 4. 기존 데이터 조사 (2026-11-12 22:28 KST, 읽기 전용)
`python -m aqe.ops inventory --data-root E:\aqe-v2\data --artifact-root E:\aqe-v2\artifacts --out-json … --out-md …`
(결과: `E:\aqe-v2-dev\inventory\inventory_20261112.{json,md}`)
| 루트 | 파일 | 용량 | 분류 |
|---|---|---|---|
| data | 373 | 0.808 GiB | transfer 360(0.794 GiB) · live 8(기록 중) · host_local 4(status/checkpoint/lock) · snapshot 1(SQLite) |
| artifacts | 110 | 0.255 GiB | transfer 106(0.230 GiB) · regenerable 4(`pred_cache`, 0.025 GiB) |

- 형식: `.jsonl.gz` 원본 수신 142개(0.72 GiB), `.parquet` 167개(0.074 GiB), 기록 중 `.jsonl.gz.open` 3개.
- 기간: 5분봉 REST 2019-09-08 ~ 2026-11-11 02:30 · funding 2019-09-10 ~ 2026-11-10 · OI 통계 2026-10-12 ~ 2026-11-10 ·
  실시간 흐름 요약 2026-11-10 19:47 ~ (수집 중) · 원본 WS 2026-11-10 19:41 ~ (수집 중).
- manifest: `bars_…_5m.json` 87개 항목 SHA-256 **87/87 일치**(manifest의 `E:\aqe-v2\data\…` 경로를 현재 루트로 대응). manifest 파일은 바꾸지 않는다(그 해시가 데이터셋 계보).
- SQLite `meta/aqe_meta.sqlite`: 사본에서 integrity `ok`(context_jobs 1, schema_migrations 3).
- 모델: `exp_btcusdt_ohlcv_v0_001` 레지스트리 12슬롯 모두 metadata 필수 키 충족, 평가 기록 2, OOS manifest 4.
- 증가량 추정: 원본 WS ≈ 0.4 GiB/일(약 42시간 0.72 GiB) → 연 ≈ 150 GiB. Oracle 디스크 경보(§9)와 보관 계획 필요.

## 5. 데이터 이전 도구 (`python -m aqe.ops transfer`)
- 방향: Windows→Oracle(push), Windows→Google(push), Oracle→Google(Google에서 pull), Google→Oracle은 모델 릴리스(§7)로.
  한쪽만 원격(`ssh://user@host/절대경로`), 원격 쪽은 같은 코드의 `python -m aqe.ops transfer-*`를 실행한다(`/opt/aqe/bin/aqe-python`).
- **기본은 DRY-RUN**(계획만), 실제 전송은 `--execute`.
- 단계: manifest(SHA-256) → 계획(대상에 없으면 COPY, 같은 해시면 SKIP, 다르면 CONFLICT) → `_transfer/<id>/incoming/`로 복사·해시 재검증(불일치 1회 재시도)
  → 대상이 여전히 없을 때만 배치(덮어쓰기 없음) → journal·receipt·`_transfer/transfers.jsonl`(전송량 기록).
- 중복: 같은 내용은 경로가 같아도 다시 보내지 않는다. 다른 수집 실행이 만든 같은 이름(예: 같은 시간의 `flow_summaries/<day>/<hour>.parquet`)은
  **절대 덮어쓰지 않고** `_transfer/<id>/conflicts/`에 보관 → 비교·병합은 사람이 결정.
- 기록 중 파일 제외: `*.open`, 현재 파티션, 최근 15분 내 수정 파일, 오늘자 품질 일지. 호스트 전용(status/checkpoint/lock, `_transfer/`) 제외.
- SQLite: 원본을 SQLite로 열지 않는다(읽기 전용 연결도 WAL 보조 파일을 만들기 때문). DB와 기존 `-wal`을 임시 폴더로 복사(연속 2회 동일할 때까지)한 뒤 백업 API·`integrity_check`.
  대상에는 `_transfer/<id>/sqlite/…`로만 놓이고 운영 DB 경로에 자동으로 들어가지 않는다 → 필요 시 `sqlite-restore`(새 경로만).
- 모드: `--mode safe`(기본, Windows→Oracle: 절대 교체 안 함) / `--mode mirror`(Oracle→Google 복제: 지난번 받은 그대로인 파일만 새 버전으로 갱신, 이전 버전 보관). 겹친 수집기 파티션 합치기는 `transfer-merge`(§13.5).
- 중단·재개: 같은 `--transfer-id`로 다시 실행하면 검증된 incoming 파일은 다시 복사하지 않는다. 롤백: `transfer-rollback`(기본 DRY-RUN)이
  이 전송이 배치한 파일 중 그 뒤 바뀌지 않은 것만 `_transfer/<id>/rolled_back/`으로 **이동**(삭제 없음). 원본은 항상 읽기만 한다.

**실데이터 로컬 리허설**(대상 = `E:\aqe-v2-dev\cloud-sim\oracle\…`, 원격 아님):
DRY-RUN 361개 852 MB + SQLite 1 · 제외 15(기록 중 9, 호스트 전용 4, 임시 2) → 실행 361개 배치·해시 검증 53초 완료 → 재실행 361개 SKIP·새로 닫힌 1개만 추가.
artifacts 106개 247 MB 완료. 대상 인벤토리에서 manifest 87/87 경로 대응 확인. 원격 ssh/scp 경로는 가짜 실행기 시험만(실제 SSH 미실행).

### 이전 절차 (Windows PowerShell, `C:\vscode\aqe-dev`, 개발 venv — 원본 venv·폴더는 건드리지 않음)
```powershell
# 0) Oracle 서버 준비(§6) 후, 계획만
.venv\Scripts\python -m aqe.ops transfer --source E:\aqe-v2\data --dest ssh://aqe@<ORACLE_IP>/srv/aqe/data --kind data --remote-python /opt/aqe/bin/aqe-python
# 1) 실행(재실행 = 증분, 중단 시 같은 --transfer-id로 재개)
.venv\Scripts\python -m aqe.ops transfer --source E:\aqe-v2\data --dest ssh://aqe@<ORACLE_IP>/srv/aqe/data --kind data --remote-python /opt/aqe/bin/aqe-python --execute --transfer-id tx-win-oracle-001
.venv\Scripts\python -m aqe.ops transfer --source E:\aqe-v2\artifacts --dest ssh://aqe@<ORACLE_IP>/srv/aqe/artifacts --kind artifacts --remote-python /opt/aqe/bin/aqe-python --execute
# Google도 같은 방식(학습용 과거 데이터·artifact): --dest ssh://aqe@<GOOGLE_IP>/srv/aqe/data
```
Windows OpenSSH(`ssh`, `scp`)와 SSH 키(`ssh-keygen -t ed25519`)가 필요하다. 원격 결과 영수증: `/srv/aqe/data/_transfer/<id>/receipt.json`.

## 6. Oracle 서버 설치 (Ubuntu 24.04 ARM64, 운영자가 직접)
```bash
# 관리자 계정, 저장소를 임시로 받은 위치에서
sudo bash deploy/linux/bootstrap_host.sh oracle-collector      # 사용자 aqe, /opt/aqe, /srv/aqe, chrony, python3.12-venv, logrotate, journald 상한
sudo -u aqe git clone https://github.com/haw-kkm/aqe-research.git /opt/aqe/repo
sudo -u aqe bash /opt/aqe/repo/deploy/linux/install_release.sh <40자리 커밋> oracle-collector   # venv·시험·사전 점검·current 전환
sudo cp /opt/aqe/current/deploy/linux/systemd/aqe-{collector@.service,health@.service,health@.timer,alert@.service} /etc/systemd/system/
sudo systemctl daemon-reload
sudo -u aqe bash -c 'set -a; . /etc/aqe/aqe.env; set +a; cd /opt/aqe/current && .venv/bin/python -m aqe.ops preflight --config configs/cloud/oracle_collector.yaml --role oracle-collector'
sudo systemctl enable --now aqe-collector@BTCUSDT aqe-health@BTCUSDT.timer
```
- 사전 점검: Python 3.12, ARM64, 경로 쓰기·여유 공간, import, **시간 동기화(NTPSynchronized=yes)**, **Binance REST 연결·시계 오차 ≤ 1000 ms**(오프라인 생략 불가),
  같은 data_root에 다른 수집기 없음(정상 종료 후 재시작은 허용). 실패하면 서비스가 시작되지 않는다(`ExecStartPre`).
- 서비스: 부팅 시 자동 시작, `Restart=always`, 중복 실행은 OS 파일 잠금 + 상태 파일 확인으로 차단. 정지는 SIGINT(KillMode=mixed)로 감독자가 자식을 중단시켜
  flush·checkpoint 후 종료(SIGTERM도 같은 경로로 매핑, 강제 종료는 120초 뒤). 재시작 시 aggTrade id·CVD·kline은 checkpoint에서 복구, 호가창은 항상 새 스냅샷으로 재동기화.
- 호가 원본 공백: 연결 끊김·desync·재동기화·재시작이 `{data_root}/quality/binance/usdm_perpetual/BTCUSDT/<UTC일>.jsonl`에 **추가 전용으로** 기록된다(이번 추가, 종전에는 상태 파일의 최근 40건뿐).
- 헬스체크(5분): 상태 파일 신선도·피드 지연·호가 상태·디스크·시계 오차·흐름 파티션 지연·24시간 품질 이벤트 → `ops/health.jsonl`, 실패 시 `aqe-alert@`(저널 + 선택적 `/etc/aqe/alert.sh`).
- 로그: logrotate(주 단위 12개, 압축) · journald 500 MB 상한. 원본 시장 데이터는 회전·삭제 대상이 아니다.
- shadow 분석(`aqe-shadow.service/.timer`)은 모델 승격 후에만 켠다. G01(실시간 `5m_live` 봉 병합) 전이라 REST klines를 먼저 갱신한다.
- 방화벽: 수집기는 나가는 연결만 필요. 보안 목록·`ufw`에서 인바운드는 SSH(22, 본인 IP)만 허용.

## 7. Google 학습 서버 (필요할 때만)
```bash
sudo bash deploy/linux/bootstrap_host.sh google-cpu-train        # GPU면 google-gpu-train (NVIDIA 드라이버·nvidia-smi 먼저 확인)
sudo -u aqe git clone https://github.com/haw-kkm/aqe-research.git /opt/aqe/repo
sudo -u aqe bash /opt/aqe/repo/deploy/linux/install_release.sh <같은 커밋> google-cpu-train
sudo -u aqe bash /opt/aqe/current/deploy/google/sync_from_oracle.sh aqe@<ORACLE_IP>              # 계획(미러 모드, §13.6)
sudo -u aqe bash /opt/aqe/current/deploy/google/sync_from_oracle.sh aqe@<ORACLE_IP> --execute    # 증분 가져오기
sudo -u aqe bash /opt/aqe/current/deploy/google/train_pipeline.sh --role google-cpu-train --shutdown-after
```
- 단계: build-dataset → train-lgbm → train-tcn → build-oos → train-meta → calibrate. **evaluate(잠금 시험 구간, 시도 번호 증가)는 `--allow-locked-evaluate`일 때만.**
- 재개: 같은 `--run-id`로 다시 실행하면 같은 커밋·설정 해시로 끝난 단계는 건너뛰고, 한 단계라도 다시 돌면 뒤 단계는 모두 다시 돈다.
- **단계 내부 체크포인트는 없다**(TCN은 최적 epoch를 메모리에만 보관) → 중단된 단계는 처음부터. 따라서 Spot/선점형 VM은 epoch 체크포인트를 구현·시험하기 전에는 권하지 않는다.
- 로그·결과: `/srv/aqe/data/logs/pipeline/<run>.log`, `/srv/aqe/data/pipeline/<run>/`, artifact는 `/srv/aqe/artifacts` — 영구 디스크에 둔다(VM 정지에도 유지).
- `--shutdown-after`: 끝나면(실패 포함) 2분 뒤 `shutdown -h` → VM이 STOPPED(컴퓨트 과금 중지, 디스크 과금은 유지).
- 학습 완료 ≠ 배포. 이 작업에서 기존 모델을 재학습하지 않았다.

## 8. 모델 학습 결과 → Oracle 배포·롤백 (`python -m aqe.ops release-*`)
1. Google: `release-build --artifact-root /srv/aqe/artifacts --experiment <id> --out /srv/aqe/releases` → 레지스트리가 가리키는 artifact·OOS·평가 기록 + `release.json`(파일별 SHA-256, 코드 커밋, 빌드 호스트).
2. 전송: `transfer --kind release --source /srv/aqe/releases/<rid> --dest ssh://aqe@<ORACLE>/srv/aqe/incoming_releases/<rid> --execute`.
3. Oracle: `release-stage --received … --artifact-root /srv/aqe/artifacts` → `_releases/<rid>/`(해시 재검증, 운영 레지스트리 불변).
4. Oracle: `release-verify --artifact-root … --release-id <rid> --experiment-config configs/cloud/experiment_oracle.yaml [--fixture-bars …]`
   - 무결성, metadata 필수 키, OOS fold 누수(학습 라벨 끝 < OOS 시작), **그 서버에서 실제 추론**(`predict_horizon`: 식별자·피처 스키마·계보·라벨 컷오프 검사 포함) + 위험 엔진,
     현재 활성 모델 대비 바뀐 슬롯·평가 기록 비교 → `checks.json`. 통과 = "이 서버에서 일관되고 적재 가능", 수익성·우위 증거 아님.
5. `release-activate --approve <rid>`: 통과한 같은 `release.json`에 대해서만. 없는 artifact만 복사(같은 id·다른 내용이면 중단), 이전 레지스트리를 `registry_history/`에 백업 후 교체.
6. 롤백: `release-rollback --backup <파일명> --approve <파일명>` — 현재 레지스트리를 먼저 백업하고 복원. artifact는 지우지 않는다.

**리허설**(로컬, x86-64 Windows): 운영 artifact(읽기)로 릴리스 생성 → 시뮬레이션 Oracle에 stage → verify 통과(1h·24h LGBM·TCN 추론 OK, 위험 `ABSTAIN`, 실행 false)
→ 승인 없는 활성화 거부 → 승인 활성화 → 롤백 성공. **x86에서 학습한 모델의 ARM64 적재·추론은 미검증**: LightGBM 텍스트 모델과 torch state_dict는 형식상 아키텍처
독립이지만, Oracle에서 4단계를 실제로 통과해야 한다.

## 9. 백업·복구·장애 대응
- 원본 백업: Windows 원본은 이전 후에도 그대로 둔다(자동 삭제 없음). Oracle은 주기적으로 Google 또는 별도 저장소로 `transfer`(증분) — 원본 WS `.jsonl.gz`가 핵심.
- SQLite: `python -m aqe.ops sqlite-backup --db /srv/aqe/data/meta/aqe_meta.sqlite --out /srv/aqe/backup/meta-<날짜>.sqlite`(운영 호스트에서도 기본은 복사 방식) / 복원은 `sqlite-restore`(새 경로).
- 장애 대응표
| 증상 | 확인 | 조치 |
|---|---|---|
| 헬스 CRITICAL: 상태 파일 정체 | `systemctl status aqe-collector@BTCUSDT`, `journalctl -u … -n 200` | 서비스가 자동 재시작; 반복되면 `quality/<일>.jsonl`·로그 확인 |
| 피드 지연·호가 DESYNC 반복 | 품질 일지의 `BOOK_*`, `WS_DISCONNECTED` | 네트워크·Binance 상태 확인. 공백은 기록되어 있으므로 데이터를 꾸미지 않음 |
| 시계 오차 경고 | `timedatectl status`, `chronyc tracking` | chrony 재동기화. 1초 초과면 사전 점검이 시작을 막음 |
| 디스크 부족(WARN 20 GiB / CRITICAL 5 GiB) | `df -h /srv/aqe` | 오래된 원본을 Google/백업으로 **이전 확인 후** 수동 정리(도구는 삭제하지 않음) |
| 새 코드 문제 | `/opt/aqe/releases/installed.jsonl` | `deploy/linux/rollback_release.sh <이전 커밋>` 후 서비스 재시작 |
| 새 모델 문제 | `artifacts/<exp>/active_release.json` | `release-rollback --approve …` |

## 10. Windows 수집기 → Oracle 전환 (사용자 승인 필수)
상세 순서는 §13.5(최종 증분 이전·겹친 구간 합치기 포함).
1. Oracle 수집기를 **별도 data_root**에서 시작(Windows 수집기는 계속 실행). 최소 24시간 병행.
2. Windows 원본을 Oracle로 이전(§5). 같은 시간대 파티션은 충돌 보관소로 가며 덮어쓰지 않는다.
3. 비교·판정: 두 루트를 한 곳에 둔 뒤(예: Oracle 데이터의 일부를 PC로 transfer)
   `python -m aqe.ops cutover-compare --ref-root <PC data> --new-root <Oracle data 사본> --start … --end … --health <ops/health.jsonl> --out cutover.json`
   → 분 단위 커버리지 ≥ 99%, 호가 유효 ≥ 98%, 체결 수 차이 ≤ 1%, 창 안 CRITICAL 헬스 0건일 때만 GO.
4. `python -m aqe.ops cutover-approve --report cutover.json --approve <report_id> --migration-receipt <완료된 receipt.json>` → `cutover_approved.json`만 기록.
5. **그 뒤 사용자가** Windows 수집기 창에서 Ctrl+C(flush 후 종료). 도구는 어떤 수집기도 멈추거나 시작하지 않는다. Windows 데이터는 백업으로 보존.

## 11. 최소 비용 운영·보안
- Oracle: Always Free 한도(A1 합계 4 OCPU/24 GB, 블록 스토리지 합계 200 GB) 안에서 2 OCPU/12 GB 1대. 유료 업그레이드·추가 볼륨을 만들지 않는다. 디스크 경보로 200 GB 초과 전에 정리 계획.
- Google: 학습 때만 VM 시작 → `--shutdown-after`로 자동 정지. 예산 알림(콘솔 Billing → Budgets, 예: 크레딧의 25/50/90%)을 사용자가 설정. GPU는 필요한 실험에만, 끝나면 정지 확인(`gcloud compute instances list`).
  디스크(영구)는 과금되므로 불필요한 스냅샷·디스크는 정기 점검.
- 전송량: 모든 전송이 `_transfer/transfers.jsonl`에 바이트 수로 남는다. 클라우드 간 송신 요금은 제공자 요금표를 확인.
- LLM: 설정의 `max_cost_usd_per_day: 0.0` 유지(유료 호출 거부). 서버 설정도 동일.
- 인증·비밀: SSH 키만(비밀번호 로그인 끄기), 비밀값은 `/etc/aqe/secrets.env`(0600)·환경변수, Git 금지(`.gitignore`에 키·`.env.*`·자격 증명 패턴 추가).
- 포트: 인바운드는 SSH만. 수집기·학습은 외부에 포트를 열지 않는다.
- 월 1회 점검: Oracle 사용량(Free 표시 유지), Google 청구서·크레딧 잔액, 디스크·스냅샷 목록, `transfers.jsonl` 전송량.

## 12. 실행하지 못한 것 (완료로 보고하지 않음)
ARM64·Linux에서 설치/시험/추론, Google CPU·GPU 실행, 실제 SSH 전송, systemd 유닛 동작, Linux SIGTERM 경로의 실제 프로세스 시험,
클라우드에서 Binance 연결·시계, Spot 중단 복구, 실제 전환. 이들은 위 절차의 각 단계에서 서버별로 확인해야 한다.

## 13. 수집기 클라우드 이전 상세 (2026-11-12 추가 요구 반영)
### 13.1 현재 수집기가 저장하는 정확한 위치 (원본 읽기 전용 확인, main `1785160`)
설정 연결: `local-collector\start_collect.ps1`(기본 `-Config configs\local.yaml`) → `collect_local.py --config configs\local.yaml`
→ `load_local_config` → `paths.data_root: "E:/aqe-v2/data"` → 자식 `aqe collect --config C:\vscode\aqe\configs\local.yaml --instrument BTCUSDT`.
| 경로(`E:\aqe-v2\data\` 아래) | 쓰는 코드 | 방식 |
|---|---|---|
| `raw\binance\ws\{public,market,rest}\{group}_BTCUSDT.<UTC ts>.jsonl.gz` | `storage.RawLogWriter` | 쓰는 중에는 `.jsonl.gz.open`, 60분/64 MB마다 닫으며 이름 변경. 비정상 종료로 남은 `.open`은 다음 시작 때 `.recovered.jsonl.gz` |
| `normalized\flow_summaries\binance\usdm_perpetual\BTCUSDT\<UTC일>\<시>.parquet` | `collector.flow_tick/flush` | 1분 요약, 키 `(instrument_id, interval_start)` upsert, 임시 파일 후 원자 교체 |
| `normalized\mark_funding_estimates\…\<UTC일>\<시>.parquet` | markPrice | 키 `(instrument_id, source_id)` |
| `normalized\oi_observations\…\<UTC일>.parquet` | OI 폴링 | 키 `(instrument_id, received_at)` |
| `normalized\bars\…\BTCUSDT\5m_live\<YYYY-MM>.parquet` | kline 스트림(확정 봉) | `write_bars` 월 파티션 |
| `status\collector_BTCUSDT.json`, `…checkpoint.json` | 수집기 | 상태(15초)·체크포인트(aggTrade id·CVD·kline) |
| `status\local_collect_BTCUSDT.lock`, `logs\local_collect\collect_BTCUSDT_<ts>.log` | 감독자 | OS 잠금·자식 로그 |
| `runs\run-*.json`, `runs\runs_index.jsonl` | `run_context` | 실행 기록 |
REST 과거 데이터(`normalized\bars\…\5m\`, `funding\`, `oi_hist\`, `raw\binance\rest\`)는 `aqe backfill`이, `meta\aqe_meta.sqlite`는 context worker·init-db가 쓴다 — 수집기는 SQLite를 쓰지 않는다.
**수집기 코드는 main과 dev가 커밋 기준 동일**(`git diff 1785160 HEAD -- src/aqe/ingestion local-collector …` = 설정 1줄 외 없음). 이번 개발 쪽 미커밋 변경:
`collector.py`(품질 일지), `collect_local.py`(POSIX SIGTERM, Linux 시계 안내, 저장소 소유 표시), `config/__init__.py`(`${VAR:-기본값}`).

### 13.2 Oracle에 저장될 구조 (`/srv/aqe/data`, 같은 레이아웃)
```
/srv/aqe/data/
  raw/binance/ws/{public,market,rest}/…jsonl.gz      # Oracle 수집 원본 + Windows에서 옮긴 원본(이름에 시각 → 충돌 없음)
  raw/binance/rest/                                   # REST backfill 원본
  normalized/{flow_summaries,mark_funding_estimates,oi_observations,bars,funding,oi_hist}/…
  quality/binance/usdm_perpetual/BTCUSDT/<UTC일>.jsonl # 신규: 호가 공백·끊김·재시작 기록(추가 전용)
  manifests/  context/  runs/  reports/  logs/  ops/health.jsonl
  status/                                             # Oracle 전용(상태·checkpoint·lock·collector_owner_BTCUSDT.json) — 전송 제외
  _transfer/<id>/{incoming,conflicts,replaced,merge_backup,sqlite,receipt.json,journal.jsonl}, transfers.jsonl, received_index.json
/srv/aqe/artifacts/<experiment>/{registry.json,artifacts/,oos/,evaluation/,registry_history/,active_release.json}, _releases/
```
실측(로컬, Oracle 설정·개발 코드로 75초 실제 수집, data_root=`E:\aqe-v2-dev\collector-smoke\data`): 위 구조대로 raw 3개(모두 닫힘), flow·mark·OI 파티션 3개(파티션 기간 검사 통과),
품질 일지 9건, checkpoint·final 상태·소유 표시 생성, 감독자 exit 0.

### 13.3 기존 데이터 이전과 스냅샷 정책
| 파일 | 일관성 확보 | 검증 |
|---|---|---|
| 닫힌 raw `.jsonl.gz`, 지난 파티션, manifest, 기록 | 그대로(15분 이상 안 바뀐 완료 파일) | SHA-256(전송 후 재계산), Parquet 읽기·스키마·파티션 기간 |
| 쓰는 중 raw `.jsonl.gz.open` | **보내지 않음**: gzip 스트림 중간 복사는 일관된 파일이 아니다. 회전(≤60분) 또는 PC 수집기 종료 시 닫힌 뒤 다음 증분 전송 | — |
| 현재 시간·일·월 파티션 | 원자 교체로 쓰이므로 매 순간 완전한 파일이지만 곧 바뀜 → 안정화 전에는 제외(safe), 미러 모드는 갱신으로 처리 | SHA-256 + Parquet 스키마 |
| SQLite `meta\aqe_meta.sqlite` | 원본을 열지 않고 DB+`-wal`을 임시 폴더로 복사(연속 2회 동일)한 뒤 백업 API → `integrity_check` | 스냅샷 SHA-256, 대상의 `_transfer/<id>/sqlite/`에만 놓임 |
| 읽을 수 없는 Parquet | 전송 제외(`corrupt`) | 보고 |
실제 리허설 결과는 §5(361개 852 MB complete, 재실행 361 SKIP). 원본 `E:\aqe-v2`는 이전·백업 검증 완료 후에도 도구가 지우지 않는다.

### 13.4 Oracle 실행 명령
```bash
# 설치(§6) 후
sudo systemctl enable --now aqe-collector@BTCUSDT aqe-health@BTCUSDT.timer     # 24시간, 부팅 시 시작, 비정상 종료 시 30초 후 재시작
sudo systemctl status aqe-collector@BTCUSDT ; journalctl -u aqe-collector@BTCUSDT -n 100
sudo -u aqe /opt/aqe/bin/aqe-python /opt/aqe/current/local-collector/collect_local.py --project-root /opt/aqe/current \
     --config configs/cloud/oracle_collector.yaml --status                     # 상태(네트워크 호출 없음)
sudo -u aqe bash -c 'set -a; . /etc/aqe/aqe.env; set +a; cd /opt/aqe/current && .venv/bin/python -m aqe.ops health --config configs/cloud/oracle_collector.yaml'
sudo systemctl enable --now aqe-backfill.timer        # (선택) 매일 00:20 UTC REST klines·funding·OI → Google 학습용 5분봉
```
ML 라이브러리 설치가 ARM에서 문제면 수집기 전용 환경: `AQE_REQUIREMENTS=requirements/linux-arm64-collector.txt install_release.sh <sha> oracle-collector`
(torch·LightGBM·sklearn·SciPy 없음, 수집 경로가 이들을 import하지 않음을 시험으로 확인, aarch64 wheel 23/23 메타데이터 확인).

### 13.5 동시 기록 방지와 전환 절차
중복 방지 장치: (1) 같은 머신 — OS 파일 잠금 `status/local_collect_<sym>.lock` + 상태 파일 신선도 검사(기존), (2) 같은 저장소에 다른 호스트 — **신규** 소유 표시
`status/collector_owner_<sym>.json`(처음 수집한 호스트 이름; 다른 호스트면 시작 거부, 전송 대상 아님), (3) systemd 단일 인스턴스 + 사전 점검(신선한 비최종 상태면 FAIL),
(4) 설계상 PC는 `E:\aqe-v2\data`, Oracle은 `/srv/aqe/data` — 서로 다른 저장소. 전송은 대상 파일을 덮어쓰지 않는다.
전환 순서(각 단계 사용자 확인):
1. Oracle 설치·사전 점검 통과 → Oracle 수집기 시작(PC 수집기 계속). 2. PC 과거 데이터 초기 이전(safe, `--execute`) — 겹치는 시간대 파티션은 충돌 보관.
3. ≥ 24시간 병행 → `cutover-compare`(커버리지·호가·체결·헬스) → GO 확인. 4. **사용자 승인 후** PC 수집기 창에서 Ctrl+C(flush·`.open` 닫힘).
5. 15분 뒤 최종 증분 이전(같은 명령 재실행: 새로 닫힌 파일만 이동, 이미 있는 것은 SKIP) → receipt `complete: true` 확인.
6. 겹친 구간 합치기: `python -m aqe.ops transfer-merge --root /srv/aqe/data --transfer-id <id>`(DRY-RUN) → `--execute` — 수집기 키 기준으로 Oracle에 없는 행만 추가,
   기존 Oracle 행은 그대로, 합치기 전 파일은 `merge_backup/`, 쓰는 중인 파일은 보류. 7. `cutover-approve`로 결정 기록. 8. Windows `E:\aqe-v2`는 백업으로 보존(삭제하지 않음).

### 13.6 Google 학습 서버와의 연동
- Google VM이 켜질 때 `deploy/google/sync_from_oracle.sh aqe@<ORACLE> [--execute]` → `transfer --mode mirror`(Oracle→Google pull).
  새 파일은 복사, 같은 내용은 건너뜀, **Oracle에서 자란 파티션은 Google이 지난번에 받은 그대로일 때만 갱신**(이전 버전 `replaced/` 보관), Google에서 바뀐 파일은 충돌로 보존.
  수신 이력 `/srv/aqe/data/_transfer/received_index.json`. Oracle은 Google이 꺼져 있어도 계속 수집한다(Oracle 쪽 작업 없음).
- 학습용 5분봉: Oracle `aqe-backfill.timer`(선택)가 REST 봉을 Oracle 저장소에 갱신 → 미러 동기화로 Google에 반영. (또는 Google에서 직접 `aqe backfill`.)
- 동기화 후 `python -m aqe.ops inventory --data-root /srv/aqe/data`로 manifest·Parquet·파티션 검사 → `train_pipeline.sh`(§7).

### 13.7 실제 서버 배포 전 필요한 작업
Oracle 인스턴스·블록 볼륨 생성(사용자, Always Free 한도) · SSH 키 · `bootstrap_host.sh` · 커밋 확정(현재 미커밋) · Oracle에서 `install_release.sh`로 ARM64 설치·시험·사전 점검(Binance·시계) 실측 ·
systemd 실제 시작/정지(SIGINT 경로) 확인 · Windows→Oracle 실제 SSH 전송 시험 · 병행 수집 ≥ 24h · 디스크 보관 계획(원본 ≈ 0.4 GiB/일).

## 14. 릴리스 전 최종 검증 (2026-11-12, 이 PC에서 실행한 것만)
| 항목 | 결과 | 남은 실측 |
|---|---|---|
| ARM64 수집기 전용 설치 가능성 | pip dry-run(`--platform manylinux_2_28_aarch64 --python-version 3.12 --abi cp312 --only-binary=:all:`)으로 24개 패키지가 wheel만으로 해결, 버전 고정과 일치. 전체(torch 포함) 42개도 동일 | Oracle에서 실제 `pip install`·import |
| torch 없는 실시간 수집 | torch·LightGBM·sklearn·SciPy import를 막은 상태로 수집기 시험 39개 통과, 실제 60초 수집 정상 종료(차단 시도 0건) | ARM64에서 같은 실행 |
| systemd SIGTERM | SIGTERM→KeyboardInterrupt 매핑(감독자·자식) 단위 시험, 유닛 KillMode=mixed/KillSignal=SIGINT/TimeoutStopSec=120. Windows는 외부 SIGTERM을 전달하지 못해 실제 경로 **미실행** | Oracle에서 `systemctl stop` 후 `final: true`·`.open` 0·checkpoint 확인 |
| PC 상태·소유 표시 이전 | `status/`는 host_local → 전송 대상 아님(시험·실데이터 리허설에서 제외 확인), Oracle 소유 표시는 그대로 유지(시험) | — |
| 병합 시 원본 보존 | PC 원본·충돌 보관 사본 SHA-256 불변, 합치기 전 대상 파일은 `merge_backup/`, 쓰는 중이면 보류(시험) | 실제 겹침 데이터로 DRY-RUN 먼저 |
| 중복 행과 겹친 구간 구분 | 수집기 키 기준: 같은 키 = 중복(대상 행 유지, 동일/상이 건수 보고), 다른 키 = 새 행 추가, 파일 내부 중복 제거, 시간 단위(ns/us) 정규화, 시간대 없는 키는 자동 병합 거부(시험). OI는 키가 수신 시각이라 두 호스트의 관측이 모두 남는다(서로 다른 관측) | — |
| 기존 모델·평가 무변경 | 모델·학습·평가·위험 코드·실험 설정 diff 0, 원본 artifacts 106개·기록 105개 해시 동일 | — |
