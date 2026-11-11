"""Context worker (`aqe context-worker`, `aqe context-enqueue`) — B08.3.

Slow LLM work runs here, separate from collection and numeric inference. Jobs are leased with
expiry, keyed by an idempotency key (= context cache key) and completed only by the lease holder.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime

import pandas as pd

from aqe.config import LocalConfig, config_hash
from aqe.context.backends import context_cache_key, make_backend, missing, prompt_hash
from aqe.context.evidence import build_bundle
from aqe.context.store import assessments_dir, load_bundle, save_assessment, save_bundle
from aqe.contracts.context import ContextStatus, validate_against_bundle
from aqe.contracts.core import RuntimeMode, iso_utc, utc_now
from aqe.storage.bars import read_bars
from aqe.storage.metadb import JobQueue, sqlite_path
from aqe.storage.runs import EXIT_BLOCKED, EXIT_OK, RunRecord


def _queue(cfg: LocalConfig) -> JobQueue:
    return JobQueue(sqlite_path(cfg), cfg.context.lease_seconds, cfg.context.max_attempts)


def enqueue_for_asof(cfg: LocalConfig, rec: RunRecord, as_of: datetime | None, run_id: str = "live") -> int:
    inst = cfg.instruments[0]
    if as_of is None:
        if cfg.mode == RuntimeMode.REPLAY:
            bars = read_bars(cfg.paths.data_root, inst, "5m")
            as_of = (bars["close_time"].max() + pd.Timedelta(seconds=cfg.analysis.decision_delay_seconds)).to_pydatetime()
        else:
            as_of = utc_now()
    bundle = build_bundle(cfg, inst, as_of, include_numeric=cfg.context.backend == "aqe_debate")
    save_bundle(cfg.paths.data_root, bundle)
    llm = cfg.context.llm
    key = context_cache_key(instrument_slug=inst.slug(), as_of=as_of, bundle_hash=bundle.bundle_hash, backend=cfg.context.backend,
                            upstream_ref=cfg.context.tradingagents.upstream_sha if cfg.context.backend == "tradingagents" else None,
                            provider=llm.provider, model_id=llm.model_id, prompt_h=prompt_hash(cfg.context.backend),
                            config_h=config_hash(cfg.context), run_id=run_id, portfolio_hash=None)
    q = _queue(cfg)
    job = q.enqueue(key, inst.instrument_id, as_of, bundle.bundle_id, cfg.context.backend)
    superseded = q.supersede_pending(inst.instrument_id, cfg.context.backend, as_of, job["job_id"])
    rec.outputs.update({"job_id": job["job_id"], "job_status": job["status"], "bundle_id": bundle.bundle_id,
                        "superseded_older_pending": superseded,
                        "bundle_items": len(bundle.items), "source_health": {h.source_kind.value: h.status.value for h in bundle.source_health},
                        "as_of": iso_utc(as_of)})
    return EXIT_OK


def process_one(cfg: LocalConfig, queue: JobQueue, backend, owner: str) -> dict | None:
    job = queue.lease(owner)
    if job is None:
        return None
    bundle = load_bundle(cfg.paths.data_root, job["bundle_id"])
    if bundle is None:
        queue.fail(job["job_id"], owner, "bundle missing", terminal_status="FAILED")
        return {"job_id": job["job_id"], "result": "bundle missing"}
    try:
        a = backend.assess(bundle, job)
    except Exception as exc:  # noqa: BLE001 - surfaced as INVALID, never as neutral
        a = missing(bundle, backend.name, ContextStatus.INVALID, f"backend error {type(exc).__name__}: {exc}", cfg.context.ttl_seconds)
    a = validate_against_bundle(a, bundle)
    staging = assessments_dir(cfg.paths.data_root, bundle.instruments[0]).parent / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    commit = getattr(backend, "commit", None)  # two-phase backends (aqe_debate): side effects only while the lease is held
    if commit is not None:
        try:
            held, a = commit(a, job, owner, queue)
        except Exception as exc:  # noqa: BLE001 - nothing completed; the job is retried or failed, never half-done
            queue.fail(job["job_id"], owner, f"commit error {type(exc).__name__}: {exc}")
            return {"job_id": job["job_id"], "result": f"commit failed: {type(exc).__name__}: {exc}"[:300]}
    else:
        held = queue.complete(job["job_id"], owner, a.assessment_id)
    if held:
        p = save_assessment(cfg.paths.data_root, a)
        return {"job_id": job["job_id"], "assessment_id": a.assessment_id, "status": a.status.value,
                "temporal_scope": a.temporal_scope.value, "path": str(p)}
    return {"job_id": job["job_id"], "result": "lease lost; result discarded (no double completion)"}


def run_worker(cfg: LocalConfig, rec: RunRecord, once: bool = False, max_jobs: int | None = None) -> int:
    queue = _queue(cfg)
    backend = make_backend(cfg, queue)
    if backend is None:
        rec.notes.append("context.backend is 'none': nothing to run")
        return EXIT_BLOCKED
    owner = f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    done = []
    while True:
        r = process_one(cfg, queue, backend, owner)
        if r is None:
            if once:
                break
            time.sleep(5)
            continue
        done.append(r)
        print(r, flush=True)
        if max_jobs and len(done) >= max_jobs:
            break
    rec.outputs.update({"processed": done, "queue": queue.counts(), "owner": owner})
    return EXIT_OK
