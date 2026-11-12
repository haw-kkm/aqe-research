# 진입·손절·익절 후보와 검토안 (trade plan) — 구현 상태

기준: `AQE_Claude_Trade_Plan_Prompt_KO_2026-11-12.md`. 개발 브랜치 `dev/debate-layer-update`(worktree `C:\vscode\aqe-dev`, aadae59 위 미커밋).
설계 결정: `docs/DECISIONS.md` ADR-0014. 증거 구분: **가격 후보 계산·표시**, **대체(scripted) 응답 배선 시험**, **실제 AI 분석 능력 검증**(미실행),
**수익성 검증**(미실행·범위 밖)은 서로 다른 완료 상태다.

## 1. 범위
- 한다: 완료된 봉으로 기간별 LONG/SHORT 가격 후보(진입 구간·손절·익절)를 **코드가** 계산 → 토론 근거 묶음에 같은 스냅샷 포함 →
  종합 역할이 후보 ID 하나 또는 관망을 **선택만** → 코드 검사·기존 위험 판정으로 기간별 검토안 → 보고서 `4b` 절.
- 하지 않는다: 시뮬레이터·리플레이 순회·백테스트·손익곡선·모의 주문·체결/포지션 관리, 실주문·로그인·잔고 조회·수량/배분/레버리지,
  실제 LLM 호출, 새 모델·재학습, `flow_v1`, main 병합·원본 배포. '잔고의 6%'는 향후 금액 담당의 투입액 상한이며 여기서 쓰지 않는다.
- 기본 **비활성**(`planning.enabled: false`). 개발 설정 `configs/dev_trade_plan_offline.yaml`과 시험에서만 켠다. 비활성 시 근거 묶음·프롬프트·
  보고서는 이전과 바이트 단위로 같다(시험 + HEAD 대비 프롬프트 비교).

## 2. 책임 분리
| 단계 | 담당 | 할 수 있는 것 / 없는 것 |
|---|---|---|
| 후보 계산 | `planning/candidates.py` (순수 함수) | 확정 봉 → 지지·저항·ATR → 후보. 학습 모델 아님, 피처 스키마·모델 입력 불변 |
| 분석·비판 | CHART·FLOW 초안, CRITIC | 후보를 **공유 문맥**으로 받아 근거·반대 근거 검토. 같은 봉에서 나온 후보를 독립 증거로 세지 않음 |
| 종합 | SYNTHESIS (기존 호출 1회 안에서) | 기간별 `plan_selection` = 후보 ID 또는 null(관망) + 이유·근거 ID. **가격 생성·수정 불가** |
| 코드 검사 | `planning/selection.py`, `contracts/plan.py` | ID·기간·묶음·만료·선택 가능 여부·근거 ID·가격 텍스트·형식 보완 검사 |
| 최종 판단 | `planning/plan.py` + 기존 위험 엔진 | 모든 관문 통과 + 위험 `REVIEW_CANDIDATE`일 때만 LONG/SHORT(수동 검토 제안), 그 외 WAIT |

분석용 후보 방향(`proposal_side`)과 최종 판단(`decision`)은 별도 필드다. `proposal_side=LONG, decision=WAIT, risk_disposition=RESEARCH_ONLY`가 정상 결과다.

## 3. 후보 계산기 v0 (`swing_atr_v0`)
입력: 상품 키, `as_of`, 기간 목록, 5m 봉, 설정, 비용 가정, tick(있으면), 근거 ID. 출력: `PlanCandidateSet`(스냅샷).

시점 규칙
- `bars_as_of`: `is_closed=True`, `available_at <= as_of`, `close_time <= as_of`인 봉만. 같은 봉의 늦은 개정본은 as_of 이후 가용이면 제외.
- 다른 상품 행이 섞이면 **오류**(걸러내지 않음). 5m가 아닌 기본 봉도 오류.
- 상위 시간봉은 **완전한** 5m 묶음만으로 만든다(부분 시간봉은 고점·저점이 될 수 없음). 전체 시간 격자 위에서 계산해 자료 공백이 pivot·ATR을 끊는다.
- swing 고점/저점 = 좌우 `pivot_width`개 봉보다 높은/낮은 봉(왼쪽 엄격, 오른쪽 동률 허용). `pivot_time`(그 봉의 시작)과
  `confirmed_at`(오른쪽 확인 봉 중 마지막 봉의 `available_at`)을 구분하고, `confirmed_at <= as_of`인 것만 쓴다(계약에서도 검사).
- 마지막 확정 봉이 `max_input_age_seconds`(900초)보다 오래되면 **STALE → 후보 없음**.

계산식 (LONG; SHORT은 대칭)
- ATR = 마지막 `atr_period`개 완료 봉의 true range 단순 평균.
- 지지 = 기준 종가(as_of의 마지막 5m 종가) 이하의 **유지된** swing 저점 중 가장 높은 것(확정 뒤 그 아래 종가가 없었음).
  기준 종가와의 거리 > `max_entry_distance_atr`×ATR이면 후보 없음.
- 진입 구간 = [지지, 지지 + `entry_buffer_atr`×ATR], 손절 = 지지 − `stop_buffer_atr`×ATR.
- 익절 = 진입 구간 위의 **유지된** swing 고점(이후 종가로 넘어선 고점은 제외) 가까운 순 최대 2개(TP1, TP2). 없으면 후보 없음.
- 진입 조건(관측 가능): `close_in_zone` = 마지막 5m 종가가 구간 안(MET/NOT_MET), `level_intact` = 확정 뒤 지지 이탈 종가 없음(MET).
  조건 충족은 체결이 아니다(체결 상태는 존재하지 않음).
- 유효기간 = as_of + min(기간, `max_valid_seconds`).

기본값 (개발 기본값 — 최적이라고 주장하지 않으며 잠금 시험 구간에 맞춰 조정하지 않았다)
| 항목 | 값 |
|---|---|
| 1h 기간 프로필 | 5m 봉, lookback 288개(24h), pivot_width 3, ATR 14 |
| 24h 기간 프로필 | 1h 봉(완전한 5m 12개), lookback 168개(7일), pivot_width 3, ATR 14 |
| entry_buffer_atr / stop_buffer_atr | 0.25 / 0.5 |
| max_entry_distance_atr | 3.0 |
| min_net_rr (TP1, 비용 후) | 1.0 — 미만이면 선택 불가(필터일 뿐 품질 주장 아님) |
| max_input_age_seconds / max_valid_seconds | 900 / 3600 |

정체성: `candidate_id` = 후보 내용 해시, `snapshot_id` = 스냅샷 내용 해시(계약 검증기가 재계산해 대조). 스냅샷에 알고리즘 버전,
설정 해시(`planning_config_hash`), 입력 해시(사용한 5m 봉의 시각·OHLC·가용 시각), 상품·as_of, 사용한 가격대(`levels`), 근거 ID,
`available_at`, `valid_until`이 들어간다. 같은 입력·설정 → 같은 ID(시험).

## 4. 가격·비용 검증
- 모든 가격은 Decimal, 유한·양수. NaN·무한대·0·음수 거부(`dec`, `price_problems`, 계약).
- LONG: `stop < entry_low <= entry_high < tp1 < tp2`, SHORT: `tp2 < tp1 < entry_low <= entry_high < stop`. 손절 거리 0은 관계식 위반.
- RR은 **보수적 진입가**(LONG `entry_high`, SHORT `entry_low`) 기준. 구간의 유리한 끝은 쓰지 않는다(계약이 `conservative_entry`를 강제).
- 비용: 기존 `evaluation.costs.trade_return`(taker 수수료 편도 × 2 + 진입·청산 양쪽 불리한 슬리피지)을 그대로 호출. 값은 실험 설정
  `evaluation.taker_fee_bps=5`, `slippage_bps=2`(위험 엔진·잠금 평가와 같은 값). **funding 미포함**(보유 시간을 모름)을 명시.
- 검사(`checks`): `input_fresh`, `tick_size`, `costs`, `net_reward_tpN`, `net_rr_tp1`. FAIL = 선택 불가, UNKNOWN = 선택은 가능하지만
  최종 판단은 WAIT. 비용 정보가 없으면 순보상·RR을 계산하지 않고(None) UNKNOWN. 순보상 ≤ 0이면 FAIL.
- tick: `{data_root}/imports/instrument_specs/<slug>.json`에서 as_of에 가용한 개정만 사용(`docs/IMPORT_FORMATS.md`). 있으면 불리한 쪽으로
  반올림(구간 넓힘, 손절 멀리, 익절 가깝게) 후 관계식을 **다시** 검사하고, 깨지면 후보 없음. 없으면 반올림하지 않고 `tick_size=UNKNOWN`
  → WAIT. 임의 tick을 채우지 않는다. 현재 개발 스냅샷에는 tick import가 없다.
- RR은 보상/손실의 가격 비율이며 성공 확률·기대수익이 아니다. 방향 확률과 결합해 EV를 만들지 않는다.

## 5. 호가·체결 근거 정정 (`context/evidence.flow_summary_item`)
- 이전 요약은 `bid_depth_10bps_quote`를 찾았지만 수집기는 `depth_bid_10bps_quote`/`depth_ask_10bps_quote`를 기록한다 → 수집기 이름으로 수정.
- 호가 지표는 `book_valid=true` 행만, 체결 합계는 aggTrade 누락 없는 구간(`agg_missing=0`)만 사용(유효성 분리).
- as_of 이전 1시간 창, 마지막 가용 행이 10분보다 오래되면 STALE → 항목 없음(이유는 source health detail에 기록). 이전 코드는 최근 30개
  파일만 읽어 과거 as_of에서 창을 놓칠 수 있었다 → as_of 기준 시간 파티션만 읽는다.
- 한 창의 평균·마지막 값만 제시하고, 잔량 증감·주체 신원·의도는 단정하지 않는다는 문구를 근거에 포함. 가격 후보 계산에는 v0에서 쓰지 않는다.

## 6. 토론 연결
- 후보는 토론 **전**에 `build_bundle`에서 계산되어 `EvidenceBundle.plan_candidates`(optional typed 필드)에 들어간다. `compute_bundle_hash`는
  스냅샷이 있을 때만 `snapshot_id`를 포함 → 기존 묶음 해시 불변, 후보·설정·자료가 바뀌면 묶음 해시와 context cache key가 바뀐다(시험).
- 프롬프트: CHART·FLOW 초안과 CRITIC에 `SHARED_CONTEXT PLAN_CANDIDATES`(전체 내용, 잘라내지 않음), SYNTHESIS에 같은 블록 + `plan_selection`
  스키마. 후보 ID는 `[plan:<id>]` 표기이며 근거 ID 집합에 넣지 않는다(독립 증거로 세지 않음). NEWS·CROSS_MARKET 활성 규칙은 그대로.
- **추가 LLM 호출 없음**: 선택은 기존 종합 호출에 포함. 호출 상한 8, 형식 보완 1회, 예산 사전 검사 그대로(실제 worker 시험: 4회/형식 보완 시 5회).
- 코드 검사(`check_plan_selections`) — 실패 시 선택만 REJECTED, 가설은 유지, 다른 후보로 대체하지 않음:
  계약(가격 등 추가 필드 거부), 이 묶음 스냅샷에 없는 ID, 다른 기간 ID, 다른 상품·as_of, 만료, FAIL 검사가 있는 후보, 두 기간에 같은 ID,
  근거 ID 없음·미지 ID, 이유 텍스트의 가격형 숫자가 후보 가격·근거 값에 없음(LLM의 가격 생성), 형식 보완 출력에만 나타난 선택.
- `offline_scripted`는 `plan_select` 옵션(first/none/ghost/other_horizon/unselectable/price_field/price_text)과
  `format_fix_adds_selection`으로 확장. 그 응답은 분석에서 제외되며(`latest_assessment`), 직접 경로에서도 `SCRIPTED_TEST`로 표시되어 WAIT.

## 7. 저장과 커밋 (R01/R02 보장 유지)
- 선택 판정은 `DebateRecord.plan_selections`(+ `plan_snapshot_id`)에 기록되고, **커밋 트랜잭션 안에서** 최종화된다(`finalize_selections`):
  원장 배치가 반영되면 ACCEPTED 유지, 반영 거부(늦은 결과·원장 변경·같은 job·공통 검증 INVALID)면 `NOT_COMMITTED`.
- 최종 DebateRecord와 assessment는 기존처럼 같은 트랜잭션의 `context_outbox`로 저장되고 파일은 outbox에서 기록된다. 파일 실패 시 다음 worker
  실행이 같은 선택·ID·근거를 재기록(시험: backend 1회). lease를 잃은 worker는 아무것도 커밋하지 않고 감사 기록은 `NOT_COMMITTED`.
- 별도의 가변 plan 원장이나 완료 후 쓰는 결과 파일은 만들지 않았다. 보고용 계획은 분석 단계에서 커밋된 선택 + 당시 위험 판정으로 구성한다.

## 8. 최종 계획 (`TradePlan`)과 위험 제한
- 필드: `plan_id`, 상품, 기간, as_of(사용한 스냅샷), 판단 시각, `valid_until`, 알고리즘/설정/입력 해시, `snapshot_id`, 계획 상태,
  `proposal_side`, `decision`(LONG|SHORT|WAIT), 기술적 후보 목록, 선택 ID·후보(진입 구간·조건, 손절·산출 근거, 익절·산출 근거, 비용·RR),
  근거/반대 근거/가설 ID, 무효화 조건, 선택 출처·상태·이유, WAIT 사유, 기존 `risk_disposition`, `approval_required=True`, `execution_enabled=False`.
- WAIT 조건(하나라도): 위험 판정이 `REVIEW_CANDIDATE`가 아님(RESEARCH_ONLY·ABSTAIN), 실제 LLM 선택 없음(`UNAVAILABLE`)·시험 응답(`SCRIPTED_TEST`),
  선택 REJECTED/NOT_COMMITTED/ABSENT, 종합이 관망 선택, 후보 없음, 검사 UNKNOWN/FAIL, 진입 조건 미충족·미평가, 주 가설 방향과 충돌,
  만료(`EXPIRED`: 새 분석 필요, 새 후보로 옮기지 않음).
- 계약 검증기가 LONG/SHORT에 모든 관문을 강제한다 → RESEARCH_ONLY·시험 응답으로는 LONG/SHORT 객체를 만들 수 없다(시험).
  위험 엔진은 그대로이며 계획 모듈이 대체하거나 완화하지 않는다. 1h와 24h는 별도 계획. 확률·확신은 바뀌지 않는다.
- 분석 시 선택은 그 assessment 묶음의 스냅샷(같은 `snapshot_id`)에서만 찾는다. 선택이 없으면 분석 as_of에서 새로 계산한 기술적 후보만 표시.
- 계좌 정보(잔고·포지션·수량)는 계획에 존재하지 않으며 만들지 않는다. 포트폴리오 미제공은 위험 엔진에서 계속 UNKNOWN.

## 9. 보고서
JSON: `trade_plans`(기간별 `TradePlan`) + `planning`(알고리즘·설정 해시·입력·비용·tick·선택 출처). Markdown: `## 4b. 진입·손절·익절 검토안`
— 한 줄 결론(롱 검토/숏 검토/관망), 분석 후보 방향과 최종 판단 분리, 기술적 롱/숏 후보 유무, 진입 구간·조건 충족 여부, 손절, 익절, 비용 후 RR,
검사 결과, 근거·반대 근거, 무효화 조건, 입력 시각·만료, WAIT 사유. 값이 없으면 '미확보'. ML 확률(§2)·규칙 후보(4b)·LLM 해석(§3b)을 구분.
기능 비활성/구버전 보고서는 이 절 없이 그대로 렌더링(`aqe report` 재렌더 포함).

## 10. 시험 (`tests/test_trade_plan.py`, 합성 경로·임시 DB·대체 LLM)
가격 관계·NaN/무한대·손절 거리 0 / 보수적 진입가·왕복 비용(기존 함수와 일치) / 순보상 ≤ 0 / tick 방향·반올림 뒤 역전 / 비용·tick 미확보 → UNKNOWN·WAIT /
자료 없음·STALE·프로필 없음·가격대 없음 → 사유 / 계좌 필드 없음 / 미래·개정 봉 추가 시 과거 스냅샷 불변 / pivot은 확인 봉 가용 뒤에만 /
다른 상품 거부·부분 시간봉·미확정 봉 제외 / 정상 선택·관망 선택 / 미지·다른 묶음·다른 기간·가격 필드·가격 텍스트·근거 없음·미지 근거·선택 불가·만료 /
같은 ID 두 기간·형식 보완이 추가한 선택 / 토론이 후보·수치 근거를 바꾸지 않음 / 명시적 합성 성공 경로(LONG·SHORT, 실행 false, 계약이 승격 거부) /
RESEARCH_ONLY·ABSTAIN·SCRIPTED·UNAVAILABLE·NOT_COMMITTED·진입 미충족·가설 충돌·LLM 관망 → WAIT / outbox 복구 시 같은 선택·가격, backend 1회 /
lease 상실·늦은 결과 → NOT_COMMITTED / 실제 LangGraph worker(6가지 선택 모드, 호출 수 불변) / queue → worker → 커밋 → 보고서 /
구버전 묶음·기록 역호환 / 후보·설정·tick·자료 변경 → 묶음 해시·cache key 변경 / 기능 비활성 시 묶음·프롬프트·보고서·수치 경로 불변 /
호가·체결 요약의 수집기 열 이름·유효성 분리·STALE·as_of 이후 행 제외.

## 11. 남은 작업 (서로 별개)
1. G01: `5m_live`와 REST `5m` 공통 조회 — 지금은 REST `5m`만 읽고, 라이브에서 봉이 오래되면 STALE/WAIT.
2. tick 정보 import(거래소 exchangeInfo 등)와 개정 이력 — 지금은 import가 없어 개발 스냅샷에서 `tick_size=UNKNOWN`.
3. 실제 LLM 선정·연결(제공자·키·가격표·예산 결정 후) — 실제 선택 품질은 미검증.
4. 가설·계획 결과 채점(구조안 §9) — 후보 적중·RR 실현 여부는 측정하지 않았다.
5. 향후 시뮬레이션(리플레이·체결 모델) — 이번 범위 밖.
6. 호가·체결 요약을 가격 계산에 쓰는 v1(예: 스프레드 기반 슬리피지) — 미구현.
