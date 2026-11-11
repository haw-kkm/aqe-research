# BUILD_STATUS — AQE 신규 구축

기준: `AQE_TradingAgents_Blueprint_KO.md` v1.0 (SHA-256 `1abd54b6…`, 프롬프트 명시값과 일치) · 갱신: 2026-11-11 KST (세션 1 + 세션 2: 코드 리뷰 6c9ae84 대응)
상태 값: `NOT_STARTED` · `IN_PROGRESS` · `DONE` · `BLOCKED` · `PENDING_VALIDATION`. 모델 준비도는 별도 열.
실행 위치: 코드 `C:\vscode\aqe` · data_root `E:\aqe-v2\data` · artifact_root `E:\aqe-v2\artifacts` (Git 제외).

> 증거 구분: fixture 통과 = 기능 증거 / 실 API 통과 = 연결 증거 / 학습·OOS·전향 평가는 각각 별도 증거. 아래 표는 이를 섞지 않는다.

## 0. 코드 리뷰(커밋 6c9ae84) 대응 — 세션 2

상세: `docs/review/REVIEW_RESPONSE_2026-11-11.md`, 재현 출력 `docs/review/repro_before_fix.json` → `repro_after_fix.json`.

| ID | 결함 | 상태 | 증거 |
|---|---|---|---|
| F01 | 메타·보정기 학습 시각 미검사 | **DONE** | `models/chain.py` 공통 검증(온라인·평가·보정·R1), 검증 결과의 지식 시점(`established_at`) 구분; 재현 → ABSTAIN |
| F02 | 보정기–모델 결합 이력 미검사(온라인) | **DONE** | 평가와 같은 `calibration_chain_problems`; 재현 → ABSTAIN |
| F03 | 허용 host 존재 시 직접 IP 연결 허용 | **DONE** | 해석된 IP만 허용, 직접 IP·loopback·connect_ex·sendto·IOCP 경로 차단·기록, 프록시 시 UNVERIFIED; 재현 → 차단 |
| F04 | 비용 미확정 후에도 유료 호출 허용 | **DONE (오프라인 검증)** | 원자적 예약·정산, 미확정 비용 → 당일 유료 호출 차단, 이름만으로 무료 간주 금지, TA 사용량 callback·호출 상한; 재현 → 거부. 실 과금 호출로는 미검증 |
| F05 | 일정·뉴스 미확보를 이벤트 위험 PASS로 처리 | **DONE** | 필수 근거 AVAILABLE + 시점 검증 + 일정 범위 ≥ 기간일 때만 PASS; 재현 → UNKNOWN/RESEARCH_ONLY |
| F06 | 예측 캐시가 입력 버전 무시 | **DONE** | 캐시 키 = artifact + 모델 파일 해시 + 입력 내용 해시 + 피처; OOS manifest–데이터셋 일치 검사; 재현 → 재계산 1회 |
| G01 | 실시간 봉 병합·shadow 채점 | NOT_STARTED | NEXT_SESSION 작업 1·4 |
| G02 | E4/E5·numeric_context 구현 | NOT_STARTED | API 키 외에 context 피처 계약·시점 결합 데이터셋·학습기·평가기 필요 |
| G03 | 성능 해석 표기 | **DONE** | 평가 보고: '거래별 순수익률 산술합', 'MDD(log 단위)', E3 대 E1 미검정, ECE 한계 명시. 잠금 결과 보존(재실행 안 함) |

## 1. 단계 (M0–M6)

| 단계 | 상태 | 증거 (파일·명령·실제 결과) | 미해결 |
|---|---|---|---|
| M0 환경·계약·doctor·fixture 경로 | **DONE** | `aqe doctor` (doctor_latest.json: Python 3.12.10, CUDA OK, 디스크 OK, RAM 여유 0.8GB WARN, **clock +62.4s FAIL**, PG BLOCKED); `aqe analyze --config configs/fixture.yaml` → exit 4, 1h/24h 모두 ABSTAIN 보고서; `aqe report` 재렌더; 계약 JSON Schema 9종 `docs/schemas/`; `tests/test_costs_queue_safety.py::test_m0_fixture_cli_end_to_end` 통과 | — |
| M1 실제 수집·원본·DB·L2·v0 dataset | **DONE (DB는 BLOCKED)** | `aqe backfill` run-20261110T194120Z: 5분봉 **754,581개**(2019-09-08 18:00 ~ 2026-11-10, 누락 0), funding 7,856건(2019-09-10~), OI 통계 8,352건(최근 29일만: 공식 한계), 원본 gzip `raw/binance/rest/`; `aqe collect` 150초 실연결(depth 1,466 / aggTrade 1,579 / markPrice 150 / kline 274 메시지, 오더북 SYNCED, desync 0, aggTrade 누락 0); `aqe build-dataset` → `ds-exp_btcusdt_ohlcv_v0_001-2b5dd24a34`, manifest, 6 fold/기간 | PostgreSQL 미설치 → `init-db` exit 3 (SQLite 사용, ADR-0002). 수집기 장기 상시 실행·supervisor 운영 미검증 |
| M2 LightGBM 실데이터 학습·추론·평가 | **DONE** | `train-lgbm`: 1h 557,268행 / 24h 556,992행, 라벨 종료 ≤ 2025-01-01, 조기종료(시간상 뒤 15% 검증, purge); 잠금 시험 E1 아래 §5 | — |
| M3 TCN·인과성·전진 OOS | **DONE** | `train-tcn`(GPU, receptive field 253 검산), `build-oos` 2모델×2기간×6 fold, 모든 fold leak_check PASS (`oos/*.manifest.json`); T04 시험 통과 | 24h TCN은 첫 epoch 이후 과적합(조기종료로 epoch0 유지) |
| M4 TradingAgents·단순 LLM adapter·격리 | **DONE(오프라인) / 실연결 BLOCKED** | `.venv-ta`에 TradingAgents 0.7.1 @ `9fa1196…` 설치(direct_url 커밋 확인). **실제 그래프** `propagate(..., asset_type="crypto")`를 오프라인 스크립트 모델로 실행: LLM 호출 12, 데이터 도구 7건 전부 `aqe_evidence`, 네트워크 시도 0, memory_log 비활성, 결과 `INTRADAY_ASOF_VERIFIED`; 단순 LLM backend는 mock transport로 MISSING/INVALID/TIMEOUT 경로 시험 | LLM API 키·가격표·비용 상한 미설정 → 실 LLM 호출 미실행. 사용량 callback은 구현했으나 실제 제공자 응답으로 미검증(F04) |
| M5 메타·보정·위험 통합·잠금 평가 | **DONE (numeric_only)** | `train-meta`(OOS 315k행), `calibrate`(2025-01~06 독립 구간, T: 1h 1.51 / 24h 1.62), `evaluate` trial 1·2 (§5), 위험 규칙 R1–R6 + 숫자 판정 연결, replay 분석 2026-03-02 → RESEARCH_ONLY | `numeric_context` 메타 미학습(과거 시점 컨텍스트 없음, ADR-0006) |
| M6 대조 실험·shadow·배포 준비 | **IN_PROGRESS** | E0–E3 잠금 비교 완료; E4/E5 BLOCKED; `deploy/windows/*.ps1`, `deploy/aws/README.md`(미생성), `docs/OPERATIONS.md` | shadow 장기 로그 없음(PENDING); 시계 동기화 필요 |

## 2. 요구 (R01–R12)

| ID | 상태 | 근거 |
|---|---|---|
| R01 빈 프로젝트 구축 | DONE | doctor, `requirements.lock`, `requirements-ta.lock`, CLI 14개 명령 + export-schemas/context-enqueue |
| R02 공식 소스·자체 구현 구분 | DONE | `docs/THIRD_PARTY_SOURCES.md`, `THIRD_PARTY_NOTICES.md`, `docs/UPSTREAM_PATCHES.md` |
| R03 공개 데이터 실제 수집·보존 | DONE (단기 실행) | 원본 gzip(REST·WS), manifest(SHA-256), 수집 status/checkpoint |
| R04 시간 정합성 | DONE | `available_at <= as_of` 선택, provenance 4종, T01 시험 |
| R05 LightGBM 실데이터 학습 | DONE | dataset manifest, 학습 로그(run 기록·metadata), `model.txt` |
| R06 TCN 별도 학습 | DONE | T04, `model.pt`+normalizer, fold OOS |
| R07 실제 TradingAgents 연결 | PENDING_VALIDATION | 고정 SHA·어댑터·오프라인 실그래프 실행 DONE / 실 LLM 연결 BLOCKED. 실 LLM 전제 조건(F03 egress 가드, F04 예약·정산·호출 상한)은 오프라인으로만 검증됨 |
| R08 시간 순서 OOS 메타·보정 | DONE | fold manifest, purge 검사, 독립 보정, 잠금 시험 |
| R09 LLM 분리 위험 판정 | DONE | `src/aqe/risk/engine.py`, T11 |
| R10 롱·숏·여러 기간 | DONE | 1h·24h 별도 artifact·평가·보고, 롱/숏 비용 부호 T12 |
| R11 비용 포함 비교 검증 | DONE (E0–E3) / BLOCKED (E4–E5) | `evaluation/ledger.jsonl` (trial 2회 기록), 비용·×2·지연 스트레스 |
| R12 재시작·운영·인수인계 | PENDING_VALIDATION | 복구 시험(T13), OPERATIONS.md, 이 파일, NEXT_SESSION.md / 장기 운영 미검증 |

## 3. 시험 (T01–T14) — `.venv\Scripts\python -m pytest -q` → **93 passed** (2026-11-11 세션 2; 세션 1은 57)

| ID | 상태 | 시험 |
|---|---|---|
| T01 | DONE | `test_asof_contracts.py` (미래 available_at·미완성 봉·후속 수정본 차단, 미래 증거·미발표 실제치 거부) |
| T02 | DONE | `test_orderbook.py` (12) — gap·중복·역순·재연결·0수량 삭제 |
| T03 | DONE | `test_trades.py` (8) — aggressor 부호·중복 제거·gap·CVD 단위 |
| T04 | DONE | `test_models_folds.py` — 미래 입력 섭동 불변, receptive field 253, shape |
| T05 | DONE | fold 경계·purge·라벨 성숙(24h 지연), `assert_purged` |
| T06 | DONE | 자산/기간/프로필/스키마/피처순서 불일치 거부, 온라인 INVALID |
| T07 | DONE | cache key: as_of·bundle·fold·portfolio·upstream·model·prompt·config 모두 반영 |
| T08 | DONE | 네트워크 가드 차단·기록, 우회/네트워크/메모리 미차단 시 UNVERIFIED, **실제 TA 그래프 오프라인 격리 실행** |
| T09 | DONE | 환각 ID→INVALID, 무근거→PARTIAL, JSON 오류·timeout·비용 상한 0·키 없음 |
| T10 | DONE | 메타/보정 없으면 ABSTAIN, 컨텍스트만 있고 numeric_context 메타 없으면 결합 안 함 |
| T11 | DONE | 포트폴리오 UNKNOWN≠flat, LLM 'Buy'·주입 문구가 위험 실패를 못 뒤집음, 이벤트 HIGH→RESEARCH_ONLY |
| T12 | DONE | 롱·숏·수수료·불리한 체결·funding 부호/경계 |
| T13 | DONE | lease 만료 회수, stale 완료 거부, 이중 완료 금지, 원본 로그 crash 복구, 수집기 재시작 dedup |
| T14 | DONE | 주문 엔드포인트·서명 코드 부재 스캔, REST allow-list, `execution_enabled` 리터럴 False |
| 리뷰 F01–F06 | DONE | `tests/test_review_fixes.py` (36): 미래 학습 meta/calibrator, 다른 horizon·상품, 지식 시점, lineage, 직접 IP·socketpair·프록시, 예약·정산·미확정 비용, 이벤트 근거 미확보, 캐시 입력 변경 |

## 4. 모델 준비도 (작업 상태와 별도)

| 모델 | 코드 구현 | 실제 학습 | OOS 평가(잠금 시험) | 전향 평가 | 준비도 |
|---|---|---|---|---|---|
| LightGBM 1h / 24h | DONE | DONE (2019-09~2024-12 라벨) | DONE | NOT_STARTED | OOS_EVALUATED (2026-11-10 19:40Z 이후 as_of에서만; 그 이전 replay에서는 TRAINED_UNVALIDATED) |
| 작은 TCN 1h / 24h | DONE | DONE (GPU) | DONE | NOT_STARTED | OOS_EVALUATED |
| 메타(numeric_only) 1h / 24h | DONE | DONE (fold OOS 입력) | DONE | NOT_STARTED | OOS_EVALUATED |
| 온도 보정(numeric_only) | DONE | DONE (2025-01~06) | DONE (시험 ECE ≤0.05 → validated) | NOT_STARTED | OOS_EVALUATED |
| 메타(numeric_context) | DONE(경로) | BLOCKED | — | — | IMPLEMENTED_UNTRAINED |
| flow_v1 모델 | NOT_STARTED | BLOCKED(데이터 축적 필요) | — | — | — |
| TradingAgents backend | DONE | 학습 대상 아님 | E5 BLOCKED | NOT_STARTED | 오프라인 배선 검증만 |
| 단순 LLM backend | DONE | 학습 대상 아님 | E4 BLOCKED | NOT_STARTED | 미연결 |

## 5. 잠금 최종 구간 결과 (2025-07-01 ~ 2026-11-10, BTCUSDT, provenance ASSUMED_LATENCY_REPLAY)
보고서: `E:\aqe-v2\artifacts\exp_btcusdt_ohlcv_v0_001\evaluation\eval_run-20261110T200448Z-4ae88960.md` (trial 1),
`eval_run-20261110T200729Z-9e0bcbc9.md` (trial 2: 동일 artifact·설정 재실행, 비용 판정 기록용 코드 변경만).

| 기간 | E0 prior | E1 LGBM | E2 TCN | E3 메타 | E3 Δlogloss vs E0 95% CI | 고정 정책 거래별 순수익률 산술합(기본 비용) E1/E2/E3 |
|---|---|---|---|---|---|---|
| 1h | 1.0975 | 1.0637 | 1.0647 | **1.0612** | [-0.0427, -0.0303] | −3.75 / −3.82 / −1.89 (거래 2,943/3,155/1,455) |
| 24h | 1.1005 | **1.0633** | 1.0744 | 1.0648 | [-0.0451, -0.0259] | −0.03 / −0.10 / −0.02 (거래 98/157/8) |

해석(과장 금지): 확률 품질은 사전 빈도 기준선보다 유의하게 낫다(각 모델 대 E0 비교). E3 대 E1(메타 대 LGBM)의 우열은 검정하지 않았다 — 1h에서 E3 수치가 가장 낮다는 것은 유의한 우위 증거가 아니다. '산술합'은 거래별 명목 수익률의 단순 합이지 계좌 복리 누적 수익률이 아니며 MDD는 log 단위다. ECE 하나로 클래스·레짐별 신뢰도를 보증하지 않는다. 그러나 **수수료 5bp·슬리피지 2bp(편도) 반영 시 모든 고정 정책 가상 운용이 순손실**이다
(1h 총이익 ≈ +1bp/거래 vs 비용 14bp/거래). 24h에서는 메타가 LGBM 단독보다 나쁘다(개발 구간 진단에서도 동일 경향).
LGBM·TCN 손실 상관 1h 0.91 / 24h 0.73 → TCN 추가 이득은 작다(B07.3 제외 검토 대상, 아직 승인된 설계 변경 아님).
위험 규칙: `require_cost_adjusted_edge` 때문에 현재 모든 판단은 최대 RESEARCH_ONLY.

## 6. 실행한 명령과 실제 결과 (run 기록: `E:\aqe-v2\data\runs\`)
| 명령 | 결과 |
|---|---|
| `aqe doctor --config configs/local.yaml` | exit 1 (clock FAIL +62.4s; 나머지 OK/WARN/BLOCKED) |
| `aqe init-db --config configs/local.yaml` | exit 3 BLOCKED (AQE_PG_DSN 없음), SQLite 생성 |
| `aqe backfill --config configs/local.yaml` | exit 0 (위 수치) |
| `aqe collect --config configs/local.yaml --duration-seconds 150` | exit 0 (fork 실행, 위 수치) |
| `aqe collect --config configs/local.yaml --duration-seconds 330` | exit 0: public 3,230 / market 4,301 메시지, 재연결·오류 0, snapshot 1회, checkpoint 재시작 CVD 리셋 기록, 확정 kline 20:00 봉 LIVE_CAPTURE 저장(마감 후 ~0.09s 수신) — REST 확정 봉과 OHLCV·체결수 완전 일치 |
| `bash scripts/run_pipeline.sh` | build-dataset·train-lgbm·train-tcn·build-oos·train-meta OK, calibrate 1차 실패(빈 캐시 dtype 버그) → 수정 후 calibrate·evaluate OK |
| `aqe context-enqueue` + `aqe context-worker --once` (`configs/ta_offline.yaml`) | exit 0, 실제 TA 그래프 오프라인 실행 assessment `ctx-02bae1836d76` (분석에는 사용 안 됨: offline_scripted 제외) |
| `aqe analyze --config configs/local.yaml` | exit 0, 1h/24h ABSTAIN (R2: 시계 +62s) |
| `aqe analyze --config configs/replay.yaml --as-of 2026-03-02T00:00:10Z` | exit 0, 1h/24h RESEARCH_ONLY (R4 비용 판정 NOT_POSITIVE, 근거·포트폴리오 UNKNOWN) |
| `pytest -q` | 세션 1: 57 passed / 세션 2(리뷰 대응 후): 93 passed |
| 리뷰 재현 스크립트 (수정 전/후) | 수정 전 F01–F06 모두 결함 재현 → 수정 후 모두 거부·차단·재계산 (`docs/review/`) |
| `aqe analyze --config configs/replay.yaml --as-of 2026-03-02T00:00:10Z` (세션 2) | exit 0, RESEARCH_ONLY: 당시 미확정 검증은 인정 안 함(준비도 TRAINED_UNVALIDATED, 보정 검증 False) |
| `aqe analyze --config configs/local.yaml` (세션 2) | exit 0, ABSTAIN (R2: 시계 +64.0s) |
| 미실행 | PostgreSQL migration, 실 LLM 호출(TA/단순 LLM), shadow 장기 실행, ETHUSDT, flow_v1 학습, AWS 자원 생성(의도적으로 안 함) |

## 7. 미해결 조건
1. **시스템 시계 +62~64초(드리프트 중)** — Windows 자동 시간 동기화 필요(관리자). 그 전까지 research/shadow 분석은 ABSTAIN.
2. PostgreSQL/TimescaleDB 서버(또는 Docker) — 사용자 결정 필요. 이전 세션에서 다운로드 바이너리 실행이 거부된 이력이 있음.
3. LLM 공급자·모델·API 키·가격표·일일 비용 상한 — 사용자 결정 필요(`configs/local.yaml` context.llm, 환경변수).
4. 뉴스/일정/교차자산 공급자 — 미연결. 검증된 파일 import 형식만 존재.
5. flow_v1 학습용 L2·aggTrade·OI 데이터 — 수집기를 상시 실행해 수 주~수 개월 축적 필요.
6. 자유 RAM 약 0.8~1.2GB — 큰 데이터 적재 시 주의.

## 8. 다음 첫 작업
`docs/NEXT_SESSION.md` 참조. 요약: (1) 시계 동기화 후 `aqe doctor` 재확인 → (2) `deploy\windows\supervise.ps1 -Command collect` 상시 수집 →
(3) shadow 루프로 전향 로그 축적 → (4) LLM 공급자 결정 시 TA 실연결 1건 실행·비용 기록.
