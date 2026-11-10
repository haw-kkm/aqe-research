-- AQE PostgreSQL/TimescaleDB schema v1 (B05.4). Additive only: no DROP/TRUNCATE.
-- Applied by `aqe init-db` when AQE_PG_DSN is set. NOT yet executed against a real server
-- in this build (no PostgreSQL available locally) — see BUILD_STATUS.md.
CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS ingestion_health (
  source TEXT NOT NULL, instrument_id TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL, detail JSONB, clock_offset_ms DOUBLE PRECISION,
  PRIMARY KEY (source, instrument_id, observed_at)
);

CREATE TABLE IF NOT EXISTS market_bars (
  venue TEXT NOT NULL, market_type TEXT NOT NULL, instrument_id TEXT NOT NULL, interval_seconds INT NOT NULL,
  open_time TIMESTAMPTZ NOT NULL, close_time TIMESTAMPTZ NOT NULL,
  open DOUBLE PRECISION, high DOUBLE PRECISION, low DOUBLE PRECISION, close DOUBLE PRECISION,
  volume DOUBLE PRECISION, quote_volume DOUBLE PRECISION, trade_count BIGINT,
  taker_buy_base DOUBLE PRECISION, taker_buy_quote DOUBLE PRECISION, is_closed BOOLEAN NOT NULL,
  provenance TEXT NOT NULL, received_at TIMESTAMPTZ NOT NULL, available_at TIMESTAMPTZ NOT NULL,
  source_id BIGINT NOT NULL, ingest_run_id TEXT,
  PRIMARY KEY (venue, market_type, instrument_id, interval_seconds, open_time, available_at)
);

CREATE TABLE IF NOT EXISTS flow_summaries (
  venue TEXT NOT NULL, market_type TEXT NOT NULL, instrument_id TEXT NOT NULL,
  window_start TIMESTAMPTZ NOT NULL, window_end TIMESTAMPTZ NOT NULL,
  book_state TEXT NOT NULL, spread_bps DOUBLE PRECISION, depth_10bps_quote DOUBLE PRECISION,
  imbalance_top10 DOUBLE PRECISION, buy_base DOUBLE PRECISION, sell_base DOUBLE PRECISION,
  cvd_delta_base DOUBLE PRECISION, trade_count BIGINT, agg_trade_gaps BIGINT,
  provenance TEXT NOT NULL, available_at TIMESTAMPTZ NOT NULL, detail JSONB,
  PRIMARY KEY (venue, market_type, instrument_id, window_start)
);

CREATE TABLE IF NOT EXISTS oi_observations (
  venue TEXT NOT NULL, market_type TEXT NOT NULL, instrument_id TEXT NOT NULL,
  observed_time TIMESTAMPTZ NOT NULL, kind TEXT NOT NULL, -- CURRENT_POLL | HIST_STAT_5M
  open_interest_base DOUBLE PRECISION, open_interest_value_quote DOUBLE PRECISION,
  provenance TEXT NOT NULL, received_at TIMESTAMPTZ NOT NULL, available_at TIMESTAMPTZ NOT NULL, source_id BIGINT NOT NULL,
  PRIMARY KEY (venue, market_type, instrument_id, kind, observed_time, source_id)
);

CREATE TABLE IF NOT EXISTS funding_observations (
  venue TEXT NOT NULL, market_type TEXT NOT NULL, instrument_id TEXT NOT NULL,
  funding_time TIMESTAMPTZ NOT NULL, kind TEXT NOT NULL, -- SETTLED | ESTIMATED
  funding_rate DOUBLE PRECISION NOT NULL, mark_price DOUBLE PRECISION,
  provenance TEXT NOT NULL, received_at TIMESTAMPTZ NOT NULL, available_at TIMESTAMPTZ NOT NULL, source_id BIGINT NOT NULL,
  PRIMARY KEY (venue, market_type, instrument_id, kind, funding_time, source_id)
);

CREATE TABLE IF NOT EXISTS evidence_versions (
  evidence_id TEXT NOT NULL, revision INT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
  source_url TEXT, content_hash TEXT NOT NULL, content_ref TEXT,
  event_time TIMESTAMPTZ, published_at TIMESTAMPTZ, received_at TIMESTAMPTZ NOT NULL, available_at TIMESTAMPTZ NOT NULL,
  verification_status TEXT NOT NULL, supersedes TEXT,
  PRIMARY KEY (evidence_id, revision)
);

CREATE TABLE IF NOT EXISTS scheduled_event_versions (
  event_id TEXT NOT NULL, revision INT NOT NULL, name TEXT NOT NULL, source_id TEXT NOT NULL,
  scheduled_time TIMESTAMPTZ NOT NULL, importance TEXT NOT NULL, available_at TIMESTAMPTZ NOT NULL,
  actual_value TEXT, actual_available_at TIMESTAMPTZ,
  PRIMARY KEY (event_id, revision)
);

CREATE TABLE IF NOT EXISTS context_jobs (
  job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, instrument_id TEXT NOT NULL,
  as_of TIMESTAMPTZ NOT NULL, bundle_id TEXT NOT NULL, backend TEXT NOT NULL, status TEXT NOT NULL,
  attempts INT NOT NULL DEFAULT 0, lease_owner TEXT, lease_expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL, result_assessment_id TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS context_jobs_status_idx ON context_jobs (status, created_at);

CREATE TABLE IF NOT EXISTS context_assessments (
  assessment_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, as_of TIMESTAMPTZ NOT NULL, backend TEXT NOT NULL,
  status TEXT NOT NULL, temporal_scope TEXT NOT NULL, bundle_hash TEXT, cache_key TEXT,
  generated_at TIMESTAMPTZ NOT NULL, valid_until TIMESTAMPTZ NOT NULL, body JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS model_registry (
  artifact_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, kind TEXT NOT NULL, horizon_seconds INT NOT NULL,
  feature_profile TEXT NOT NULL, readiness TEXT NOT NULL, train_label_cutoff TIMESTAMPTZ, metadata JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS predictions (
  prediction_id TEXT PRIMARY KEY, instrument_id TEXT NOT NULL, as_of TIMESTAMPTZ NOT NULL, horizon_seconds INT NOT NULL,
  feature_profile TEXT NOT NULL, model_kind TEXT NOT NULL, artifact_id TEXT, status TEXT NOT NULL,
  p_down DOUBLE PRECISION, p_neutral DOUBLE PRECISION, p_up DOUBLE PRECISION,
  generated_at TIMESTAMPTZ NOT NULL, ready_at TIMESTAMPTZ NOT NULL, valid_until TIMESTAMPTZ NOT NULL, body JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_decisions (
  analysis_id TEXT NOT NULL, horizon_seconds INT NOT NULL, ruleset_hash TEXT NOT NULL, risk_outcome TEXT NOT NULL,
  disposition TEXT NOT NULL, decided_at TIMESTAMPTZ NOT NULL, body JSONB NOT NULL,
  approval_required BOOLEAN NOT NULL CHECK (approval_required),
  execution_enabled BOOLEAN NOT NULL CHECK (NOT execution_enabled),
  PRIMARY KEY (analysis_id, horizon_seconds)
);

CREATE TABLE IF NOT EXISTS experiment_runs (
  run_id TEXT PRIMARY KEY, experiment_id TEXT, command TEXT NOT NULL, config_hash TEXT NOT NULL,
  status TEXT NOT NULL, exit_code INT, started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ, body JSONB NOT NULL
);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    PERFORM create_hypertable('market_bars', 'open_time', if_not_exists => TRUE, migrate_data => TRUE);
    PERFORM create_hypertable('flow_summaries', 'window_start', if_not_exists => TRUE, migrate_data => TRUE);
  END IF;
END $$;

INSERT INTO schema_migrations (version) VALUES ('001_init') ON CONFLICT DO NOTHING;
