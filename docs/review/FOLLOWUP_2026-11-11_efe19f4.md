# 후속 검토(커밋 efe19f4) 보완 대응 — 2026-11-11

입력: `AQE_Current_Status_Training_Flow_KO_2026-11-11.md` §7.2(보완 A), §7.3(보완 B).
시험: `pytest -q` → **109 passed** (이전 93 + `tests/test_supplement_ab.py` 16). 실제 과금 호출 0건.
모델·데이터셋·잠금 시험은 바꾸거나 다시 실행하지 않았다(ledger trial 2 유지).

## 보완 A — 비용 예약을 실제 상한으로
| 문제 | 수정 | 시험 |
|---|---|---|
| `max_prompt_tokens_per_call`이 예약 계산에만 쓰이고 실제 프롬프트를 검사하지 않음 | 단순 LLM: 호출·예약 **전에** 프롬프트 상한(UTF-8 바이트 + 메시지당 16; byte-level BPE에서 토큰 수의 증명 가능한 상한)을 계산해 초과 시 MISSING으로 거부 | 리뷰 mock 조건(상한 0.01, 1토큰 가정, 1달러/1천 토큰) → 제공자 호출 0회, 장부 0 |
| OpenAI 호환 요청에 출력 상한 미전달 | `openai`는 `max_completion_tokens`, 그 외 OpenAI 호환(ollama 등)은 `max_tokens`로 `max_output_tokens` 전달 | 요청 JSON에 필드 존재 확인(3개 제공자) |
| 그래프 누적 문맥·재시도 | TA bridge 사용량 callback이 **각 호출 직전** 메시지+도구/스키마(invocation_params) 바이트로 상한을 계산해 초과 시 중단; 업스트림 기존 키 `llm_max_retries=0`(숨은 재시도 금지), `max_tokens` 전달 → 예약 = 최대 호출수 × (최대 입력 + 최대 출력) × 가격 | `.venv-ta`에서 callback 직접 시험(큰 프롬프트·초과 호출 중단) |
| 제공자가 상한을 무시하는 경우 | 정산 시 실제 비용 > 예약이면 `budget_overruns` 증가(SQLite 추가 마이그레이션 003) → 당일 후속 유료 호출 차단 | mock 응답이 예약 초과 → 기록·차단 |
| backend별 장부 | 일일 비용·호출 상한과 미확정/초과 차단을 **모든 context backend 합산**으로 적용 | simple_llm 예약 후 tradingagents 예약이 합산 상한에 걸림 |

남은 한계: 바이트 상한은 영어 기준 실제 토큰의 약 3~4배라 보수적이다(긴 근거 묶음은 거부될 수 있음 → `max_prompt_tokens_per_call` 조정 필요).
제공자가 출력 상한을 지키지 않으면 첫 초과 1회는 막지 못하고 즉시 기록·차단한다. 실제 제공자 응답으로는 아직 미검증.

## 보완 B — 일정·뉴스 커버리지는 검증 가능한 manifest로만
| 문제 | 수정 | 시험 |
|---|---|---|
| CSV 존재만으로 AVAILABLE, coverage = as_of+3일 | 데이터 파일마다 manifest(`source_id`, `fetched_at`/`available_at`, `coverage_start`/`coverage_end`, `content_sha256`) 필수. manifest 없음·해시 불일치·as_of 이후 가용·구간 역전 → MISSING. as_of를 포함하는 검증 구간이 없으면 STALE. `calendar_coverage_until` = manifest 구간 끝(합친 구간) | 리뷰 mock(2020년 파일만, 2026-01-10 조회) → STALE·coverage None; manifest 없음/미래 가용/변조/역전 거부 |
| 빈 일정의 정상 처리 | 검증 구간이 기간을 덮고 사건 0건 → AVAILABLE·PASS 가능 | 빈 일정 → coverage = manifest 끝(2026-01-12), 30분 구간 → R4 UNKNOWN |
| 뉴스도 같은 문제 | 뉴스 manifest 구간이 `as_of − 1h`까지 이어져야 AVAILABLE | 12시간 전까지만 덮은 뉴스 → STALE |
| 계약 변경 영향 | `SourceHealth.coverage_start/coverage_end` 추가. 번들 해시는 coverage가 있을 때만 포함 → 기존 저장 번들 해시 유지 | 기존 해시 공식과 동일 확인, 실제 저장 번들 재검증 통과 |

형식 문서: `docs/IMPORT_FORMATS.md`.
