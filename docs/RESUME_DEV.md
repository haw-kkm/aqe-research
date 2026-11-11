# RESUME_DEV — 개발 worktree 인수인계 (분석·토론 계층)

갱신: 2026-11-12 03:10 KST (2026-11-11 18:10 UTC) · 개발 폴더 전용 문서. 원본 `C:\vscode\aqe`의 BUILD_STATUS/NEXT_SESSION은 수정하지 않았다.

## 1. 위치·브랜치·격리 (현재 상태)
| 항목 | 값 |
|---|---|
| 원본(수집기 사용 중) | `C:\vscode\aqe` · `main` @ `1785160`(25b3623 + `local-collector/` + 토론 계층 **초기 초안**). 이번 세션 쓰기 0건, git status 깨끗함 |
| 실행 중 수집기 | `local-collector/collect_local.py --project-root C:\vscode\aqe` → `aqe collect` (PID 6220/32640/35508/35964, 2026-11-11 13:40 KST 시작, run `run-20261111T044005Z-29fc10ca`), data_root `E:\aqe-v2\data`. 종료 시 원본 `src`를 다시 import해 자동 재시작 |
| 개발 폴더 | `C:\vscode\aqe-dev` = git worktree, 브랜치 `dev/debate-layer-update` @ **`4304c5f`**(사용자 커밋·push = D01–D05 수정). 그 위에 **이번 세션 R01/R02 수정 미커밋**(아래 §2) |
| 브랜치 이력 | 0f5d4f0(토론 계층 v0.1) → 4304c5f(독립 검토 D01–D05) → 미커밋(재검토 R01/R02). 로컬 `dev/debate-layer`는 0f5d4f0에 멈춘 옛 이름 |
| 개발 venv | `aqe-dev\.venv` (Python 3.12.10, torch 2.15.0+cpu, 나머지 requirements.lock), `aqe-dev\.venv-ta` (TradingAgents 0.7.1 @ 9fa1196, LangGraph 1.2.20) |
| 개발 데이터 | `E:\aqe-v2-dev\data`, `E:\aqe-v2-dev\artifacts` — `scripts/make_dev_snapshot.py`로 쓰기가 끝난 파일만 복사 + 개발 루트로 REST 보충 |

**main 병합 주의**: main(1785160)의 토론 계층 초기 초안 때문에 `src/aqe/context/debate.py`, `debate_worker.py`, `src/aqe/contracts/debate.py`가
add/add 충돌한다 → **개발 브랜치 쪽 채택**. 수집기는 재시작 시 원본 `src`를 import하므로 원본 반영 시점은 사용자가 정한다(§5).

## 2. 이번 세션: 재검토(`AQE_Debate_Repair_Review_KO_4304c5f_2026-11-12.md`) R01/R02 — 미커밋
| ID | 수정 요지 |
|---|---|
| R01 부분 배치 | 가설 이력을 JSONL에서 **큐 DB**(`{data_root}/meta/aqe_meta.sqlite`)의 `hypothesis_ledger`·`hypothesis_batches`로 이동. lease 확인·원장 재검사·배치 전체·결과 outbox·job DONE이 **한 SQLite 트랜잭션**(`JobQueue.complete_with(on_commit(conn))`). 예외 시 전부 rollback. 배치마다 job_id(UNIQUE)·행 수·sha256; 행 수 불일치 → `LedgerIntegrityError`(읽기 거부); UPDATE/DELETE는 트리거로 거부; `verify_ledger`로 해시 재검사 |
| R02 완료 후 파일 실패 | 최종 assessment·DebateRecord를 같은 트랜잭션의 `context_outbox`에 저장. 파일은 커밋 뒤 outbox에서 기록(감사 파일 먼저, assessment 마지막) → `materialized_at`. 실패·중단 시 **다음 `process_one`이 LLM 재실행 없이 다시 기록**(`{"recovered": [...]}` 반환). 모든 backend 결과에 적용 |

변경 파일: `src/aqe/storage/metadb.py`(표 3개·트리거·`complete_with(conn)`·outbox 메서드), `src/aqe/context/store.py`(`materialize_outbox`),
`src/aqe/context/debate.py`(SQLite 원장·`commit_ledger(conn)`·`commit_plan`·`verify_ledger`·outbox 커밋; JSONL·잠금 파일 제거),
`src/aqe/context/worker.py`(복구 → 커밋 → 기록), `src/aqe/cli/analyze.py`(`debate_summary`에 DB 경로), `tests/test_debate_layer.py`,
`docs/DECISIONS.md`(ADR-0013), `docs/DEBATE_LAYER.md`(§5), `docs/OPERATIONS.md`(표 목록), 신규 `migrations/postgres/002_hypothesis_ledger_outbox.sql`(서버 없어 미실행), 이 파일.
API 변경: `load_ledger(db, inst)`·`apply_to_ledger(db, ...)`·`debate_summary(data_root, db, ...)`의 `db` = `sqlite_path(cfg)`; `ledger_path`·`append_ledger`·`ledger_lock` 삭제.
이전 JSONL 이력(`context/hypotheses/*.jsonl`, 개발용 대체 LLM 실행분)은 읽지도 옮기지도 않는다(`E:\aqe-v2-dev`에 남아 있음, 무시됨).

## 3. 검증 결과 (모두 개발 venv, Windows 11 Pro 10.0.26200, Python 3.12.10, torch CPU)
| 명령 (cwd `C:\vscode\aqe-dev`) | 결과 |
|---|---|
| 수정 전 재현: scratchpad `aqe_debate_commit_failure_repro.py`(재검토 부록 B 그대로) | exit 0. 부록 A와 동일: 1행만 남은 뒤 재시도 DONE·1행·already applied / 큐 DONE·원장 4행·assessment 없음·재시도 None |
| `.venv\Scripts\python -m pytest -q -p no:cacheprovider tests/test_debate_layer.py` | **41 passed**, exit 0, 71.2초 (기존 32 + R01 5 + R02 4; `.venv-ta` 실제 LangGraph worker 포함, skip 0) |
| `.venv\Scripts\python -m pytest -q -p no:cacheprovider` (전체) | **150 passed**, exit 0, 106.1초 |
| 수정 후 같은 시나리오: scratchpad `aqe_debate_commit_failure_after_fix.py`(장애 주입 지점만 새 코드의 같은 위치로) | exit 0. R01: 1행 기록 후 장애 → 남은 행 0, job PENDING → 재시도 DONE·4행·1h/24h·verify 문제 0. R02: "committed; result files pending", DONE·4행·assessment 없음·outbox 1 → 다음 실행 `{"recovered": [..]}`, backend 1회, assessment 있음, DebateRecord applied·"applied", outbox 0 |
| 재검토 원본 스크립트(부록 B)를 수정 후 실행 | exit 1, `AttributeError: append_ledger` — 장애 주입 대상 함수가 제거되어 준비 단계에서 멈춤(위 after_fix가 대체) |
| 실제 자료 스냅샷 CLI: `aqe context-enqueue --config configs/dev_debate_offline.yaml --as-of 2026-11-11T14:05:10Z` → `aqe context-worker ... --once`, 다시 `14:10:10Z`로 반복 | 모두 exit 0. 배치 2개(4행, 5행), `verify_ledger` 문제 0, outbox 2건 모두 기록 완료, 두 번째 토론이 첫 토론의 1h 가설 KEEP, assessment 파일 있음 |

증거 구분: **대체(scripted) LLM 배선 시험 + 실제 시장 자료 스냅샷 연결 + 장애 주입 시험**. 실제 LLM 호출 0건, 분석 성능 검증 없음, RESEARCH_ONLY 그대로.

## 4. 남은 작업 (우선순위)
1. 사용자 결정: 이번 미커밋 변경을 `dev/debate-layer-update`에 커밋/push할지, 이후 main 병합 시점(위 add/add 충돌 → 개발 쪽 채택).
2. G01: `5m_live`/REST 봉 공통 조회 — 토론·분석은 REST `5m`만 읽음.
3. 구조안 §9 가설 채점기(철회·만료·기간별 결과를 성숙 라벨과 대조; 탐지·철회·예측 분리). 입력은 `hypothesis_ledger`(배치·job_id·as_of 순서 확정).
4. NEWS·CROSS_MARKET 활성화: manifest가 있는 검증 import 또는 공급자(`docs/IMPORT_FORMATS.md`).
5. 실제 LLM(제공자·키·가격표·예산 결정 후) 소규모 호출로 품질·토큰·비용·지연 기록 — 현재 예산 0이라 거부됨.
6. 토론 job 생성 규칙(봉 갱신·중요 변화 임계값) — 지금은 수동 enqueue. 원장 충돌로 반영 거부된 실행은 자동 재시도하지 않음.
7. (운용 전) 가설 이력이 커지면 `load_ledger`가 전체를 읽음 — 활성 구간만 읽는 조회로 바꿀 것. outbox `last_error`가 반복되는 항목 알림.

## 5. 수집기에 아직 적용하지 않은 변경
없음(수집기 코드·설정·venv·데이터 변경 0건). 개발 브랜치 변경 중 수집기가 import하는 모듈에 닿는 것: `storage/metadb.py` —
`SQLITE_SCHEMA`에 표 3개·트리거 추가(`CREATE ... IF NOT EXISTS`, 기존 표 변경 없음), `complete_with` 콜백 인자 변경(수집 경로는 사용 안 함), outbox 메서드 추가.
원본 반영 후 수집기가 재시작되면 `E:\aqe-v2\data\meta\aqe_meta.sqlite`에 빈 표 3개가 생길 수 있다(하위 호환). `config/__init__.py`의 `DebateConfig`는 0f5d4f0부터 개발 브랜치에 있음.

## 6. 다음 명령
```powershell
cd C:\vscode\aqe-dev
git status; git log --oneline -1                       # dev/debate-layer-update @ 4304c5f + 미커밋(§2)
.venv\Scripts\python -m pytest -q -p no:cacheprovider tests\test_debate_layer.py
# 개발 스냅샷 가설 이력 확인
.venv\Scripts\python -c "from pathlib import Path; from aqe.context.debate import load_ledger, verify_ledger; from aqe.contracts.core import InstrumentKey as K; i=K(venue='binance', market_type='usdm_perpetual', instrument_id='BTCUSDT'); db=Path('E:/aqe-v2-dev/data/meta/aqe_meta.sqlite'); print(len(load_ledger(db, i)), verify_ledger(db, i))"
# 실제 자료 스냅샷 갱신(완료된 시간까지): --until-hour 를 현재 UTC 시각 - 2시간으로
.venv\Scripts\python scripts\make_dev_snapshot.py --src-data E:/aqe-v2/data --src-art E:/aqe-v2/artifacts --dst-data E:/aqe-v2-dev/data --dst-art E:/aqe-v2-dev/artifacts --experiment exp_btcusdt_ohlcv_v0_001 --until-hour <YYYY-MM-DDTHH>
```

## 7. 다음 세션 재개 지시문
"C:\vscode\aqe-dev(브랜치 dev/debate-layer-update @ 4304c5f, 재검토 R01/R02 수정 미커밋)에서 docs/RESUME_DEV.md와 docs/DEBATE_LAYER.md §4–§5를 읽고
'남은 작업' 1번(사용자 결정) 이후 항목부터 이어가라. 원본 C:\vscode\aqe와 E:\aqe-v2\data는 실행 중인 수집기가 사용하므로 쓰지 말고,
개발 venv(aqe-dev\.venv, .venv-ta)와 E:\aqe-v2-dev 스냅샷으로만 시험하라. 실제 LLM 호출·모델 재학습·모의 주문·RESEARCH_ONLY 해제는 하지 마라."
