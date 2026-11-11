"""File-backed store for EvidenceBundles and ContextAssessments (B08).

Layout: {data_root}/context/bundles/{bundle_id}.json
        {data_root}/context/assessments/{instrument_slug}/{assessment_id}.json
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from aqe.contracts.context import ContextAssessment, EvidenceBundle, validate_against_bundle
from aqe.contracts.core import InstrumentKey
from aqe.storage import atomic_write_json, read_json


def bundles_dir(data_root: Path) -> Path:
    return Path(data_root) / "context" / "bundles"


def assessments_dir(data_root: Path, inst: InstrumentKey) -> Path:
    return Path(data_root) / "context" / "assessments" / inst.slug()


def save_bundle(data_root: Path, bundle: EvidenceBundle) -> Path:
    p = bundles_dir(data_root) / f"{bundle.bundle_id}.json"
    if p.exists():
        existing = EvidenceBundle.model_validate(read_json(p))
        if existing.bundle_hash != bundle.bundle_hash:
            raise ValueError(f"bundle {bundle.bundle_id} already stored with different content (bundles are immutable)")
        return p
    atomic_write_json(p, bundle.model_dump(mode="json"))
    return p


def load_bundle(data_root: Path, bundle_id: str) -> EvidenceBundle | None:
    p = bundles_dir(data_root) / f"{bundle_id}.json"
    if not p.exists():
        return None
    return EvidenceBundle.model_validate(read_json(p))


def save_assessment(data_root: Path, a: ContextAssessment) -> Path:
    p = assessments_dir(data_root, a.instrument) / f"{a.assessment_id}.json"
    atomic_write_json(p, a.model_dump(mode="json"))
    return p


def materialize_outbox(data_root: Path, queue, ids: list[str] | None = None) -> tuple[list[str], dict[str, str]]:
    """Write committed results from the queue DB outbox to files (ADR-0013). Idempotent (atomic overwrites).

    Audit files (e.g. the debate record) are written first and the assessment last, so a reader never sees an
    assessment whose audit record is missing. On failure the entry stays pending (error recorded) and the next worker
    run writes it again from the stored result — the LLM is never re-run. Returns (materialized ids, {id: error})."""
    done: list[str] = []
    errors: dict[str, str] = {}
    for row in queue.outbox_pending(ids):
        aid = row["assessment_id"]
        try:
            for f in json.loads(row["files_json"] or "[]"):
                atomic_write_json(Path(data_root) / f["relpath"], f["json"])
            save_assessment(data_root, ContextAssessment.model_validate_json(row["assessment_json"]))
            queue.outbox_mark(aid, None)
            done.append(aid)
        except Exception as exc:  # noqa: BLE001 - stays pending; retried by the next worker run
            errors[aid] = f"{type(exc).__name__}: {exc}"[:500]
            try:
                queue.outbox_mark(aid, errors[aid])
            except Exception:  # noqa: BLE001 - the pending state itself is the durable signal
                pass
    return done, errors


def ready_time(a: ContextAssessment, replay: bool) -> datetime:
    """When the assessment could first be used. Replay uses as_of + measured latency (never earlier)."""
    if replay:
        lat = a.usage.latency_seconds
        return a.as_of + timedelta(seconds=lat if lat is not None else 10**9)
    return a.generated_at


def latest_assessment(data_root: Path, inst: InstrumentKey, as_of: datetime, decision_time: datetime,
                      replay: bool | None = None, backend: str | None = None) -> ContextAssessment | None:
    """Latest usable assessment of the configured backend. Offline-scripted/fixture outputs are
    contract-test artefacts and are never used for analysis."""
    d = assessments_dir(data_root, inst)
    if not d.exists() or backend in (None, "none", "offline_fixture"):
        return None
    if replay is None:
        replay = decision_time < datetime.now(decision_time.tzinfo) - timedelta(minutes=10)
    best: ContextAssessment | None = None
    for p in d.glob("*.json"):
        a = ContextAssessment.model_validate(read_json(p))
        if a.backend != backend or a.provider == "offline_scripted":
            continue
        if a.as_of > as_of or a.valid_until <= decision_time or ready_time(a, replay) > decision_time:
            continue
        if best is None or a.as_of > best.as_of:
            best = a
    if best is None:
        return None
    bundle = load_bundle(data_root, best.bundle_id) if best.bundle_id else None
    return validate_against_bundle(best, bundle)
