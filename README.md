# AQE — BTCUSDT 연구·검토 지원 시스템 (LightGBM + 작은 인과 TCN + TradingAgents 근거 + 시간 순서 메타 + 독립 위험 코드)

설계 기준: [AQE_TradingAgents_Blueprint_KO.md](AQE_TradingAgents_Blueprint_KO.md) v1.0 (SHA-256 `1abd54b6…`, 일치 확인).
현재 상태·증거·다음 작업: [BUILD_STATUS.md](BUILD_STATUS.md), [docs/NEXT_SESSION.md](docs/NEXT_SESSION.md).

> 연구·replay·shadow 전용이다. 주문 클라이언트·거래소 서명 엔드포인트·자동 레버리지는 **존재하지 않는다**.
> 모든 출력은 `approval_required=true`, `execution_enabled=false`.

## 설치 (Windows, 검증된 경로)
```powershell
py install 3.12                       # Python 3.12.10 (Python install manager)
py -V:3.12 -m venv .venv
.venv\Scripts\python -m pip install -e . --no-deps
.venv\Scripts\python -m pip install -r requirements.lock --extra-index-url https://download.pytorch.org/whl/cu132
#   GPU가 없으면 torch는 CPU 빌드: pip install torch==2.15.0 --index-url https://download.pytorch.org/whl/cpu
# TradingAgents worker (별도 환경, 고정 SHA)
py -V:3.12 -m venv .venv-ta
.venv-ta\Scripts\python -m pip install "tradingagents @ git+https://github.com/TauricResearch/TradingAgents@9fa1196fffbcfd9de5146d2dd2869df84ab99838"
```
설정: `configs/local.yaml`(경로 `data_root`/`artifact_root`는 설정값; 기본 `E:/aqe-v2/...`), `configs/experiment.yaml`(구간·모델),
`configs/risk_rules_v1.yaml`(위험 규칙). 비밀값은 환경변수만 사용(`.env.example`).

## 실제로 동작하는 명령 (B12)
| 명령 | 동작 | 종료 코드 |
|---|---|---|
| `aqe doctor --config configs/local.yaml` | Python/패키지/GPU/경로/메모리/DB/LLM/TA worker/거래소 시계 진단 | 0, FAIL 항목 있으면 1 |
| `aqe init-db --config configs/local.yaml` | 로컬 SQLite 메타 저장소 생성; `AQE_PG_DSN` 있으면 PostgreSQL migration 적용(삭제 없음) | DSN 없으면 3(BLOCKED) |
| `aqe backfill --config configs/local.yaml [--what klines,funding,oi] [--dry-run]` | Binance 공개 REST: 5분봉 전체, 확정 funding 이력, OI 통계(최근 ~30일만) | 0 |
| `aqe collect --config configs/local.yaml [--duration-seconds N]` | WS `/public`(depth) + `/market`(aggTrade, markPrice@1s, kline_5m), L2 복원(U/u/pu), OI polling, 원본 gzip 로그·flow 요약 | 0 |
| `aqe build-dataset --config configs/experiment.yaml` | ohlcv_v0 피처, 1h/24h 라벨, 전진 fold, manifest | 0 / 4(데이터 부족) |
| `aqe train-lgbm` / `aqe train-tcn --config configs/experiment.yaml` | 보정 구간 시작 전 성숙 라벨로 최종 base 학습·등록 | 0 |
| `aqe build-oos --config configs/experiment.yaml` | fold별 재학습(정규화 포함)·시간 순서 OOS 예측 | 0 |
| `aqe train-meta` · `aqe calibrate` · `aqe evaluate` | OOS 기반 메타 → 독립 구간 온도 보정 → 잠금 시험(E0~E3, 비용 포함) | 0 / 4 |
| `aqe context-enqueue` · `aqe context-worker [--once]` | 시점 근거 묶음 생성 → lease 작업 → backend(TradingAgents/단순 LLM/fixture) | 0 / 3 |
| `aqe analyze --config ... [--as-of ISO]` | 최신 유효 입력+artifact → 기간별 앙상블 → 위험 판정 → JSON/MD 보고 | 0 / 4(전부 ABSTAIN) |
| `aqe report --config ...` | 저장된 분석을 다시 렌더링(판정 불변) | 0 |
| `aqe export-schemas --config ... --out docs/schemas` | 계약 JSON Schema 출력 | 0 |

모든 명령은 `{data_root}/runs/<run_id>.json`에 실행 ID·설정 해시·입출력·상태·종료 코드를 남긴다.
전체 학습 파이프라인: `bash scripts/run_pipeline.sh` (build-dataset → train-lgbm → train-tcn → build-oos → train-meta → calibrate → evaluate).

## 시험
```powershell
.venv\Scripts\python -m pytest -q        # T01~T14 실패 경로 + M0 fixture 경로 + 실제 TA 그래프 오프라인 격리 시험
```

## 현재 제약 (요약 — 상세는 BUILD_STATUS.md)
- 시스템 시계가 거래소 대비 약 +62초: research/shadow 분석은 R2 규칙으로 ABSTAIN. Windows 자동 시간 동기화 필요.
- PostgreSQL/TimescaleDB 미설치 → migration 미실행(BLOCKED), 로컬 SQLite 사용(ADR-0002).
- LLM API 키·비용 상한 미설정 → TradingAgents/단순 LLM 실연결 BLOCKED. 실제 그래프는 오프라인 스크립트 모델로 배선·격리만 검증.
- 뉴스·일정·소셜·교차자산·온체인 공급자 미연결(NOT_CONFIGURED). manifest가 붙은 검증된 파일 import만 지원(`docs/IMPORT_FORMATS.md`).
- 과거 캔들은 `ASSUMED_LATENCY_REPLAY`(사후 다운로드). L2/aggTrade/OI 흐름(`flow_v1`)은 지금부터 수집해야 학습 가능.

## 데이터 준비 절차
1. `aqe doctor` → 2. `aqe backfill` (5분봉 ~75만 개, 수 분) → 3. `aqe collect`를 상시 실행(flow_v1 축적) →
4. `bash scripts/run_pipeline.sh` → 5. `aqe analyze` (shadow는 `deploy/windows/shadow_loop.ps1`).
