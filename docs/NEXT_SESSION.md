# NEXT_SESSION — 다음 세션 시작점 (작성: 2026-11-11 KST, 세션 1 종료 시)

먼저 읽을 것: `AQE_TradingAgents_Blueprint_KO.md` → `BUILD_STATUS.md` → 이 파일 → `docs/DECISIONS.md`.
이미 된 작업을 재생성하지 말 것. 데이터·artifact는 아래 경로에 실제로 존재한다(없으면 있다고 가정하지 말고 `aqe doctor`로 확인).

## 현재 존재하는 것
- 코드: `C:\vscode\aqe` (Git 저장소 아님). venv: `.venv`(core, torch 2.15.0+cu132), `.venv-ta`(TradingAgents 0.7.1 @ 9fa1196…).
- 데이터 `E:\aqe-v2\data`: `normalized/bars/.../5m`(REST, 754,5xx 확정 봉), `5m_live`(LIVE_CAPTURE 소량), `funding/`, `oi_hist/`(최근 29일),
  `flow_summaries/`·`mark_funding_estimates/`·`oi_observations/`(수집기 2회 단기 실행분), `raw/`(gzip 원본), `manifests/`, `meta/aqe_meta.sqlite`,
  `context/`(bundle·오프라인 TA 실행 결과), `reports/`, `runs/`, `status/`.
- Artifact `E:\aqe-v2\artifacts\exp_btcusdt_ohlcv_v0_001`: `dataset/`, `artifacts/`(38개: 최종·fold·메타·보정; 등록본은 `registry.json`),
  `oos/`, `pred_cache/`, `evaluation/`(eval 보고서 2개, `ledger.jsonl` trial 2).
- 이전 빌드 데이터 `E:\aqe-data`는 손대지 않았고 사용하지 않는다.

## 차단 원인 (사용자 결정/조치 필요)
1. 시스템 시계 +62초 → Windows 자동 시간 동기화(관리자). 해결 전 research/shadow 분석은 R2로 ABSTAIN.
2. PostgreSQL/TimescaleDB 미설치 → `AQE_PG_DSN` 제공 시 `aqe init-db`로 migration 적용·검증.
3. LLM 공급자/모델/키/가격표/일일 비용 상한 미정 → TradingAgents·단순 LLM 실연결 BLOCKED.
4. 뉴스·일정·교차자산 공급자 미정 → 현재는 검증된 파일 import 형식만 지원.

## 정확한 다음 명령
```powershell
cd C:\vscode\aqe
.venv\Scripts\aqe doctor --config configs\local.yaml            # clock 항목이 OK인지 먼저 확인
.venv\Scripts\python -m pytest -q                              # 57 passed 기대
# 상시 수집(flow_v1 축적) — 별도 창
powershell -ExecutionPolicy Bypass -File deploy\windows\supervise.ps1 -Command collect -Config configs\local.yaml
# shadow 전향 로그: configs\local.yaml 복사 → configs\shadow.yaml 에서 mode: shadow 로 변경 후
powershell -ExecutionPolicy Bypass -File deploy\windows\shadow_loop.ps1 -Config configs\shadow.yaml
```
LLM 결정 후(예시, 값은 사용자 결정):
```yaml
# configs/local.yaml
context:
  backend: tradingagents
  max_cost_usd_per_day: <상한>
  llm: {provider: <openai|anthropic|...>, model_id: <모델>, api_key_env: <ENV 이름>,
        usd_per_1k_prompt_tokens: <가격>, usd_per_1k_completion_tokens: <가격>}
```
```powershell
.venv\Scripts\aqe context-enqueue --config configs\local.yaml
.venv\Scripts\aqe context-worker --config configs\local.yaml --once
```

## 남은 구현 작업 (우선순위)
1. TradingAgents bridge에서 LLM 토큰 사용량 수집(LangChain callback) → 비용 추정; 실 LLM 1건 실행 후 지연·호출수 기록.
2. `5m_live`와 `5m` 병합 정책(확정 LIVE_CAPTURE 우선) 및 analyze가 수집기 봉을 읽도록 연결(ADR-0003).
3. flow_v1 피처(L2 불균형·spread·depth·CVD·OI 변화·funding) 계산기 + 별도 프로필 artifact. 데이터 축적 후 학습.
4. shadow 전향 평가 집계기(보고서 → 성숙 라벨과 대조, generated_at 기준 판단 시각).
5. 잠금 시험 결과에 따른 설계 검토: 1h 비용 대비 엣지 부족, 24h 메타가 LGBM 단독보다 나쁨, LGBM–TCN 손실 상관 높음 →
   TCN 제외/메타 단순화 여부는 **새 실험 ID와 새 잠금 구간(또는 전향 구간)**으로만 판단(현 시험 구간 재사용 금지).
6. ETHUSDT 확장(설정에 instrument 추가 → backfill → 별도 실험 ID).
7. PostgreSQL 연결 시 run/registry/prediction/risk 로그 DB 기록 경로 추가.
