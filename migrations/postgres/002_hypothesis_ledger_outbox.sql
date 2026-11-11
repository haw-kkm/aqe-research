-- ADR-0013: hypothesis ledger (append-only) + result outbox, same names/semantics as the SQLite store (ADR-0002).
-- NOT EXECUTED: no PostgreSQL server is available yet; kept in sync with src/aqe/storage/metadb.py SQLITE_SCHEMA.
CREATE TABLE IF NOT EXISTS hypothesis_ledger (
  seq BIGSERIAL PRIMARY KEY, record_id TEXT NOT NULL UNIQUE, batch_id TEXT NOT NULL, instrument_slug TEXT NOT NULL,
  hypothesis_id TEXT NOT NULL, as_of TIMESTAMPTZ NOT NULL, record_json JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS hypothesis_ledger_inst_idx ON hypothesis_ledger (instrument_slug, seq);
CREATE TABLE IF NOT EXISTS hypothesis_batches (
  batch_id TEXT PRIMARY KEY, instrument_slug TEXT NOT NULL, job_id TEXT UNIQUE, debate_id TEXT NOT NULL, assessment_id TEXT,
  as_of TIMESTAMPTZ NOT NULL, base_version TEXT NOT NULL, row_count INT NOT NULL, rows_sha256 TEXT NOT NULL,
  committed_at TIMESTAMPTZ NOT NULL
);
CREATE OR REPLACE FUNCTION aqe_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS hypothesis_ledger_append_only ON hypothesis_ledger;
CREATE TRIGGER hypothesis_ledger_append_only BEFORE UPDATE OR DELETE ON hypothesis_ledger
  FOR EACH ROW EXECUTE FUNCTION aqe_append_only();
DROP TRIGGER IF EXISTS hypothesis_batches_append_only ON hypothesis_batches;
CREATE TRIGGER hypothesis_batches_append_only BEFORE UPDATE OR DELETE ON hypothesis_batches
  FOR EACH ROW EXECUTE FUNCTION aqe_append_only();
CREATE TABLE IF NOT EXISTS context_outbox (
  assessment_id TEXT PRIMARY KEY, job_id TEXT, assessment_json JSONB NOT NULL, files_json JSONB NOT NULL DEFAULT '[]',
  committed_at TIMESTAMPTZ NOT NULL, materialized_at TIMESTAMPTZ, attempts INT NOT NULL DEFAULT 0, last_error TEXT
);
CREATE INDEX IF NOT EXISTS context_outbox_pending_idx ON context_outbox (materialized_at, committed_at);
