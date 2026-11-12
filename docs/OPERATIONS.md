# Operations, recovery and handover (B14, R12)

Native Windows execution (Docker not installed; WSL present but not used). All long-running processes
are plain `aqe` commands; `deploy/windows/supervise.ps1` restarts them with exponential backoff.

## Processes
| Process | Command | Health / state files |
|---|---|---|
| Public-data collector | `deploy\windows\supervise.ps1 -Command collect` | `{data_root}/status/collector_BTCUSDT.json` (connections, book state, last message per stream, desyncs, aggTrade gaps, clock offset, disk free), `collector_BTCUSDT.checkpoint.json` |
| Context worker (LLM) | `deploy\windows\supervise.ps1 -Command context-worker` | SQLite `{data_root}/meta/aqe_meta.sqlite` tables `context_jobs`, `context_usage_daily`, `hypothesis_ledger`/`hypothesis_batches` (append-only), `context_outbox` (results with `materialized_at IS NULL` are re-written by the next worker run; ADR-0013) |
| Shadow analysis | `deploy\windows\shadow_loop.ps1 -Config configs\shadow.yaml` | `{data_root}/reports/analysis-*.json/.md`, `{data_root}/runs/` |
| Environment check | `aqe doctor --config configs/local.yaml` | `{data_root}/status/doctor_latest.json` |

## What to watch
- **Clock skew**: doctor/collector measure `local - Binance server`. On 2026-11-10 it was **+62 s** (Windows time
  service on "Local CMOS Clock"). Risk rule R2 ABSTAINs in research/shadow mode while |skew| > 1000 ms.
  Fix: Settings → Time & language → Date & time → "Set time automatically" + "Sync now", or `w32tm /resync` (admin).
  The collector still stores corrected `received_at` (local + measured offset) and the raw local clock.
- **Disk**: raw depth logs grow by hundreds of MB/day (compressed). No automatic deletion/retention exists
  (B05.4); decide retention explicitly before enabling any.
- **Source lag**: `last_message_at` per stream in the collector status; a 30 s no-data watchdog reconnects.
- **Order book**: `book_state` must be `SYNCED`; desync count rising means gaps (each forces a REST snapshot resync).
- **LLM spend**: `context_usage_daily` (calls, cost_usd, reserved_usd, cost_unknown_calls, budget_overruns). The daily
  cost cap and call cap are **project-wide** (summed over all context backends). Paid calls are refused when
  `max_cost_usd_per_day` is 0, no price table is configured, no usage ledger exists, the prompt upper bound exceeds
  `max_prompt_tokens_per_call`, the reservation would exceed the cap, or any paid call that day has unknown cost or
  cost more than its reservation (ADR-0008).
  Reconciling an unknown-cost day: check the provider's billing page, then record the confirmed amount, e.g.
  `sqlite3 E:\aqe-v2\data\meta\aqe_meta.sqlite "UPDATE context_usage_daily SET cost_usd=<confirmed>, cost_unknown_calls=0 WHERE day='YYYY-MM-DD' AND backend='tradingagents'"`.
  Never set it to 0 without a confirmed amount. After an overrun, check why the provider exceeded the requested
  limits before clearing `budget_overruns`.
- **Model/prompt versions**: every artifact has `metadata.json` (dataset manifest hash, config hash, code version,
  dependency versions, seed, readiness history); TradingAgents runs record upstream commit.

## Restart / recovery behaviour
- Raw logs: active file `*.jsonl.gz.open`; after a crash it is renamed to `*.recovered.jsonl.gz` at next start and is
  readable up to the last complete block (test: `test_raw_log_recovers_after_crash`).
- Collector: checkpoint is written atomically; restarts dedup aggTrade ids and re-sync the book from a fresh snapshot
  (book features are invalid until SYNCED). CVD restarts from zero with an explicit reset reason.
- Backfill is idempotent: monthly Parquet partitions are upserted on `(instrument_id, open_time)`; reruns resume after
  the newest closed bar.
- Context jobs: lease expiry → another worker reclaims; only the current lease holder can complete; enqueue is
  idempotent by cache key (`test_job_queue_lease_expiry_and_no_double_completion`).
- Experiments: `aqe evaluate` appends to `{artifact_root}/<exp>/evaluation/ledger.jsonl` with a trial counter for the
  locked test segment; re-running training after calibration invalidates calibrators (evaluate refuses mismatched
  calibrators).

## Backup
- Copy `{data_root}/raw`, `{data_root}/normalized`, `{data_root}/manifests`, `{data_root}/meta` and
  `{artifact_root}` (e.g. `robocopy E:\aqe-v2 <backup> /E /XO`). SQLite: copy while the worker is stopped or use
  `sqlite3 .backup`.
- Restore = copy back; manifests carry SHA-256 of every Parquet partition for verification.

## Secrets
Only environment variables (`.env.example`). Never commit `.env`. No exchange API keys are used or needed —
the project contains no order client (T14).

## Trade-plan candidates (docs/TRADE_PLAN.md, ADR-0014)
- Off by default (`planning.enabled: false`). Dev-only config: `configs/dev_trade_plan_offline.yaml` (copied snapshot
  `E:\aqe-v2-dev`, separate queue/ledger DB `meta/aqe_meta_trade_plan.sqlite`, scripted LLM).
- `aqe analyze` adds `trade_plans` (JSON) and section `4b` (Markdown). Every plan is advisory: approval_required=true,
  execution_enabled=false; the decision is WAIT unless the risk disposition is REVIEW_CANDIDATE and a committed
  real-LLM selection passes every check. Tick sizes come only from `{data_root}/imports/instrument_specs/` (none yet →
  `tick_size=UNKNOWN`).
