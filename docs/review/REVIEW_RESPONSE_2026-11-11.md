# 코드 리뷰 대응 — 검토 커밋 6c9ae84 (2026-11-11)

입력: `AQE_Git_Code_Review_KO_2026-11-11.md`(F01~F06, G01~G03)와 `AQE_Git_Review_Reproduction.py`.
재현 스크립트는 리뷰 원본에 F01 출력부 `None` 가드와 F03 예외 포착만 덧붙여 실행했다(판정 로직 동일).

| 단계 | 결과 파일 |
|---|---|
| 수정 전 (이 PC, Windows/CUDA, 커밋 6c9ae84) | `docs/review/repro_before_fix.json` — F01~F06 모두 결함 동작 재현 |
| 수정 후 | `docs/review/repro_after_fix.json` — 아래 표 |

| ID | 수정 내용 | 수정 후 재현 결과 | 회귀 시험 (`tests/test_review_fixes.py`) |
|---|---|---|---|
| F01 | `src/aqe/models/chain.py` 공통 검증: base·meta·calibrator 모두 정체성(자산·기간·프로필·스키마·피처순서·클래스)과 `train_label_cutoff ≤ as_of` 검사. 온라인·평가·보정이 같은 함수 사용. 위험 R1도 meta·calibrator metadata까지 받아 cutoff 검사. **검증 상태의 지식 시점**: OOS 판정·보정 검증·OOS_EVALUATED 준비도는 증거 구간 종료(`established_at`) 이후의 as_of에서만 인정. 차단성 판정(NOT_POSITIVE 등)은 과거 as_of에도 보수적으로 적용 | ensemble `ABSTAIN` (사유: future leakage) | meta만 미래 / calibrator만 미래 / 다른 horizon·상품·스키마·kind, 지식 시점, legacy 이력 파싱, R1 cutoff |
| F02 | 온라인이 평가와 같은 `calibration_chain_problems` 사용: 보정기의 `fitted_on_artifacts`가 현재 base·meta와 정확히 일치해야 함(누락·불완전 lineage도 거부). 모델 교체 시 기존 보정기는 자동 사용 불가, 파일은 보존 | `ABSTAIN` (사유: fitted on OLD-…) | OLD id / 일부 불일치 / 불완전 / lineage 없음 / base 재학습 후 |
| F03 | `ta_bridge.install_network_guard`: 허용 이름이 해석한 IP만 연결 허용, 직접 IP·비허용 loopback 차단·기록(허용 목록이 비어 있지 않아도), `connect_ex`·`sendto`·Windows IOCP `ConnectEx` 경로 포함, `socketpair` 내부 연결만 예외, loopback은 loopback 이름이 허용될 때만, 프록시 환경변수 존재 시 `UNVERIFIED`. 가드 해제 함수 반환 | `127.0.0.2` 직접 연결 차단, `network_denied=["127.0.0.2"]` | 빈/비어있지 않은 목록, 허용 이름→IP 매핑, socketpair, 프록시 |
| F04 | 원자적 예약·정산(`JobQueue.try_reserve/settle`, SQLite `reserved_usd` 추가 마이그레이션): 작업 전 최악 비용(호출수×(최대 입력+출력 토큰)×가격표) 예약, 완료 후 정산, 비용 미확정·timeout·실패는 예약액을 비용으로 청구하고 `cost_unknown_calls` 증가 → **그날 후속 유료 호출 차단**(수동 정산 전). 사용량 장부 없는 유료 호출 거부. `openai_compatible`/`ollama`는 base_url이 loopback일 때만 무료로 간주. TA bridge에 LangChain 사용량 callback(호출·토큰 집계, `max_llm_calls_per_job` 초과 시 중단)·`max_tokens` 전달 | `next_paid_job_allowed=false` | unknown 차단, 동시 예약 상한, 정산, 제공자 이름≠무료, 토큰 불완전 시 청구, 호출 초과 중단 |
| F05 | R4: 필수 근거(`event_required_sources: [MACRO_CALENDAR, NEWS]`)가 AVAILABLE이고 `temporal_scope=INTRADAY_ASOF_VERIFIED`이며 검증된 일정 범위(`calendar_coverage_until`)가 기간 끝을 덮을 때만 PASS. 모르는 경우는 UNKNOWN(PASS 아님). 알려진 HIGH 이벤트는 미검증 평가에서도 보수적으로 적용. 온라인 결합도 시점 미검증 context를 쓰지 않음(`context_usable_for_decision`) | R4 `UNKNOWN`, 최종 `RESEARCH_ONLY` | NOT_CONFIGURED·STALE·UNVERIFIED·DAILY_ONLY·일정 범위 부족 → UNKNOWN, 검증된 빈 일정 → PASS |
| F06 | 예측 캐시 키 = artifact id + 모델 파일 SHA-256 + 입력 프레임 내용 해시(전체 프레임: TCN 창 의존성) + 피처 목록. 키별 파일·sidecar 기록, 기존 open_time 전용 캐시는 읽지 않음. 메타 학습 시 OOS manifest의 dataset 해시가 현재 데이터셋과 다르면 거부 | `recompute_calls=1` | 입력 변경→재계산, 동일 입력→재사용, 모델 파일 교체→재계산 |

시험: `pytest -q` → **93 passed** (기존 57 + 리뷰 회귀 36). 실제 TA 그래프 오프라인 격리 시험도 강화된 가드에서 통과.

실데이터 확인(재학습·재평가 없음):
- 등록 artifact 체인(1h·24h)의 공통 검증 문제 0건, `established_at = 2026-11-10T19:40Z`(legacy 이력에서 파싱).
- replay 2026-03-02: 결합 확률은 계산되지만 준비도 TRAINED_UNVALIDATED·보정 검증 False(당시 미확정) → RESEARCH_ONLY.
- 실시간 research: 결합 OK, R2 시계 +64초로 ABSTAIN.
- 잠금 시험은 **다시 실행하지 않았다**(ledger trial 2 유지). 코드 변경은 판정 방어 로직이며 모델·정책·구간을 바꾸지 않았다.

G 항목(결함과 구분)
- G01: `5m_live` 병합·shadow 성숙 라벨 채점기 미구현 — 그대로 NEXT_SESSION 작업으로 유지. REST top-up 기반 shadow 경로는 존재.
- G02: E4/E5·numeric_context는 API 키만으로 완성되지 않음 — context 피처 계약·시점 결합 데이터셋·학습기·동일 조건 평가기 구현이 남음(BUILD_STATUS에 명시).
- G03: 평가 보고 표기를 "거래별 순수익률 산술합", "MDD(log 단위)"로 수정하고 E3 대 E1 비교는 검정되지 않았음을 명시. 기존 잠금 결과(비용 후 손실)는 그대로 보존.
