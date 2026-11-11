# 분석·토론 계층 — 구조안 v0.1 대비 구현 상태

기준: `AQE_Agent_Debate_Structure_KO_v0_1.md`(2026-11-11). 개발 브랜치 `dev/debate-layer`(worktree `C:\vscode\aqe-dev`).
구분: **대체 LLM 시험**(scripted, 배선 검증) / **실제 LLM 호출**(미실행) / **실제 시장 자료 연결** / **분석 성능 검증**(미실행)은 서로 다른 증거다.

## 1. 기존 구현과의 관계 (중복 없음)
| 기존 | 상태 | 이번 작업에서의 사용 |
|---|---|---|
| TradingAgents 어댑터(`ta_bridge.py`, `TradingAgentsBackend`) | 존재, 고정 SHA 9fa1196, 오프라인 실그래프 시험 통과 | **수정 없음**. 업스트림 그래프(애널리스트→Bull/Bear→리서치 매니저→트레이더→위험 토론→PM)는 그대로 `tradingagents` backend(E5)로 유지 |
| TA 업스트림 LLM 팩토리 `create_llm_client` | 존재 | 토론 worker가 실제 LLM 모드에서 재사용(callbacks·max_tokens·max_retries=0 전달) |
| LangGraph (TA 의존성으로 `.venv-ta`에 설치) | 존재 | 토론 흐름(병렬 초안→비판→조건부 재검토→종합)을 StateGraph로 구현 — 두 번째 오케스트레이터 미도입 |
| 네트워크 가드·사용량 상한(`ta_bridge`) | 존재(F03/F04/보완 A) | 토론 worker가 그대로 import해 사용 |
| EvidenceBundle·ContextAssessment·비용 예약/정산·job 큐 | 존재 | 입력·출력 계약으로 재사용. 토론 결과는 ContextAssessment로 투영 |
| 위험 엔진 R1–R6 | 존재 | 변경 없음. 토론은 RESEARCH_ONLY 등 제한을 해제할 수 없음(시험) |

선택 근거(ADR-0011): TA 그래프에는 비판자·1회 재검토·가설 수명주기 노드가 없고 역할 구성이 다르다. 이를 바꾸려면 업스트림 그래프를 fork해야 하므로,
업스트림은 그대로 두고 **역할 아이디어 + LangGraph + TA LLM 팩토리만 재사용**한 별도 그래프로 만들었다. "TradingAgents 전체 통합"으로 표시하지 않는다.

## 2. 구조안 항목별
| 구조안 | 구현 | 상태 |
|---|---|---|
| §10-1 공통 계약·자료 연결 | `contracts/debate.py`(AnalystReport, CritiqueReport, SynthesisReport, Hypothesis, HypothesisRecord, DebateRecord); `context/evidence.numeric_evidence` — AQE 수치 예측을 시점·artifact 체인 검증을 거쳐 근거 항목(`AQE_NUMERIC`)으로 제공 | 구현·단위시험 |
| §4 역할 | CHART(봉·일봉·수치 예측), FLOW(funding·OI·호가/체결 요약 + 가격 요약은 공유 문맥으로 표시) 활성. NEWS·CROSS_MARKET은 검증된 근거가 없으면 `NOT_ACTIVATED`(사유 기록) | 구현. NEWS/CROSS_MARKET은 현재 자료 없음 |
| §6-1 자료·시간 검사 | EvidenceBundle 검증(available_at ≤ as_of), 역할 활성화 판정 | 구현 |
| §6-2 독립 초안 | 역할별 근거 slice만 전달, 다른 보고서·이전 종합 미전달(시험) | 구현 |
| §6-3 주장 구조화 | OBSERVATION / INTERPRETATION / HYPOTHESIS, 가설마다 철회 조건 필수, 확신은 LOW/MEDIUM/HIGH 문구만 | 구현 |
| §6-4 비판 | 근거 연결·범위·중복·기간 혼합·시간 위반·주체 의도 단정 검사, 반대 강요 없음, 재검토 역할 최대 1개 | 구현 |
| §6-5 제한된 보완 | 지정 역할 1회 재검토(예산 2회 이상 남을 때만) | 구현 |
| §6-6 종합 | 다수결·확신 평균 금지 지시, 기간별 주/반대 가설·미해결, 이전 가설 KEEP/REVISE/INVALIDATE | 구현 |
| §6-7 기계 검사 | 근거 ID ⊂ 묶음, 역할/회차 일치, 기간 ⊂ 설정, 누락 기간 → ABSTAIN, INVALIDATE는 근거 필수, REVISE는 대체 가설 필수 | 구현·시험 |
| §6-8 저장 | `context/debates/<inst>/<assessment>.debate.json`, 가설 이력 `context/hypotheses/<inst>.jsonl`(append-only) | 구현 |
| 호출 상한 8 | worker가 매 호출 전 검사 + 실제 LLM 모드에서 usage callback 상한·프롬프트 상한·재시도 0, 비용 예약 = 8회분 | 구현(대체 LLM으로 시험) |
| §7 가설 상태 | ACTIVE / REVISED / INVALIDATED / EXPIRED(코드가 시간으로 판정) / ABSTAIN; 이전 판단 연결(previous_id) | 구현·시험 |
| 늦게 도착한 분석 | 이력의 최신 as_of보다 오래된 토론은 이력에 반영하지 않음; 대기 중 오래된 job은 SUPERSEDED | 구현·시험 |
| 수치 확률·LLM 확신 분리 | 토론은 확률을 쓰지 않고 `contextual_rating=None`; 위험 판정 불변 | 구현·시험 |
| 보고서 | `analyze` 보고서에 "3b. 분석·토론 가설"(주/반대 가설, 철회 조건, 상태, 역할 활성, 비판 수) | 구현 |
| §9 평가(탐지·철회·예측 채점) | — | 미구현(다음 단계) |
| §10-3 뉴스·교차시장 활성 | manifest 검증 import 또는 공급자 필요 | 자료 미확보 |
| §10-4 실제 LLM | 제공자·키·가격표·예산 필요 | 미실행 |
| §8 모의 운용 팀 | — | 범위 밖(이번에 하지 않음) |
| §11 자동 연구 팀 | — | 범위 밖 |

## 3. 사용 방법(개발 폴더)
```powershell
# configs/<dev>.yaml: context.backend: aqe_debate, context.llm.provider: offline_scripted (배선 시험)
.venv\Scripts\aqe context-enqueue --config configs\<dev>.yaml   # AQE 수치 예측 포함 근거 묶음 + job
.venv\Scripts\aqe context-worker --config configs\<dev>.yaml --once
.venv\Scripts\aqe analyze --config configs\<dev>.yaml           # 보고서 3b 섹션
```
`offline_scripted` 결과는 분석에 사용되지 않는다(`latest_assessment`가 제외). 실제 LLM은 비용 예약 규칙(ADR-0008/보완 A)을 그대로 따른다.
