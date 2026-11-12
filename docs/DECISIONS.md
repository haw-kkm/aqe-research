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
  Such a replay is a conservative re-run under today's usage restrictions, NOT an evaluation of what a historical
  trading policy could have known; historical policy performance must only use knowledge available at each as_of.

## ADR-0008 — LLM budget: reserve, settle, block on unknown (review F04)
- Before a paid job: atomically reserve calls × (max_prompt_tokens + max_output_tokens) × price table.
- After: settle with the actual cost; if unknown (missing usage, timeout, failure after send) the reservation is
  booked as cost and `cost_unknown_calls` blocks further paid calls that day until a human reconciles
  (docs/OPERATIONS.md). `ollama`/`openai_compatible` count as free only on a loopback base URL.

- Follow-up (supplement A): the reservation is a true bound only if the prompt bound is checked before every call
  (UTF-8 bytes + per-message overhead; TA callback per graph call), the output cap is sent to the provider,
  hidden client retries are disabled (`llm_max_retries=0`), and the daily cap/call cap/blocks are project-wide
  across backends. Actual > reserved => `budget_overruns` blocks further paid calls that day.

## ADR-0010 — Evidence imports need a verifiable manifest (supplement B)
- A calendar/news file counts only with a manifest (fetched/available time, coverage interval, content SHA-256)
  available at as_of. Coverage = manifest interval (never as_of + lookahead). See docs/IMPORT_FORMATS.md.

## ADR-0009 — Unknown event information is not 'no event' (review F05)
- Event risk passes only with AVAILABLE `event_required_sources` (MACRO_CALENDAR, NEWS), an
  INTRADAY_ASOF_VERIFIED assessment and a verified calendar window covering the horizon. Otherwise UNKNOWN
  (caps at RESEARCH_ONLY). A verified empty calendar is a real PASS.

## ADR-0006 — No numeric_context meta yet
- No historical, point-in-time structured context assessments exist (news receipt times are not recoverable,
  no LLM provider configured). A `numeric_context` meta/calibrator is therefore not trained; context is shown in
  reports but never combined into probabilities (T10). E4/E5 are BLOCKED until context assessments accumulate
  forward in shadow mode.

## ADR-0011 — Analysis/debate layer as a separate LangGraph graph (debate proposal v0.1)
- Context: the proposal asks for specialists → critic → one revision → synthesis → code checks → hypothesis history,
  reusing the existing TradingAgents integration where possible. The pinned TradingAgents graph (analysts → Bull/Bear →
  research manager → trader → risk debate → PM) has no critic/revision/hypothesis-lifecycle nodes and a different role set.
- Decision: keep the upstream graph untouched as backend `tradingagents` (E5). Add backend `aqe_debate`: a small
  LangGraph StateGraph in the worker venv (`src/aqe/context/debate_worker.py`) reusing LangGraph, TradingAgents'
  `create_llm_client` factory and the ta_bridge isolation/budget helpers. Core (`src/aqe/context/debate.py`) builds
  prompts and role evidence slices, runs code checks, maintains the append-only hypothesis ledger and projects the
  synthesis onto ContextAssessment. This is "role ideas + LangGraph + LLM factory reused", not "full TradingAgents integration".
- Limits: max 8 LLM calls per job; roles without verified evidence are NOT_ACTIVATED; the layer never writes
  probabilities, never sets contextual_rating, cannot lift risk restrictions; real-LLM runs follow ADR-0008.

## ADR-0012 — Hypothesis ledger: plan → commit under lease and version checks (debate review 2026-11-12, D01–D05)
- Context: `aqe_debate` appended ledger rows inside `assess`, using the ledger read BEFORE the LLM run and BEFORE the
  worker checked its lease. A slow run could re-activate a hypothesis that a newer run had invalidated (D01); a worker
  that had lost its lease still wrote rows (D02). REVISE could link different horizons (D03), duplicate hypothesis ids
  were remapped across horizons (D04), and parallel drafts all passed the call cap on the same state snapshot (D05).
- Decision (two phases): `assess` only PLANS rows (`plan_ledger`, pure) and returns a candidate assessment; the rows
  change the ledger only in `DebateBackend.commit` → `commit_ledger`, which takes a per-instrument ledger lock (SQLite
  write lock next to the JSONL) and re-reads the ledger, refusing when (a) rows of the same job already exist (retry),
  (b) the ledger holds a newer as_of (late result), (c) the ledger version (row count + last record id) differs from
  the one the debate read (conflict). In the worker path `commit` runs inside `JobQueue.complete_with`: the lease is
  verified, the ledger is committed and the job is marked DONE in one queue transaction (lock order queue → ledger),
  after the common `validate_against_bundle`. `states_at` orders by as_of, not by file position (defence in depth).
- Refused results: the DebateRecord stays as an audit record (`applied_to_ledger=false` + reason); the assessment
  carries `hypothesis ledger: <note>` and its claims do not show canonical ids that were never written. A conflicting
  run is not retried automatically — the next enqueue runs on the current ledger.
- Crash windows: crash before the queue COMMIT → job stays LEASED, lease expires, job re-runs; rows the job already
  appended (tagged `job_id`) are not applied twice. Crash after the queue COMMIT but before the assessment/record save →
  job DONE and ledger applied, record still says "pending commit" (rows traceable via debate_id/job_id). A torn final
  JSONL line fails closed on load (manual inspection; never skipped silently).
- Identity: synthesis hypothesis ids must be unique and must not reuse prior ids; one decision per prior; a REVISE
  replacement must have the prior's instrument and horizon (another horizon is a new question). `DebateRecord.
  hypothesis_id_map` is the single LLM-id → canonical-id map used by ledger rows, projected claims and the report.
- Call cap: the worker takes a slot from one lock-protected counter before every model invocation (never refunded,
  failed calls included); core refuses before reserving budget when `max_llm_calls < active roles + 2`.

## ADR-0013 — Ledger, result outbox and job completion in one SQLite transaction (re-review 4304c5f, R01/R02)
- Context: ADR-0012 appended JSONL rows inside the queue transaction, but a file append does not roll back with SQLite.
  An I/O error after some complete rows left a partial batch that the retry then treated as "already applied" (R01).
  Files written after the queue COMMIT (debate record, assessment) could fail with the job already DONE and no path
  to recover them (R02).
- Decision: the hypothesis ledger moves into the queue database (`sqlite_path(cfg)`): `hypothesis_ledger` (rows, in
  commit order) + `hypothesis_batches` (one per commit: job_id UNIQUE, row_count, sha256 of the rows). UPDATE/DELETE
  are refused by triggers. `DebateBackend.commit` runs inside `JobQueue.complete_with(on_commit(conn))`, which in ONE
  transaction checks the lease, re-validates the ledger (ADR-0012), inserts the whole batch, stores the FINAL
  assessment + final DebateRecord in `context_outbox` and marks the job DONE. Any exception rolls everything back, so a
  batch is all-or-nothing and the job is retried as a whole. `load_ledger` refuses a batch whose row count does not
  match (LedgerIntegrityError); `verify_ledger` re-checks the hashes.
- Files are written only from the outbox (`context/store.materialize_outbox`): audit files first, the assessment last
  (a reader never sees an assessment without its record), then `materialized_at` is set. If writing fails, the entry
  stays pending (error recorded) and every `process_one` call first re-writes pending entries — the LLM is not re-run
  and the job is not reopened. Every backend's result now goes through the outbox (not only `aqe_debate`).
- Supersedes the storage part of ADR-0012 (JSONL ledger + ledger lock file). Legacy `context/hypotheses/*.jsonl`
  (dev-only scripted runs) are not read or migrated. The PostgreSQL target schema gets the same tables
  (`migrations/postgres/002_hypothesis_ledger_outbox.sql`, not executed — no server).
- Residual: a crash between materialization and `materialized_at` re-writes identical files (atomic, idempotent).
  The pre-commit DebateRecord written in `finish` stays "pending commit" if the job is never committed — that is true.

## ADR-0014 — Trade-plan candidates: code computes prices, the LLM only selects, risk gates the decision (2026-11-12)
- Context: reports should show a reviewable plan per horizon (long/short/wait, entry zone, stop, take-profit, reasons,
  invalidation) without letting an LLM invent prices and without loosening RESEARCH_ONLY/ABSTAIN. Simulation, orders,
  sizing and new models are out of scope.
- Decision: (1) `planning/candidates.py` (`swing_atr_v0`, pure) computes at most one LONG and one SHORT candidate per
  horizon from completed bars only — confirmed swing levels (`confirmed_at` = availability of the last right-side bar),
  ATR buffers, intact opposite levels as targets, tick rounding away from the favourable side when a tick import is
  available at as_of, net reward/loss/RR from the conservative zone end via the existing `trade_return` (fees + adverse
  slippage both legs, funding excluded). Missing inputs give no candidate or UNKNOWN checks — never filled-in values.
  (2) The snapshot is computed before the debate and stored in `EvidenceBundle.plan_candidates` (hashed only when
  present, so old bundle hashes are unchanged; content/config changes change the bundle hash and the cache key).
  (3) The synthesis may return `plan_selection` = one listed id or null inside its existing call (no extra LLM call);
  `planning/selection.py` rejects unknown/foreign/other-horizon/expired/unselectable ids, extra fields (prices),
  invented price-like numbers in the reason and selections that appear only after the format repair. A rejected
  selection is never replaced by another candidate. (4) Verdicts are stored in `DebateRecord.plan_selections` and
  finalized inside the ADR-0013 commit transaction: not applied (late, stale ledger, lease lost, invalid) →
  NOT_COMMITTED. (5) `planning/plan.py` builds `TradePlan` at analysis time from the committed selection, the SAME
  snapshot (by id) and the independent risk decision; the `TradePlan` contract allows LONG/SHORT only with
  REVIEW_CANDIDATE, an ACCEPTED real-LLM selection, all checks PASS and all entry conditions MET; otherwise WAIT with
  reasons. approval_required=true, execution_enabled=false always; no account fields exist.
- Also fixed: the order-book summary read `bid_depth_10bps_quote` while the collector writes `depth_bid_10bps_quote`;
  it now uses the collector names, book metrics only from `book_valid` rows, trade sums only from gap-free intervals,
  the as_of hour partitions (not "the last 30 files") and reports STALE instead of old values.
- Off by default (`planning.enabled: false`); with it off, bundles, prompts (byte-identical) and reports are unchanged.
  Defaults are explicit development values, not tuned on the locked segment. offline_scripted selections stay
  excluded from analysis and are labelled SCRIPTED_TEST. Not done: G01 live-bar merge, tick import source, real LLM,
  scoring of plan outcomes, simulation.

## ADR-0015 — One codebase, one verified commit, role-separated cloud hosts (2026-11-12, branch dev/cloud-deploy)
- Context: the Windows PC runs the collector from `main`; development continues in `aqe-dev`. Target: Oracle ARM64
  (24/7 collection + light inference) and Google x86-64 (on-demand training), without separate projects, without
  touching the running collector or its data, and without letting unverified dev work reach a server.
- Decision: (1) servers install exactly one 40-hex commit (`deploy/linux/install_release.sh`; branch names refused) after
  `python -m aqe.ops code-release-check` (clean tree, tests) and run tests + role preflight on the target before switching
  `/opt/aqe/current`; services are never restarted automatically. (2) Roles differ only by config/units:
  `configs/cloud/{oracle_collector,oracle_shadow,google_cpu_train,google_gpu_train}.yaml` with `${VAR:-default}` paths;
  cloud experiment configs equal `experiment.yaml` except `base_config` (test-enforced); per-platform requirement files
  (`requirements/`), the Windows CUDA lock is not used on servers; wheel availability checked from index metadata.
  (3) Data moves only through `aqe.ops.transfer`: dry-run default, SHA-256 per file, resumable, never overwrites
  (different content -> quarantined conflict), live/host-local files excluded, journal + receipt + volume log, rollback
  moves (never deletes). SQLite sources are never opened by SQLite (a read-only WAL open creates sidecars): copy, then
  backup API + integrity_check on the copy. Manifests are copied verbatim and verified by path rebasing (their hash is
  lineage). (4) Models move as releases: build (Google) -> stage (Oracle) -> verify (integrity, OOS fold leakage,
  inference smoke through `predict_horizon` + risk engine on that host) -> activate only with `--approve <id>` ->
  registry rollback; artifacts are never overwritten or deleted. (5) Linux collector: systemd unit with KillMode=mixed +
  KillSignal=SIGINT and SIGTERM mapped to KeyboardInterrupt (flush + checkpoint), preflight (time sync, Binance clock,
  single collector) as ExecStartPre, 5-minute healthcheck; quality events (book desync/resync, disconnects, restarts)
  are now journaled append-only under `{data_root}/quality/`. (6) PC -> Oracle cutover only after a GO comparison
  (coverage/book/trades/health over >= 24 h), a complete migration receipt and an explicit recorded approval; the
  operator stops the PC collector manually.
- Not done / limits: nothing ran on ARM64, Linux, a GPU or a real SSH link; Google training has stage-level resume
  only (no epoch checkpoints) -> Spot VMs not recommended yet; G01 (live-bar merge) still open; no trading path exists.
- Addendum (collector migration, 2026-11-12): `transfer --mode mirror` for downstream replicas (Oracle -> Google)
  replaces a file only when the destination still holds the exact version it last received from the same source
  (`_transfer/received_index.json`), keeping the old version; `safe` stays the default (Windows -> Oracle).
  Overlapping collector partitions (PC and Oracle both collected the same hour) are unioned explicitly with
  `transfer-merge`: collector keys, destination rows win, backup + verification, live files deferred. One data store
  has one collecting host: `status/collector_owner_<symbol>.json` (host-local) makes the supervisor refuse a second
  host. Parquet transfers are checked for readability, schema and partition period; files being written (`*.open`,
  recent partitions) wait for the next incremental pass instead of being copied mid-write. A collector-only
  requirements file (`requirements/linux-arm64-collector.txt`, no ML libraries) is supported because the collect path
  imports none (test). Oracle may run a daily REST backfill timer so Google syncs one data source.
