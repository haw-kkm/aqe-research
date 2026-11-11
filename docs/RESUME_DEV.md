# RESUME_DEV — 개발 worktree 인수인계 (분석·토론 계층)

갱신: 2026-11-12 00:16 KST (2026-11-11 15:16 UTC) · 개발 폴더 전용 문서. 원본 `C:\vscode\aqe`의 BUILD_STATUS/NEXT_SESSION은 수정하지 않았다.

## 1. 위치와 격리
| 항목 | 값 |
|---|---|
| 원본(수집기 사용 중) | `C:\vscode\aqe` · `main` @ `25b3623` · **이번 세션에서 쓰기 0건**(git status 세션 시작과 동일 확인) |
| 실행 중 수집기 | `local-collector/collect_local.py --project-root C:\vscode\aqe` → `aqe collect` (PID 6220/32640/35508/35964, 2026-11-11 13:40 KST 시작, run `run-20261111T044005Z-29fc10ca`), data_root `E:\aqe-v2\data`. 종료 시 원본 `src`를 다시 import해 자동 재시작 |
| 개발 폴더 | `C:\vscode\aqe-dev` = git worktree, 브랜치 `dev/debate-layer` (분기점 25b3623), **미커밋** |
| 개발 venv | `aqe-dev\.venv` (Python 3.12.10, torch 2.15.0+**cpu**, 나머지 requirements.lock), `aqe-dev\.venv-ta` (TradingAgents 0.7.1 @ 9fa1196, LangGraph 1.2.20) |
| 개발 데이터 | `E:\aqe-v2-dev\data`, `E:\aqe-v2-dev\artifacts` — `scripts/make_dev_snapshot.py`로 **쓰기가 끝난 파일만 복사**(REST 봉·funding·OI, 13시 UTC까지 완료된 수집기 시간 파일, 등록 모델 12개) + 개발 루트로 REST 보충(봉 153개, OI 235행, funding 2행) |

원본의 미커밋 상태(손대지 않음): `src/aqe/context/evidence.py`, `src/aqe/contracts/context.py` 수정, 신규 `src/aqe/context/debate.py`, `debate_worker.py`,
`src/aqe/contracts/debate.py`, 사용자 도구 `local-collector/`. **원본의 debate 파일 3개는 이번 세션 시작 시점의 구버전**이다 — 병합·커밋은 `dev/debate-layer` 기준으로 하고 원본 쪽 구버전은 커밋하지 말 것.
수집기 재시작 시 원본에서 import되는 변경은 `contracts/context.py`의 enum 1개(`AQE_NUMERIC`) 추가뿐 — 수집 경로 영향 없음.

## 2. 이번 세션 작업 (AQE_Agent_Debate_Structure_KO_v0_1 §10 1~2단계)
| 단위 | 상태 | 파일 |
|---|---|---|
| 개발 worktree·미커밋 변경 복사(cmp 일치) | DONE | — |
| 개발 venv 2개 | DONE | `.venv`, `.venv-ta` |
| 계약 | DONE | `src/aqe/contracts/debate.py` (Hypothesis 철회조건 필수·숫자 확신 금지·기간 혼합 금지, HypothesisRecord.position 추가) |
| 근거 어댑터(AQE 수치 예측 → EvidenceBundle, `AQE_NUMERIC`) | DONE | `context/evidence.py::numeric_evidence`, `build_bundle(include_numeric=)` |
| worker 그래프(LangGraph: 병렬 초안→비판→조건부 1회 재검토→종합→형식 보완 1회, 상한 8회) | DONE | `context/debate_worker.py` (시도한 호출은 실패해도 계수) |
| core: 역할 slice·활성화, 프롬프트, 코드 검사, 가설 이력(append-only), ContextAssessment 투영, 기록 저장 | DONE | `context/debate.py` |
| 연결: 설정 `context.debate`, backend `aqe_debate`, job 합치기(SUPERSEDED), enqueue가 수치 근거 포함 | DONE | `config/__init__.py`, `context/backends.py`, `storage/metadb.py`, `context/worker.py` |
| 보고서 "3b. 분석·토론 가설" | DONE | `reporting/report.py::render_debate`, `cli/analyze.py` |
| 문서 | DONE | `docs/DEBATE_LAYER.md`(구조안 대비 표), `docs/DECISIONS.md` ADR-0011, 이 파일 |

## 3. 검증 결과 (모두 개발 venv, Windows 11, CPU torch)
| 명령 | 결과 |
|---|---|
| `cd C:\vscode\aqe-dev; .venv\Scripts\python -m pytest -q -p no:cacheprovider tests/test_debate_layer.py` | **17 passed**, exit 0 (단위 14 + `.venv-ta`의 실제 LangGraph worker 통합 3, skip 0) |
| `... -m pytest -q -p no:cacheprovider` (전체) | **126 passed**, exit 0 (기존 109 + 신규 17) — 보고서 렌더링 assert 추가 전 실행; 추가 후 debate 파일만 재실행 17 passed |
| 실제 자료 스냅샷 연결: `aqe context-enqueue --config configs/dev_debate_offline.yaml --as-of 2026-11-11T13:55:10Z` → `aqe context-worker ... --once` | exit 0. 근거 7개(봉·일봉·funding·OI·**수집기 호가/체결 요약**·수치 예측 1h/24h), CHART/FLOW ACTIVE, NEWS/CROSS_MARKET NOT_ACTIVATED, 대체 LLM 호출 4/8, 코드 검사 실패 0, 가설 4개 ACTIVE, assessment `ctx-bd7a000b861d` PARTIAL(범위 축소 표시) |

증거 구분: 위는 **대체(scripted) LLM 배선 시험 + 실제 시장 자료 연결**이다. **실제 LLM 호출 0건**, **분석 성능 검증 없음**.
offline_scripted 결과는 `latest_assessment`가 제외하므로 분석 보고·위험 판정에 쓰이지 않는다. RESEARCH_ONLY 제한은 그대로다(시험).

## 4. 남은 작업 (우선순위)
1. 사용자 결정: `dev/debate-layer`를 커밋/병합할지. 원본 폴더 반영은 **수집기를 멈춘 뒤** 하거나, 수집기 재시작 시 새 코드가 import됨을 감안해 결정.
2. 구조안 §9 채점: 가설 철회 조건 발생·만료·기간별 결과를 성숙 라벨과 대조하는 채점기(탐지·철회·예측 분리).
3. G01: `5m_live`/REST 봉 공통 조회 — 현재 토론·분석은 REST `5m`만 읽음(개발 스냅샷은 REST 보충으로 해결).
4. NEWS·CROSS_MARKET 활성화: manifest가 있는 검증 import 또는 공급자(`docs/IMPORT_FORMATS.md`).
5. 실제 LLM(제공자·키·가격표·예산 결정 후): 소규모 호출로 품질·토큰·비용·지연 기록 — 현재 예산 0이라 거부됨.
6. 토론 job 생성 규칙(봉 갱신·중요 변화 임계값)은 전향 지연·비용 측정 후 확정 — 지금은 수동 enqueue.

## 5. 수집기에 아직 적용하지 않은 변경
없음(수집기 코드·설정·venv·데이터 변경 0건). 이번 변경 중 수집기가 import하는 모듈에 닿는 것: `contracts/context.py`(enum 추가), `config/__init__.py`(`DebateConfig` 추가, 기본값 있음), `storage/metadb.py`(메서드 추가) — 모두 하위 호환이며 원본에는 아직 반영 안 함.

## 6. 다음 명령
```powershell
cd C:\vscode\aqe-dev
git status; git log --oneline -1                       # dev/debate-layer, 미커밋 확인
.venv\Scripts\python -m pytest -q -p no:cacheprovider tests\test_debate_layer.py
# 실제 자료 스냅샷 갱신(완료된 시간까지): --until-hour 를 현재 UTC 시각 - 2시간으로
.venv\Scripts\python scripts\make_dev_snapshot.py --src-data E:/aqe-v2/data --src-art E:/aqe-v2/artifacts --dst-data E:/aqe-v2-dev/data --dst-art E:/aqe-v2-dev/artifacts --experiment exp_btcusdt_ohlcv_v0_001 --until-hour <YYYY-MM-DDTHH>
```

## 7. 다음 세션 재개 지시문
"C:\vscode\aqe-dev(브랜치 dev/debate-layer, 미커밋)에서 docs/RESUME_DEV.md와 docs/DEBATE_LAYER.md를 읽고 '남은 작업' 1번(사용자 결정) 이후 항목부터 이어가라.
원본 C:\vscode\aqe와 E:\aqe-v2\data는 실행 중인 수집기가 사용하므로 쓰지 말고, 개발 venv(aqe-dev\.venv, .venv-ta)와 E:\aqe-v2-dev 스냅샷으로만 시험하라.
실제 LLM 호출·모델 재학습·모의 주문·RESEARCH_ONLY 해제는 하지 마라."
