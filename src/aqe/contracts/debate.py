"""Analysis/debate layer contracts (AQE_Agent_Debate_Structure_KO v0.1, §5–§7, §12).

Specialist analysts -> critic -> at most one revision -> synthesis -> code checks -> hypothesis history.
The layer is research output only: it never rewrites numeric model probabilities, never lifts a
risk restriction and LLM confidence is never stored as a calibrated probability.
Inputs reuse the existing EvidenceBundle; the synthesis is also projected onto ContextAssessment.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from aqe.contracts.core import InstrumentKey, StrictModel, UTCDateTime


class AnalystRole(StrEnum):
    CHART = "CHART"  # price structure, trend, volatility, indicators, AQE numeric predictions
    FLOW = "FLOW"  # order book, trades/CVD, OI, funding (observations only; no intent/identity claims)
    NEWS = "NEWS"  # dated articles and scheduled events
    CROSS_MARKET = "CROSS_MARKET"  # Nasdaq etc. with trading hours and delays


class RoleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    NOT_ACTIVATED = "NOT_ACTIVATED"  # required evidence not available: scope reduced, NOT "nothing happened"
    FAILED = "FAILED"  # call/parse/contract failure


class StatementKind(StrEnum):
    OBSERVATION = "OBSERVATION"  # measured fact from evidence
    INTERPRETATION = "INTERPRETATION"  # reading of the observation
    HYPOTHESIS = "HYPOTHESIS"  # claim about the future, must carry invalidation conditions


class ThesisType(StrEnum):
    DIRECTIONAL = "DIRECTIONAL"  # trend direction over the horizon
    REBOUND = "REBOUND"  # short counter-move inside a larger trend
    REVERSAL = "REVERSAL"  # change of the prevailing trend
    RANGE = "RANGE"  # no directional edge / mean reversion


class Direction(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    NONE = "NONE"


class Statement(StrictModel):
    statement_id: str
    kind: StatementKind
    text: str
    evidence_ids: tuple[str, ...]


class Hypothesis(StrictModel):
    hypothesis_id: str
    horizon_seconds: int
    thesis_type: ThesisType
    direction: Direction
    statement: str
    evidence_ids: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]  # what observation would withdraw it
    review_conditions: tuple[str, ...] = ()
    qualitative_confidence: str | None = None  # LOW/MEDIUM/HIGH wording only — NOT a probability

    @model_validator(mode="after")
    def _check(self) -> "Hypothesis":
        if not self.invalidation_conditions:
            raise ValueError("a hypothesis must state at least one invalidation condition")
        if self.qualitative_confidence is not None and self.qualitative_confidence.upper() not in ("LOW", "MEDIUM", "HIGH"):
            raise ValueError("qualitative_confidence must be LOW/MEDIUM/HIGH (no numeric confidence)")
        return self


class AnalystReport(StrictModel):
    role: AnalystRole
    round: int = 0  # 0 = independent draft, 1 = revision after critique
    statements: tuple[Statement, ...]
    hypotheses: tuple[Hypothesis, ...] = ()
    counter_evidence: tuple[Statement, ...] = ()
    missing_data: tuple[str, ...] = ()


class IssueType(StrEnum):
    UNSUPPORTED = "UNSUPPORTED"  # claim not backed by the cited evidence
    LEAP = "LEAP"  # conclusion does not follow
    DUPLICATE_EVIDENCE = "DUPLICATE_EVIDENCE"  # same underlying data counted as independent support
    MISSING_DATA_IGNORED = "MISSING_DATA_IGNORED"
    HORIZON_MIX = "HORIZON_MIX"  # different horizons merged
    TIME_VIOLATION = "TIME_VIOLATION"
    NUMERIC_MISMATCH = "NUMERIC_MISMATCH"
    INTENT_CLAIM = "INTENT_CLAIM"  # asserts identity/intent of market participants


class CritiqueIssue(StrictModel):
    target_role: AnalystRole
    target_ref: str  # statement_id or hypothesis_id
    issue_type: IssueType
    severity: str  # MAJOR | MINOR
    explanation: str
    required_check: str = ""


class CritiqueReport(StrictModel):
    issues: tuple[CritiqueIssue, ...]
    revision_role: AnalystRole | None = None  # at most one role gets one revision


class PriorDecisionType(StrEnum):
    KEEP = "KEEP"
    REVISE = "REVISE"
    INVALIDATE = "INVALIDATE"  # requires evidence that an invalidation condition was observed


class PriorDecision(StrictModel):
    hypothesis_id: str
    decision: PriorDecisionType
    reason: str
    evidence_ids: tuple[str, ...] = ()
    replacement_id: str | None = None  # for REVISE: id of the new hypothesis


class HorizonSynthesis(StrictModel):
    horizon_seconds: int
    primary: Hypothesis | None  # None => ABSTAIN for this horizon
    opposing: tuple[Hypothesis, ...] = ()
    unresolved: tuple[str, ...] = ()
    changed_since_previous: str = ""
    abstain_reason: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "HorizonSynthesis":
        if self.primary is None and not self.abstain_reason:
            raise ValueError("a horizon without a primary hypothesis must state abstain_reason")
        for h in (self.primary, *self.opposing):
            if h is not None and h.horizon_seconds != self.horizon_seconds:
                raise ValueError("hypothesis horizon differs from its synthesis horizon (no horizon mixing)")
        return self


class SynthesisReport(StrictModel):
    horizons: tuple[HorizonSynthesis, ...]
    prior_decisions: tuple[PriorDecision, ...] = ()
    limitations: tuple[str, ...] = ()


class HypothesisState(StrEnum):
    PROPOSED = "PROPOSED"  # produced, not yet accepted by code checks
    ACTIVE = "ACTIVE"
    REVISED = "REVISED"  # replaced by a newer hypothesis (lineage kept)
    INVALIDATED = "INVALIDATED"  # withdrawn because an invalidation condition was observed
    EXPIRED = "EXPIRED"  # valid_until passed without re-confirmation (time, not evidence)
    ABSTAIN = "ABSTAIN"  # no hypothesis for this horizon in this run


class HypothesisRecord(StrictModel):
    """Append-only ledger row (state transitions are new rows; nothing is overwritten)."""

    record_id: str
    hypothesis_id: str
    instrument: InstrumentKey
    horizon_seconds: int
    state: HypothesisState
    reason: str
    debate_id: str
    as_of: UTCDateTime
    recorded_at: UTCDateTime
    valid_until: UTCDateTime
    previous_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    hypothesis: Hypothesis | None = None
    position: str = "primary"  # primary | opposing (alternative hypothesis kept for the same horizon)


class DebateRecord(StrictModel):
    debate_id: str
    assessment_id: str
    instrument: InstrumentKey
    as_of: UTCDateTime
    generated_at: UTCDateTime
    valid_until: UTCDateTime
    bundle_id: str
    bundle_hash: str
    horizons_seconds: tuple[int, ...]
    roles: dict[str, str]  # role -> RoleStatus (+ reason in role_notes)
    role_notes: dict[str, str] = Field(default_factory=dict)
    drafts: tuple[AnalystReport, ...] = ()
    critique: CritiqueReport | None = None
    revision: AnalystReport | None = None
    synthesis: SynthesisReport | None = None
    check_failures: tuple[str, ...] = ()
    llm_calls: int = 0
    max_llm_calls: int = 8
    provider: str | None = None
    model_id: str | None = None
    prompt_hash: str | None = None
    code_version: str | None = None
    status: str = "OK"  # OK | PARTIAL | INVALID | MISSING | TIMEOUT
    applied_to_ledger: bool = False
    ledger_note: str = ""
