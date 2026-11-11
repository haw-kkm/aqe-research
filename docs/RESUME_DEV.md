# RESUME_DEV — 개발 worktree 인수인계 (분석·토론 계층)

갱신: 2026-11-12 02:25 KST (2026-11-11 17:25 UTC) · 개발 폴더 전용 문서. 원본 `C:\vscode\aqe`의 BUILD_STATUS/NEXT_SESSION은 수정하지 않았다.

## 1. 위치·브랜치·격리 (현재 상태 — 과거 표기와 구분)
| 항목 | 값 |
|---|---|
| 원본(수집기 사용 중) | `C:\vscode\aqe` · `main` @ **`1785160`**(사용자 커밋·push. 25b3623 + `local-collector/` + **토론 계층 초기 초안** debate 3파일). 이번 세션 쓰기 0건, git status 깨끗함 |
| 실행 중 수집기 | `local-collector/collect_local.py --project-root C:\vscode\aqe` → `aqe collect` (PID 6220/32640/35508/35964, 2026-11-11 13:40 KST 시작, run `run-20261111T044005Z-29fc10ca`), data_root `E:\aqe-v2\data`. 종료 시 원본 `src`를 다시 import해 자동 재시작 |
| 개발 폴더 | `C:\vscode\aqe-dev` = git worktree, 브랜치 **`dev/debate-layer-update`** @ **`0f5d4f0`**(사용자 커밋·push = 2026-11-11 세션의 토론 계층 전체). 그 위에 **이번 세션 검토 반영 6파일 미커밋** |
| 옛 브랜치 | 로컬 `dev/debate-layer`도 0f5d4f0을 가리킴(원격 없음). 이전 문서의 "dev/debate-layer, 미커밋"은 0f5d4f0 커밋 전 상태 |
| 개발 venv | `aqe-dev\.venv` (Python 3.12.10, torch 2.15.0+cpu, 나머지 requirements.lock), `aqe-dev\.venv-ta` (TradingAgents 0.7.1 @ 9fa1196, LangGraph 1.2.20) |
| 개발 데이터 | `E:\aqe-v2-dev\data`, `E:\aqe-v2-dev\artifacts` — `scripts/make_dev_snapshot.py`로 쓰기가 끝난 파일만 복사 + 개발 루트로 REST 보충 |

**main 병합 주의**: main(1785160)에 토론 계층 **초기 초안**이 들어가 있어 `src/aqe/context/debate.py`, `debate_worker.py`,
`src/aqe/contracts/debate.py` 3개가 add/add 충돌한다(`git merge-tree --write-tree 1785160 0f5d4f0`로 확인). 해결은 **개발 브랜치 쪽 파일 채택**.
수집기는 재시작 시 원본 `src`를 import하므로 원본 반영 시점은 사용자가 정한다.

## 2. 작업 이력
| 세션 | 내용 | 상태 |
|---|---|---|
| 2026-11-11 | 토론 계층 v0.1(구조안 §10 1~2단계): 계약, 수치 근거 어댑터, LangGraph worker, core 검사·가설 이력·투영, 설정·backend·job 합치기, 보고서 3b, 문서 | 사용자 커밋 **0f5d4f0** |
| 2026-11-12 | 독립 검토(`AQE_Debate_Code_Review_KO_2026-11-12.md`) D01–D05 수정 + 회귀 시험 + ADR-0012 | **미커밋**(아래 파일) |

이번 세션 변경 파일(미커밋): `src/aqe/context/debate.py`, `src/aqe/context/debate_worker.py`, `src/aqe/context/worker.py`,
`src/aqe/contracts/debate.py`, `src/aqe/storage/metadb.py`, `tests/test_debate_layer.py`, `docs/DECISIONS.md`(ADR-0012), `docs/DEBATE_LAYER.md`(§4), 이 파일.

| 검토 ID | 수정 요지 |
|---|---|
| D01 늦은 결과가 철회 가설 부활 | assess는 원장 행을 **계획**만(`plan_ledger`), `commit_ledger`가 원장 잠금 아래 다시 읽어 늦은 as_of·버전 변경·같은 job 재반영을 거부. `states_at`은 as_of 순서 |
| D02 lease 잃은 worker의 원장 기록 | `process_one` → `DebateBackend.commit` → `JobQueue.complete_with`: lease 확인·원장 반영·job DONE이 한 큐 트랜잭션(공통 검증 이후). 확정 중 오류 → job 미완료(재시도 가능), 원장 무변경 |
| D03 기간 다른 REVISE | 대체 가설의 상품·기간이 이전 가설과 같아야 함 |
| D04 가설 ID 중복 | 종합 ID 유일·이전 ID 재사용 금지·이전 가설당 판정 1개·대체 가설 공유 금지; 계획 단계도 중복이면 예외; `DebateRecord.hypothesis_id_map`을 원장·주장·보고서가 공통 사용 |
| D05 병렬 초안 호출 상한 | worker 공유 슬롯 카운터(lock, 실패해도 반환 없음) + core가 `max_llm_calls < 활성 역할+2`이면 예산 예약·worker 실행 전 거부(UNSUPPORTED) |

## 3. 검증 결과 (모두 개발 venv, Windows 11 Pro 10.0.26200, Python 3.12.10, torch CPU)
| 명령 (cwd `C:\vscode\aqe-dev`) | 결과 |
|---|---|
| 검토 재현(수정 전): scratchpad `aqe_debate_boundary_repro_d01_d04.py`(검토 부록 B에서 D05 블록만 제거 — core venv에 langgraph 없음) + `d05_worker_repro.py`(core가 4역할 job 생성 → `.venv-ta` worker 실행) | exit 0. 부록 A와 동일: INVALIDATED→ACTIVE, lease 잃은 worker 행 4개, 1h→24h REVISE OK, 중복 ID OK, 상한 3에서 호출 4회 |
| `.venv\Scripts\python -m pytest -q -p no:cacheprovider tests/test_debate_layer.py` | **32 passed**, exit 0, 66.9초 (기존 17 + 신규/수정 15; `.venv-ta` 실제 LangGraph worker 통합 포함, skip 0) |
| `.venv\Scripts\python -m pytest -q -p no:cacheprovider` (전체) | **141 passed**, exit 0, 153.9초 |
| 수정 후 검토 시나리오: scratchpad `aqe_debate_boundary_after_fix.py`(같은 입력·순서, 원장 반영만 명시적 commit) | exit 0. INVALIDATED 유지·old 미반영(late arrival), lease 잃은 worker 행 0, 1h→24h REVISE INVALID·연결 0, 중복 ID INVALID·가설 행 0 |
| 수정 후 D05: `d05_worker_repro.py --max 0 1 3 8` | exit 0. 실제 모델 호출 0/1/3/6 (상한 이하) |
| 실제 자료 스냅샷, 실제 worker 경로: `aqe context-enqueue --config configs/dev_debate_offline.yaml --as-of 2026-11-11T14:00:10Z` → `aqe context-worker ... --once` | 둘 다 exit 0. 토론 OK, 대체 LLM 호출 4/8, 검사 실패 0, 원장 반영(행 5개 모두 `job_id` 기록, base_version `4:hr-4274e19e3323`), 이전 1h 가설 KEEP |

참고: 원래 검토 스크립트(부록 B)는 수정 후 D01 준비 단계에서 StopIteration으로 끝난다 — 스크립트가 `finish()`의 원장 기록 부작용에 의존했는데,
이제 기록은 commit 단계에서만 일어나기 때문이다. 같은 시나리오는 위 after_fix 스크립트와 `test_d01_*`~`test_d05_*`가 대신한다.
증거 구분: **대체(scripted) LLM 배선 시험 + 실제 시장 자료 스냅샷 연결**. 실제 LLM 호출 0건, 분석 성능 검증 없음, RESEARCH_ONLY 그대로.

## 4. 남은 작업 (우선순위)
1. 사용자 결정: 이번 미커밋 변경을 `dev/debate-layer-update`에 커밋/push할지, 이후 main 병합 시점(위 add/add 충돌 → 개발 쪽 채택; 수집기 재시작 시 새 코드 import).
2. G01: `5m_live`/REST 봉 공통 조회 — 토론·분석은 REST `5m`만 읽음.
3. 구조안 §9 가설 채점기(철회 조건 발생·만료·기간별 결과를 성숙 라벨과 대조; 탐지·철회·예측 분리). 이번에 `job_id`·`hypothesis_id_map`·as_of 순서가 확정되어 채점 입력이 안정됨.
4. NEWS·CROSS_MARKET 활성화: manifest가 있는 검증 import 또는 공급자(`docs/IMPORT_FORMATS.md`).
5. 실제 LLM(제공자·키·가격표·예산 결정 후) 소규모 호출로 품질·토큰·비용·지연 기록 — 현재 예산 0이라 거부됨.
6. 토론 job 생성 규칙(봉 갱신·중요 변화 임계값) — 지금은 수동 enqueue. 원장 충돌로 반영 거부된 실행은 자동 재시도하지 않으므로 다음 enqueue가 최신 원장으로 다시 돈다.
7. (선택) 원장 JSONL 마지막 줄이 잘린 경우(전원 단절 등) 현재는 읽기 실패로 멈춘다(fail closed) — 운용 전에 복구 절차 정의.

## 5. 수집기에 아직 적용하지 않은 변경
없음(수집기 코드·설정·venv·데이터 변경 0건). 개발 브랜치 변경 중 수집기가 import하는 모듈에 닿는 것: `storage/metadb.py`(`JobQueue.complete_with` 메서드 추가),
`config/__init__.py`(`DebateConfig`, 0f5d4f0에 포함) — 모두 추가·하위 호환이며 원본에는 아직 반영 안 함. `contracts/context.py`의 `AQE_NUMERIC` enum은 main 1785160에 이미 있음.

## 6. 다음 명령
```powershell
cd C:\vscode\aqe-dev
git status; git log --oneline -1                       # dev/debate-layer-update @ 0f5d4f0 + 미커밋 6파일·문서 3개
.venv\Scripts\python -m pytest -q -p no:cacheprovider tests\test_debate_layer.py
# 실제 자료 스냅샷 갱신(완료된 시간까지): --until-hour 를 현재 UTC 시각 - 2시간으로
.venv\Scripts\python scripts\make_dev_snapshot.py --src-data E:/aqe-v2/data --src-art E:/aqe-v2/artifacts --dst-data E:/aqe-v2-dev/data --dst-art E:/aqe-v2-dev/artifacts --experiment exp_btcusdt_ohlcv_v0_001 --until-hour <YYYY-MM-DDTHH>
```

## 7. 다음 세션 재개 지시문
"C:\vscode\aqe-dev(브랜치 dev/debate-layer-update @ 0f5d4f0, 검토 D01–D05 수정 미커밋)에서 docs/RESUME_DEV.md와 docs/DEBATE_LAYER.md §4를 읽고
'남은 작업' 1번(사용자 결정) 이후 항목부터 이어가라. 원본 C:\vscode\aqe와 E:\aqe-v2\data는 실행 중인 수집기가 사용하므로 쓰지 말고,
개발 venv(aqe-dev\.venv, .venv-ta)와 E:\aqe-v2-dev 스냅샷으로만 시험하라. 실제 LLM 호출·모델 재학습·모의 주문·RESEARCH_ONLY 해제는 하지 마라."
