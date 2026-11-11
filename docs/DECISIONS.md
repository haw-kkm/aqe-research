# Architecture decision records (minimal changes that preserve the blueprint)

## ADR-0001 — Python 3.12 core venv + separate TradingAgents worker venv
- Context: B03 allows splitting core and context-worker environments if dependencies conflict.
- Decision: core `.venv` (Python 3.12.10) and `.venv-ta` (Python 3.12.10, TradingAgents @ pinned SHA).
  TradingAgents loads `.env` at import, mutates global config and pulls LangChain/yfinance; isolating it in a
  subprocess (`ta_bridge.py`, JSON in/out) keeps the core deterministic and lets the worker be killed on timeout.
- Consequence: the JSON contract (`EvidenceBundle` in → bridge result → `ContextAssessment`) is the boundary.

## ADR-0002 — Local SQLite metadata store until PostgreSQL/TimescaleDB is available
- Context: B05.4 targets PostgreSQL/TimescaleDB. Doctor: no PostgreSQL service, no Docker on this PC.
- Decision: PostgreSQL migrations are written (`migrations/postgres/001_init.sql`, additive only) and applied by
  `aqe init-db` when `AQE_PG_DSN` is set. Until then the context job queue / usage budget / health tables use a
  local SQLite file with the same table names and lease/idempotency semantics. Normalized market data stays in
  Parquet + manifests (B05.4 does not require ticks in DB).
- Status: PostgreSQL path **not executed** (BLOCKED). Not reported as DB-validated.

## ADR-0003 — Live 5m klines stored in a separate `5m_live` partition
- Context: backfill resumes from the newest closed bar in `bars/.../5m`. If the collector wrote LIVE_CAPTURE bars
  there, a gap between backfill end and collector start would never be refilled.
- Decision: collector writes `bars/.../5m_live` (provenance LIVE_CAPTURE). Datasets/analyze read `5m`
  (ASSUMED_LATENCY_REPLAY via REST). Merging policy is a later task (prefer LIVE_CAPTURE for rows it covers).

## ADR-0004 — CUDA PyTorch build for TCN training
- Context: CPU throughput ≈400 samples/s (≈7 h for the fold protocol). RTX 3060 Ti available.
- Decision: `torch==2.15.0+cu132` (only CUDA index carrying 2.15.0 for cp312/win). `tcn.device: auto` falls back to
  CPU; CPU smoke runs remain supported (tests run on CPU).

## ADR-0005 — Readiness is a stage; skill is a separate verdict
- Context: B10 readiness enum describes validation stage. A model can be OOS-evaluated and still show no skill.
- Decision: `aqe evaluate` sets `OOS_EVALUATED` after the locked test and records `oos_verdict`
  (`SKILL_DEMONSTRATED` only if the block-bootstrap 95% CI of Δlog-loss vs the prior baseline is entirely < 0).
  Risk rule R1 (`require_oos_skill: true`) caps the disposition at RESEARCH_ONLY otherwise. Calibration is
  `calibration_validated=true` only if the locked-test ECE ≤ 0.05 and the calibration segment had ≥100
  non-overlapping label windows.

## ADR-0007 — Validation results have a knowledge time (review F01)
- A locked-test verdict, `calibration_validated` and `OOS_EVALUATED` readiness are facts learned at the end of the
  evidence segment (`readiness_history[].established_at`; legacy entries parsed from the evidence string).
- Replay/online use them only for `as_of >= established_at`. Earlier as_of → readiness TRAINED_UNVALIDATED,
  calibration not validated, positive verdicts `NOT_ESTABLISHED_AT_AS_OF`. Blocking verdicts (e.g. NOT_POSITIVE)
  are applied to any as_of (stopping use on current knowledge is allowed; claiming past knowledge is not).

## ADR-0008 — LLM budget: reserve, settle, block on unknown (review F04)
- Before a paid job: atomically reserve calls × (max_prompt_tokens + max_output_tokens) × price table.
- After: settle with the actual cost; if unknown (missing usage, timeout, failure after send) the reservation is
  booked as cost and `cost_unknown_calls` blocks further paid calls that day until a human reconciles
  (docs/OPERATIONS.md). `ollama`/`openai_compatible` count as free only on a loopback base URL.

## ADR-0009 — Unknown event information is not 'no event' (review F05)
- Event risk passes only with AVAILABLE `event_required_sources` (MACRO_CALENDAR, NEWS), an
  INTRADAY_ASOF_VERIFIED assessment and a verified calendar window covering the horizon. Otherwise UNKNOWN
  (caps at RESEARCH_ONLY). A verified empty calendar is a real PASS.

## ADR-0006 — No numeric_context meta yet
- No historical, point-in-time structured context assessments exist (news receipt times are not recoverable,
  no LLM provider configured). A `numeric_context` meta/calibrator is therefore not trained; context is shown in
  reports but never combined into probabilities (T10). E4/E5 are BLOCKED until context assessments accumulate
  forward in shadow mode.
