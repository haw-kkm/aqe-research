"""Metadata store (B05.4, B08.3).

PostgreSQL/TimescaleDB is the target (migrations/postgres). Until a server is available the local
SQLite store implements the job-queue/registry tables with the same names and semantics
(ADR-0002). `init-db` never deletes data.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aqe.config import LocalConfig, resolve_secret
from aqe.contracts.core import iso_utc, utc_now
from aqe.storage.runs import EXIT_BLOCKED, EXIT_ERROR, EXIT_OK, RunRecord

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS context_jobs (
  job_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, instrument_id TEXT NOT NULL,
  as_of TEXT NOT NULL, bundle_id TEXT NOT NULL, backend TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_expires_at TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, result_assessment_id TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS context_jobs_status_idx ON context_jobs (status, created_at);
CREATE TABLE IF NOT EXISTS context_usage_daily (
  day TEXT NOT NULL, backend TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0, cost_unknown_calls INTEGER NOT NULL DEFAULT 0,
  reserved_usd REAL NOT NULL DEFAULT 0, budget_overruns INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, backend));
CREATE TABLE IF NOT EXISTS ingestion_health (
  source TEXT NOT NULL, instrument_id TEXT NOT NULL, observed_at TEXT NOT NULL, status TEXT NOT NULL,
  detail TEXT, clock_offset_ms REAL, PRIMARY KEY (source, instrument_id, observed_at));
CREATE TABLE IF NOT EXISTS experiment_runs (
  run_id TEXT PRIMARY KEY, experiment_id TEXT, command TEXT NOT NULL, config_hash TEXT NOT NULL,
  status TEXT NOT NULL, exit_code INTEGER, started_at TEXT NOT NULL, finished_at TEXT, body TEXT NOT NULL);
"""


def sqlite_path(cfg: LocalConfig) -> Path:
    return Path(cfg.paths.data_root) / cfg.database.sqlite_relpath


@contextmanager
def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        conn.close()


def init_sqlite(path: Path) -> None:
    with connect(path) as c:
        c.executescript(SQLITE_SCHEMA)
        c.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?, ?)", ("sqlite_001_init", iso_utc(utc_now())))
        cols = {r["name"] for r in c.execute("PRAGMA table_info(context_usage_daily)")}
        if "reserved_usd" not in cols:  # additive migration (review F04)
            c.execute("ALTER TABLE context_usage_daily ADD COLUMN reserved_usd REAL NOT NULL DEFAULT 0")
            c.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?, ?)", ("sqlite_002_budget_reservation", iso_utc(utc_now())))
        if "budget_overruns" not in cols:  # additive migration (supplement A): actual cost exceeded the reservation
            c.execute("ALTER TABLE context_usage_daily ADD COLUMN budget_overruns INTEGER NOT NULL DEFAULT 0")
            c.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?, ?)", ("sqlite_003_budget_overruns", iso_utc(utc_now())))


def init_db(cfg: LocalConfig, rec: RunRecord) -> int:
    sp = sqlite_path(cfg)
    init_sqlite(sp)
    rec.outputs["sqlite"] = str(sp)
    dsn = resolve_secret(cfg.database.postgres_dsn_env)
    if dsn is None:
        rec.notes.append(f"{cfg.database.postgres_dsn_env} not set: PostgreSQL migrations NOT applied (BLOCKED); local SQLite store initialised")
        return EXIT_BLOCKED
    try:
        import psycopg
    except ImportError:
        rec.notes.append("psycopg not installed")
        return EXIT_BLOCKED
    mig_dir = Path(__file__).resolve().parents[3] / "migrations" / "postgres"
    applied = []
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            for f in sorted(mig_dir.glob("*.sql")):
                sql = f.read_text(encoding="utf-8")
                upper = sql.upper()
                if "DROP " in upper or "TRUNCATE " in upper:
                    raise RuntimeError(f"{f.name}: destructive statement refused")
                conn.execute(sql)
                applied.append(f.name)
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        rec.notes.append(f"PostgreSQL migration failed: {type(exc).__name__}: {exc}")
        return EXIT_ERROR
    rec.outputs["postgres_migrations"] = applied
    return EXIT_OK


class JobQueue:
    """Lease-based job queue with idempotency keys (B08.3, T13). SQLite implementation."""

    def __init__(self, path: Path, lease_seconds: int = 900, max_attempts: int = 2):
        self.path = path
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        init_sqlite(path)

    def enqueue(self, idempotency_key: str, instrument_id: str, as_of: datetime, bundle_id: str, backend: str) -> dict[str, Any]:
        now = iso_utc(utc_now())
        with connect(self.path) as c:
            c.execute("INSERT OR IGNORE INTO context_jobs (job_id, idempotency_key, instrument_id, as_of, bundle_id, backend, status, attempts, created_at, updated_at) VALUES (?,?,?,?,?,?, 'PENDING', 0, ?, ?)",
                      (f"job-{uuid.uuid4().hex[:12]}", idempotency_key, instrument_id, iso_utc(as_of), bundle_id, backend, now, now))
            return dict(c.execute("SELECT * FROM context_jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone())

    def lease(self, owner: str, now: datetime | None = None) -> dict[str, Any] | None:
        now = now or utc_now()
        exp = iso_utc(now + timedelta(seconds=self.lease_seconds))
        with connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT job_id FROM context_jobs WHERE status='PENDING' OR (status='LEASED' AND lease_expires_at < ?) ORDER BY created_at LIMIT 1", (iso_utc(now),)).fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            c.execute("UPDATE context_jobs SET status='LEASED', lease_owner=?, lease_expires_at=?, attempts=attempts+1, updated_at=? WHERE job_id=?",
                      (owner, exp, iso_utc(now), row["job_id"]))
            job = dict(c.execute("SELECT * FROM context_jobs WHERE job_id=?", (row["job_id"],)).fetchone())
            c.execute("COMMIT")
            return job

    def complete(self, job_id: str, owner: str, assessment_id: str) -> bool:
        """Only the current lease holder may complete; a stale worker cannot double-complete."""
        with connect(self.path) as c:
            cur = c.execute("UPDATE context_jobs SET status='DONE', result_assessment_id=?, updated_at=?, lease_owner=NULL, lease_expires_at=NULL WHERE job_id=? AND lease_owner=? AND status='LEASED'",
                            (assessment_id, iso_utc(utc_now()), job_id, owner))
            return cur.rowcount == 1

    def fail(self, job_id: str, owner: str, error: str, terminal_status: str = "FAILED") -> str | None:
        with connect(self.path) as c:
            row = c.execute("SELECT attempts FROM context_jobs WHERE job_id=? AND lease_owner=? AND status='LEASED'", (job_id, owner)).fetchone()
            if row is None:
                return None
            status = terminal_status if row["attempts"] >= self.max_attempts else "PENDING"
            c.execute("UPDATE context_jobs SET status=?, error=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=? WHERE job_id=?",
                      (status, error[:2000], iso_utc(utc_now()), job_id))
            return status

    def get(self, job_id: str) -> dict[str, Any] | None:
        with connect(self.path) as c:
            r = c.execute("SELECT * FROM context_jobs WHERE job_id=?", (job_id,)).fetchone()
            return dict(r) if r else None

    def supersede_pending(self, instrument_id: str, backend: str, newer_as_of, keep_job_id: str) -> int:
        """Coalesce: older PENDING jobs of the same instrument/backend are superseded by a newer as_of
        (a late, stale analysis must not overwrite a newer conclusion). Leased jobs are left alone."""
        with connect(self.path) as c:
            cur = c.execute("UPDATE context_jobs SET status='SUPERSEDED', updated_at=?, error=? WHERE status='PENDING' "
                            "AND instrument_id=? AND backend=? AND as_of < ? AND job_id <> ?",
                            (iso_utc(utc_now()), f"superseded by {keep_job_id}", instrument_id, backend, iso_utc(newer_as_of), keep_job_id))
            return cur.rowcount

    def counts(self) -> dict[str, int]:
        with connect(self.path) as c:
            return {r["status"]: r["n"] for r in c.execute("SELECT status, count(*) n FROM context_jobs GROUP BY status")}

    # --- budget (B08.3, review F04): reserve before a paid call, settle after -------------------
    # Unknown cost is never recorded as 0: an unsettled/unknown call is charged at its reservation
    # and blocks further paid calls for the day until a human reconciles it.
    def usage_today(self, backend: str) -> dict[str, Any]:
        day = utc_now().strftime("%Y-%m-%d")
        with connect(self.path) as c:
            r = c.execute("SELECT * FROM context_usage_daily WHERE day=? AND backend=?", (day, backend)).fetchone()
            return dict(r) if r else {"day": day, "backend": backend, "calls": 0, "cost_usd": 0.0, "cost_unknown_calls": 0,
                                      "reserved_usd": 0.0, "budget_overruns": 0}

    @staticmethod
    def _totals(c, day: str) -> dict[str, Any]:
        r = c.execute("SELECT coalesce(sum(calls),0) calls, coalesce(sum(cost_usd),0) cost_usd, coalesce(sum(reserved_usd),0) reserved_usd, "
                      "coalesce(sum(cost_unknown_calls),0) cost_unknown_calls, coalesce(sum(budget_overruns),0) budget_overruns "
                      "FROM context_usage_daily WHERE day=?", (day,)).fetchone()
        return dict(r)

    def usage_totals_today(self) -> dict[str, Any]:
        """Project-wide totals over all context backends (the daily budget is project-wide)."""
        day = utc_now().strftime("%Y-%m-%d")
        with connect(self.path) as c:
            return {"day": day, **self._totals(c, day)}

    def try_reserve(self, backend: str, estimate_usd: float, cap_usd: float | None, max_calls: int, calls: int,
                    paid: bool) -> tuple[bool, str]:
        """Atomically check PROJECT-WIDE daily limits (all backends) and reserve. Returns (ok, reason)."""
        day = utc_now().strftime("%Y-%m-%d")
        with connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT OR IGNORE INTO context_usage_daily (day, backend) VALUES (?, ?)", (day, backend))
            u = self._totals(c, day)
            reason = ""
            if u["calls"] + calls > max_calls:
                reason = f"daily call cap {max_calls} (all backends) would be exceeded ({u['calls']} used, {calls} requested)"
            elif paid and u["cost_unknown_calls"] > 0:
                reason = f"{u['cost_unknown_calls']} paid call(s) today have unknown cost; reconcile before further paid calls"
            elif paid and u["budget_overruns"] > 0:
                reason = f"{u['budget_overruns']} job(s) today cost more than their reservation; review limits before further paid calls"
            elif paid and (cap_usd is None or cap_usd <= 0):
                reason = "max_cost_usd_per_day is 0: paid LLM calls are not permitted"
            elif paid and u["cost_usd"] + u["reserved_usd"] + estimate_usd > cap_usd:
                reason = (f"daily cost cap {cap_usd} would be exceeded: spent {u['cost_usd']:.4f} + reserved "
                          f"{u['reserved_usd']:.4f} + this job {estimate_usd:.4f}")
            if reason:
                c.execute("COMMIT")
                return False, reason
            c.execute("UPDATE context_usage_daily SET reserved_usd=reserved_usd+?, calls=calls+? WHERE day=? AND backend=?",
                      (estimate_usd if paid else 0.0, calls, day, backend))
            c.execute("COMMIT")
            return True, ""

    def settle(self, backend: str, reserved_usd: float, actual_usd: float | None, paid: bool,
               reserved_calls: int = 0, actual_calls: int | None = None) -> None:
        """Release the reservation and book the actual cost; unknown => charge the reservation and flag."""
        day = utc_now().strftime("%Y-%m-%d")
        extra_calls = max(0, (actual_calls or 0) - reserved_calls)
        with connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT OR IGNORE INTO context_usage_daily (day, backend) VALUES (?, ?)", (day, backend))
            if not paid:
                c.execute("UPDATE context_usage_daily SET calls=calls+? WHERE day=? AND backend=?", (extra_calls, day, backend))
            elif actual_usd is None:
                c.execute("UPDATE context_usage_daily SET reserved_usd=max(0, reserved_usd-?), cost_usd=cost_usd+?, "
                          "cost_unknown_calls=cost_unknown_calls+1, calls=calls+? WHERE day=? AND backend=?",
                          (reserved_usd, reserved_usd, extra_calls, day, backend))
            else:
                over = 1 if actual_usd > reserved_usd + 1e-12 else 0  # provider ignored limits / estimate wrong
                c.execute("UPDATE context_usage_daily SET reserved_usd=max(0, reserved_usd-?), cost_usd=cost_usd+?, "
                          "budget_overruns=budget_overruns+?, calls=calls+? WHERE day=? AND backend=?",
                          (reserved_usd, actual_usd, over, extra_calls, day, backend))
            c.execute("COMMIT")

    def add_usage(self, backend: str, calls: int, cost_usd: float | None) -> None:
        """Record usage that was not reserved (kept for compatibility; unknown cost is flagged, never 0)."""
        day = utc_now().strftime("%Y-%m-%d")
        with connect(self.path) as c:
            c.execute("INSERT OR IGNORE INTO context_usage_daily (day, backend) VALUES (?, ?)", (day, backend))
            if cost_usd is None:
                c.execute("UPDATE context_usage_daily SET calls=calls+?, cost_unknown_calls=cost_unknown_calls+? WHERE day=? AND backend=?", (calls, calls, day, backend))
            else:
                c.execute("UPDATE context_usage_daily SET calls=calls+?, cost_usd=cost_usd+? WHERE day=? AND backend=?", (calls, cost_usd, day, backend))
