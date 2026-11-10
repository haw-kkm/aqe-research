"""Evidence and context contracts (B06, B08): EvidenceItem, EvidenceBundle, ContextAssessment.

The contextual rating from an LLM backend is *not* a probability and is never converted into one.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import Field, model_validator

from aqe.contracts.core import InstrumentKey, StrictModel, UTCDateTime, iso_utc


class SourceKind(StrEnum):
    MARKET_FLOW = "MARKET_FLOW"
    NEWS = "NEWS"
    SOCIAL = "SOCIAL"
    MACRO_CALENDAR = "MACRO_CALENDAR"
    EARNINGS_CALENDAR = "EARNINGS_CALENDAR"
    CROSS_ASSET = "CROSS_ASSET"
    ONCHAIN = "ONCHAIN"
    CRYPTO_FUNDAMENTAL = "CRYPTO_FUNDAMENTAL"


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"  # primary source / exchange data
    REPORTED = "REPORTED"  # reputable outlet, not independently verified
    UNVERIFIED = "UNVERIFIED"  # social claim, rumor
    RETRACTED = "RETRACTED"


class SourceHealthStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    STALE = "STALE"
    MISSING = "MISSING"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    MARKET_CLOSED = "MARKET_CLOSED"  # e.g. Nasdaq outside trading hours (not a broken feed)


class EvidenceItem(StrictModel):
    evidence_id: str
    source_kind: SourceKind
    source_id: str
    source_url: str | None = None
    title: str
    excerpt: str  # bounded text shown to the LLM; treated as data, never as instructions
    content_hash: str
    content_ref: str | None = None
    event_time: UTCDateTime | None = None
    published_at: UTCDateTime | None = None
    received_at: UTCDateTime
    available_at: UTCDateTime
    revision: int = 0
    supersedes: str | None = None
    verification_status: VerificationStatus
    instrument_scope: tuple[str, ...] = ()


class ScheduledEvent(StrictModel):
    event_id: str
    name: str
    source_id: str
    scheduled_time: UTCDateTime
    importance: str  # LOW | MEDIUM | HIGH
    revision: int = 0
    available_at: UTCDateTime
    actual_value: str | None = None
    actual_available_at: UTCDateTime | None = None


class SourceHealth(StrictModel):
    source_kind: SourceKind
    status: SourceHealthStatus
    detail: str = ""
    last_available_at: UTCDateTime | None = None


def compute_bundle_hash(
    instruments: tuple[InstrumentKey, ...],
    as_of,
    items: tuple[EvidenceItem, ...],
    calendar: tuple[ScheduledEvent, ...],
    health: tuple[SourceHealth, ...],
) -> str:
    payload = {
        "instruments": [i.slug() for i in instruments],
        "as_of": iso_utc(as_of),
        "items": sorted((it.evidence_id, it.content_hash, it.revision) for it in items),
        "calendar": sorted((e.event_id, e.revision, iso_utc(e.scheduled_time)) for e in calendar),
        "health": sorted((h.source_kind.value, h.status.value) for h in health),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class EvidenceBundle(StrictModel):
    bundle_id: str
    instruments: tuple[InstrumentKey, ...]
    as_of: UTCDateTime
    items: tuple[EvidenceItem, ...]
    calendar_snapshot: tuple[ScheduledEvent, ...] = ()
    source_health: tuple[SourceHealth, ...]
    bundle_hash: str

    @model_validator(mode="after")
    def _check(self) -> "EvidenceBundle":
        for it in self.items:
            if it.available_at > self.as_of:
                raise ValueError(f"evidence {it.evidence_id} available_at after as_of (future data)")
        for ev in self.calendar_snapshot:
            if ev.available_at > self.as_of:
                raise ValueError(f"calendar entry {ev.event_id} revision not yet available at as_of")
            if ev.actual_value is not None and (
                ev.actual_available_at is None or ev.actual_available_at > self.as_of
            ):
                raise ValueError(f"calendar entry {ev.event_id} exposes an actual value not yet released")
        ids = [it.evidence_id for it in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate evidence ids in bundle")
        expected = compute_bundle_hash(
            self.instruments, self.as_of, self.items, self.calendar_snapshot, self.source_health
        )
        if expected != self.bundle_hash:
            raise ValueError("bundle_hash does not match bundle content")
        return self

    def evidence_ids(self) -> set[str]:
        return {it.evidence_id for it in self.items} | {e.event_id for e in self.calendar_snapshot}


class ContextStatus(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"
    TIMEOUT = "TIMEOUT"
    INVALID = "INVALID"
    UNSUPPORTED = "UNSUPPORTED"


class TemporalScope(StrEnum):
    INTRADAY_ASOF_VERIFIED = "INTRADAY_ASOF_VERIFIED"
    DAILY_CONTEXT_ONLY = "DAILY_CONTEXT_ONLY"
    UNVERIFIED = "UNVERIFIED"


class ClaimKind(StrEnum):
    OBSERVATION = "OBSERVATION"
    INFERENCE = "INFERENCE"


class ClaimDirection(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    NONE = "NONE"


class Claim(StrictModel):
    text: str
    kind: ClaimKind
    direction: ClaimDirection
    evidence_ids: tuple[str, ...]
    falsifier: str | None = None  # condition that would refute the claim


class EventFlag(StrictModel):
    name: str
    severity: str  # LOW | MEDIUM | HIGH
    evidence_ids: tuple[str, ...]
    window_start: UTCDateTime | None = None
    window_end: UTCDateTime | None = None


class ContextUsage(StrictModel):
    llm_calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_estimate_usd: float | None = None  # None = unknown, never silently 0
    cost_basis: str = "unknown"
    latency_seconds: float | None = None


class ContextAssessment(StrictModel):
    assessment_id: str
    instrument: InstrumentKey
    as_of: UTCDateTime
    backend: str  # tradingagents | simple_llm | offline_fixture | none
    upstream_ref: str | None = None  # e.g. TradingAgents git SHA
    provider: str | None = None
    model_id: str | None = None
    response_model: str | None = None
    temperature: float | None = None
    prompt_hash: str | None = None
    config_hash: str | None = None
    bundle_id: str | None = None
    bundle_hash: str | None = None
    cache_key: str | None = None
    evidence_ids_used: tuple[str, ...] = ()
    claims: tuple[Claim, ...] = ()
    counterclaims: tuple[Claim, ...] = ()
    event_flags: tuple[EventFlag, ...] = ()
    coverage: dict[str, str] = Field(default_factory=dict)  # SourceKind -> SourceHealthStatus
    temporal_scope: TemporalScope
    contextual_rating: str | None = None  # raw upstream label (e.g. Buy/Hold/Sell); NOT a probability
    review_required: bool = False
    status: ContextStatus
    status_reasons: tuple[str, ...] = ()
    usage: ContextUsage = ContextUsage()
    generated_at: UTCDateTime
    valid_until: UTCDateTime

    @model_validator(mode="after")
    def _check(self) -> "ContextAssessment":
        if self.status in (ContextStatus.MISSING, ContextStatus.TIMEOUT, ContextStatus.INVALID, ContextStatus.UNSUPPORTED):
            if not self.status_reasons:
                raise ValueError(f"status {self.status} requires status_reasons")
        if self.valid_until <= self.as_of:
            raise ValueError("valid_until must be after as_of")
        return self


def validate_against_bundle(assessment: ContextAssessment, bundle: EvidenceBundle | None) -> ContextAssessment:
    """Downgrade an assessment whose evidence references are not grounded in the bundle (T09).

    - any referenced evidence id not in the bundle            -> INVALID
    - bundle hash mismatch                                    -> INVALID
    - an OK assessment with a claim lacking evidence ids      -> PARTIAL
    - an OK assessment with no claims at all                  -> PARTIAL
    """
    if assessment.status not in (ContextStatus.OK, ContextStatus.PARTIAL):
        return assessment
    reasons = list(assessment.status_reasons)
    status = assessment.status
    if bundle is None:
        return assessment.model_copy(
            update={"status": ContextStatus.INVALID, "status_reasons": tuple(reasons + ["no evidence bundle"])}
        )
    if assessment.bundle_hash != bundle.bundle_hash:
        return assessment.model_copy(
            update={"status": ContextStatus.INVALID, "status_reasons": tuple(reasons + ["bundle hash mismatch"])}
        )
    valid_ids = bundle.evidence_ids()
    referenced = set(assessment.evidence_ids_used)
    for c in (*assessment.claims, *assessment.counterclaims):
        referenced.update(c.evidence_ids)
    for f in assessment.event_flags:
        referenced.update(f.evidence_ids)
    unknown = sorted(referenced - valid_ids)
    if unknown:
        return assessment.model_copy(
            update={
                "status": ContextStatus.INVALID,
                "status_reasons": tuple(reasons + [f"unknown evidence ids: {unknown[:10]}"]),
            }
        )
    ungrounded = [c.text[:60] for c in (*assessment.claims, *assessment.counterclaims) if not c.evidence_ids]
    if ungrounded:
        status = ContextStatus.PARTIAL
        reasons.append(f"{len(ungrounded)} claim(s) without evidence ids")
    if not assessment.claims and not assessment.counterclaims:
        status = ContextStatus.PARTIAL
        reasons.append("no grounded claims")
    return assessment.model_copy(update={"status": status, "status_reasons": tuple(reasons)})
