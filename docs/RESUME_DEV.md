# RESUME_DEV — 개발 worktree 인수인계 (분석·토론 계층 + 진입·손절·익절 후보)

갱신: 2026-11-12 05:00 KST (2026-11-11 20:00 UTC) · 개발 폴더 전용 문서. 원본 `C:\vscode\aqe`의 BUILD_STATUS/NEXT_SESSION은 수정하지 않았다.

## 1. 위치·브랜치·격리 (현재 상태)
| 항목 | 값 |
|---|---|
| 원본(수집기 사용 중) | `C:\vscode\aqe` · `main` @ `1785160`(토론 계층 **초기 초안** 포함). 이번 세션 쓰기 0건, git status 깨끗함 |
| 실행 중 수집기 | `local-collector/collect_local.py --project-root C:\vscode\aqe` → `aqe collect` (PID 6220/32640/35508/35964, 2026-11-11 13:40 KST 시작, run `run-20261111T044005Z-29fc10ca`), data_root `E:\aqe-v2\data`. 종료 시 원본 `src`를 다시 import해 자동 재시작 |
| 개발 폴더 | `C:\vscode\aqe-dev` = git worktree, 브랜치 `dev/debate-layer-update` @ **`aadae59`**(사용자 커밋·push = 재검토 R01/R02 수정). 그 위에 **이번 세션 진입·손절·익절 후보 작업 미커밋**(§2) |
| 브랜치 이력 | 0f5d4f0(토론 계층 v0.1) → 4304c5f(검토 D01–D05) → aadae59(재검토 R01/R02) → 미커밋(trade plan). 로컬 `dev/debate-layer`는 0f5d4f0에 멈춘 옛 이름 |
| 개발 venv | `aqe-dev\.venv` (Python 3.12.10, torch 2.15.0+cpu), `aqe-dev\.venv-ta` (TradingAgents 0.7.1 @ 9fa1196, LangGraph 1.2.20) |
| 개발 데이터 | `E:\aqe-v2-dev\data`, `E:\aqe-v2-dev\artifacts` — 쓰기가 끝난 파일 복사본 + 개발 루트 REST 보충(5m 봉은 2026-11-11 15:15Z까지, flow 요약은 14:00Z까지). **`make_dev_snapshot.py`를 다시 돌리면 보충된 5m 봉 파티션을 원본 사본으로 덮어쓴다**(이번에는 실행하지 않음) |

**main 병합 주의**: main(1785160)의 토론 계층 초기 초안 때문에 `src/aqe/context/debate.py`, `debate_worker.py`, `src/aqe/contracts/debate.py`가
add/add 충돌한다 → **개발 브랜치 쪽 채택**. 수집기는 재시작 시 원본 `src`를 import하므로 원본 반영 시점은 사용자가 정한다(§5).

## 2. 이번 세션: 진입·손절·익절 후보와 검토안 (`AQE_Claude_Trade_Plan_Prompt_KO_2026-11-12.md`) — 미커밋
상세 `docs/TRADE_PLAN.md`, 결정 ADR-0014. 기본 **비활성**(`planning.enabled: false`), 개발 설정 `configs/dev_trade_plan_offline.yaml`에서만 켬.

| 단계 | 구현 |
|---|---|
| 계약 | `contracts/plan.py`: PlanCandidate·PlanCandidateSet(내용 해시 ID), PlanSelection(가격 필드 없음), PlanSelectionRecord, TradePlan(LONG/SHORT은 REVIEW_CANDIDATE + 실제 LLM 선택 ACCEPTED + 검사 전부 PASS + 진입 조건 전부 MET일 때만, `approval_required=True`·`execution_enabled=False` 고정) |
| 후보 계산 | `planning/candidates.py` `swing_atr_v0`(순수): 확정 봉만, 완전한 상위 시간봉만, pivot은 확인 봉 가용 뒤(`confirmed_at`), 유지된 지지/저항, ATR 완충, 유지된 반대편 swing을 익절로, tick import 있으면 불리한 쪽 반올림 후 재검사, 기존 `trade_return`으로 보수적 진입가 기준 비용 후 보상·손실·RR(funding 미포함), 자료 없음·STALE·가격대 없음 → 후보 없음 + 사유 |
| 토론 연결 | `EvidenceBundle.plan_candidates`(있을 때만 해시) → CHART·FLOW·CRITIC 공유 문맥, SYNTHESIS `plan_selection`(추가 호출 없음). `planning/selection.py` 코드 검사, `DebateRecord.plan_snapshot_id/plan_selections`, 커밋 트랜잭션 안에서 최종화(원장 미반영 → NOT_COMMITTED). worker는 형식 보완 전 원문(`raw_before_fix`)을 남기고, scripted 모델에 선택 모드 추가 |
| 최종 계획·보고서 | `planning/plan.py`: 커밋된 선택 + 같은 스냅샷(ID 대조) + 기존 위험 판정 → 기간별 TradePlan. `aqe analyze` JSON `trade_plans`/`planning`, Markdown `4b` 절, `aqe report` 재렌더 |
| 근거 정정 | `context/evidence.flow_summary_item`: 수집기 열 이름(`depth_bid_10bps_quote` 등)으로 수정(이전에는 `bid_depth_10bps_quote`를 찾아 깊이가 빠짐), 호가는 `book_valid` 행만, 체결은 aggTrade 누락 없는 구간만, as_of 기준 시간 파티션, 10분 넘으면 STALE |

변경 파일: 신규 `src/aqe/contracts/plan.py`, `src/aqe/planning/{__init__,candidates,selection,plan}.py`, `tests/test_trade_plan.py`(45개),
`configs/dev_trade_plan_offline.yaml`, `docs/TRADE_PLAN.md` / 수정 `src/aqe/config/__init__.py`(PlanningConfig), `src/aqe/contracts/context.py`
(optional `plan_candidates` + 해시), `src/aqe/contracts/debate.py`(`plan_selection`, `plan_snapshot_id`, `plan_selections`), `src/aqe/context/evidence.py`,
`src/aqe/context/debate.py`, `src/aqe/context/debate_worker.py`, `src/aqe/cli/analyze.py`, `src/aqe/reporting/report.py`, `docs/DECISIONS.md`(ADR-0014),
`docs/DEBATE_LAYER.md`(§6), `docs/IMPORT_FORMATS.md`(instrument spec), `docs/OPERATIONS.md`, `docs/schemas/EvidenceBundle·EvidenceItem.schema.json`
(재생성 — EvidenceItem의 `AQE_NUMERIC` 누락은 토론 계층 때부터의 이전 불일치), 이 파일.
API: `compute_bundle_hash(..., plan_candidates=None)`(기존 호출 그대로 동작), `check_debate` 결과에 `plan_selections`, `debate_summary`에 `plan_snapshot_id`·`plan_selections`.

## 3. 검증 결과 (모두 개발 venv, Windows 11 Pro 10.0.26200, Python 3.12.10)
| 명령 (cwd `C:\vscode\aqe-dev`) | 결과 |
|---|---|
| `.venv\Scripts\python -m pytest -q -p no:cacheprovider tests/test_debate_layer.py` (핵심 코드 변경 후) | **41 passed**, exit 0, 44.6초 — D01–D05·R01/R02 회귀 유지 |
| `... pytest tests/test_trade_plan.py -m "not slow"` / `-m slow` | **38 passed** / **7 passed**(`.venv-ta` 실제 LangGraph worker, scripted LLM), skip 0 |
| `... pytest -q -p no:cacheprovider` (전체) | **195 passed**, exit 0, 98.1초 (이전 150 + 신규 45). 마지막 코드 수정 뒤 재실행: **195 passed**, exit 0, 75.2초 |
| 비활성 시 프롬프트 비교: scratchpad `cmp_prompts.py`(HEAD의 debate.py와 대조) | 프롬프트 동일 True, prompt hash 동일 True |
| 변이 확인: 가격 텍스트 검사·원장 미반영 시 NOT_COMMITTED를 임시로 끄고 실행 | 해당 시험 2개 실패(검출됨) → 원복 후 38 passed |
| 실제 자료 스냅샷 A: `aqe context-enqueue/context-worker --once/analyze --config configs\dev_trade_plan_offline.yaml --as-of 2026-11-11T14:05:10Z` | 모두 exit 0. flow 근거 55행(14:00Z까지, depth·imbalance 표시). 1h·24h 각각 기술적 LONG·SHORT 후보(진입 조건 `close_in_zone` 미충족, tick UNKNOWN), 위험 RESEARCH_ONLY, 선택 출처 UNAVAILABLE → **WAIT** |
| 스냅샷 A 재실행(최종 코드, 입력 표기 변경으로 새 묶음) | enqueue 0 / worker: 출력 첫 줄만 보려던 `Select-Object -First 1`이 프로세스를 끊어 **exit -1·run 기록 RUNNING**(작업 자체는 DONE·outbox 0 확인, 이어서 `--once` exit 0) / analyze 0 |
| 실제 자료 스냅샷 B: 같은 명령 `--as-of 2026-11-11T14:10:10Z` (최종 코드) | 모두 exit 0. flow 마지막 가용 14:00:00.029Z → **STALE, 항목 없음**. 1h 후보는 TP1 순RR < 1.0으로 선택 불가 → scripted 선택 null, 24h는 LONG 선택(ACCEPTED, scripted). 분석은 scripted 제외 → **1h·24h WAIT** |
| 원장: `load_ledger/verify_ledger(E:\aqe-v2-dev\data\meta\aqe_meta_trade_plan.sqlite)` | 14행, 문제 0 |

보고서: `E:\aqe-v2-dev\data\reports\analysis-20261111T195547Z-651b38c7.{json,md}`(B), `...195454Z-928ece73`(A 재실행), `...195228Z-f3fafa62`(A).
증거 구분: **가격 후보 계산·표시(실제 자료 스냅샷)** + **대체 LLM 배선·계약·장애 시험**. 실제 LLM 호출 0, 모델 학습 0, 시뮬레이션 0, 주문 0.
실제 AI 선택 품질·후보의 수익성은 검증하지 않았다. RESEARCH_ONLY 그대로.

## 4. 남은 작업 (우선순위, 서로 별개)
1. 사용자 결정: 이번 미커밋 변경 커밋/push, 이후 main 병합 시점(add/add 충돌 → 개발 쪽 채택).
2. G01: `5m_live`/REST 봉 공통 조회 — 후보·토론·분석 모두 REST `5m`만 읽음(라이브에서 오래되면 STALE/WAIT).
3. tick import(거래소 exchangeInfo 등, `docs/IMPORT_FORMATS.md` 형식) — 지금은 없어서 `tick_size=UNKNOWN` → 항상 WAIT.
4. 실제 LLM 선정·연결(제공자·키·가격표·예산 결정 후) — 실제 선택 품질 미검증, 예산 0이라 거부됨.
5. 가설·계획 결과 채점(구조안 §9): 후보 도달·RR 실현 여부 측정.
6. 향후 시뮬레이션(리플레이·체결 모델) — 이번 범위 밖.
7. NEWS·CROSS_MARKET 활성화, 토론 job 생성 규칙, 원장 부분 조회·outbox 오류 알림(이전 목록 유지).

## 5. 수집기에 아직 적용하지 않은 변경
없음(수집기 코드·설정·venv·데이터 변경 0건). 개발 브랜치 변경 중 수집기가 import할 수 있는 모듈: `config/__init__.py`(`LocalConfig.planning`
기본값 추가 — 기존 YAML 그대로 로드, 수집 경로 불변), `contracts/context.py`(optional 필드), `storage/metadb.py`(aadae59의 표 3개·트리거).
원본 반영 후 재시작 시 `aqe_meta.sqlite`에 빈 표가 생길 수 있다(하위 호환). trade plan 기능은 설정에서 켜지 않으면 동작하지 않는다.

## 6. 다음 명령
```powershell
cd C:\vscode\aqe-dev
git status; git log --oneline -1                       # dev/debate-layer-update @ aadae59 + 미커밋(§2)
.venv\Scripts\python -m pytest -q -p no:cacheprovider tests\test_trade_plan.py tests\test_debate_layer.py
# 실제 자료 스냅샷 한 시점(개발 전용 DB meta/aqe_meta_trade_plan.sqlite; 같은 as_of 재실행은 같은 job으로 합쳐짐)
.venv\Scripts\aqe context-enqueue --config configs\dev_trade_plan_offline.yaml --as-of 2026-11-11T14:10:10Z
.venv\Scripts\aqe context-worker  --config configs\dev_trade_plan_offline.yaml --once
.venv\Scripts\aqe analyze         --config configs\dev_trade_plan_offline.yaml --as-of 2026-11-11T14:10:10Z   # 보고서 4b 절
```

## 7. 다음 세션 재개 지시문
"C:\vscode\aqe-dev(브랜치 dev/debate-layer-update @ aadae59, 진입·손절·익절 후보 작업 미커밋)에서 docs/RESUME_DEV.md, docs/TRADE_PLAN.md,
docs/DECISIONS.md ADR-0014를 읽고 '남은 작업' 1번(사용자 결정) 이후 항목부터 이어가라. 원본 C:\vscode\aqe와 E:\aqe-v2\data는 실행 중인 수집기가
사용하므로 쓰지 말고, 개발 venv(aqe-dev\.venv, .venv-ta)와 E:\aqe-v2-dev 스냅샷으로만 시험하라. make_dev_snapshot.py는 보충된 5m 봉을 덮어쓰므로
그대로 다시 돌리지 마라. 실제 LLM 호출·모델 재학습·시뮬레이션·모의 주문·RESEARCH_ONLY 해제는 하지 마라."
