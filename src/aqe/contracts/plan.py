"""Trade-plan contracts: rule-computed entry / stop / take-profit candidates and review proposals (docs/TRADE_PLAN.md).

Research output for manual review only:
  * candidates are computed by code from completed bars (unvalidated technical levels — not a learned price forecast,
    not a validated profit target, no success probability);
  * an LLM may only SELECT one listed candidate id per horizon, or wait; it never creates or changes prices;
  * a plan's final `decision` is LONG/SHORT only when every code check passes, the entry conditions are met, a real
    LLM selection was committed AND the independent risk engine says REVIEW_CANDIDATE — otherwise WAIT;
  * approval_required is always true and execution_enabled always false: never an order, size, leverage or account
    instruction. RR is a reward/loss ratio of prices after costs, not a probability or an expected value.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import model_validator

from aqe.contracts.core import Disposition, InstrumentKey, StrictModel, UTCDateTime

CANDIDATE_LABEL = "rule-based technical candidate (unvalidated): not a learned price forecast, not a validated profit target"


class PlanSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class PlanDecision(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


class CheckOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"  # candidate not selectable
    UNKNOWN = "UNKNOWN"  # cannot be verified (e.g. tick size or costs missing): selectable, but the final decision is WAIT


class ConditionStatus(StrEnum):
    MET = "MET"
    NOT_MET = "NOT_MET"
    NOT_EVALUATED = "NOT_EVALUATED"


class SelectionSource(StrEnum):
    LLM = "LLM"  # synthesis of a real LLM debate (provider configured, budget rules applied)
    SCRIPTED_TEST = "SCRIPTED_TEST"  # offline_scripted wiring test — never presented as an AI selection
    UNAVAILABLE = "UNAVAILABLE"  # no usable debate result


class SelectionStatus(StrEnum):
    ACCEPTED = "ACCEPTED"  # passed the code checks and was committed together with the ledger batch
    REJECTED = "REJECTED"  # failed a code check (unknown/other-horizon id, invented price, unselectable, ...)
    NOT_COMMITTED = "NOT_COMMITTED"  # the debate result was not applied (late, stale ledger, invalid)
    ABSENT = "ABSENT"  # the synthesis gave no selection


class PlanStatus(StrEnum):
    NO_CANDIDATES = "NO_CANDIDATES"
    CANDIDATES_ONLY = "CANDIDATES_ONLY"  # technical candidates, no usable selection
    WAIT_SELECTED = "WAIT_SELECTED"  # the synthesis chose to wait
    SELECTED = "SELECTED"
    SELECTION_REJECTED = "SELECTION_REJECTED"
    EXPIRED = "EXPIRED"


def _finite_positive(v: Decimal) -> bool:
    return isinstance(v, Decimal) and v.is_finite() and v > 0


def price_problems(side: PlanSide, entry_low: Decimal, entry_high: Decimal, stop: Decimal, tps: list[Decimal]) -> list[str]:
    """Price-relation check shared by the calculator and the contract (empty = valid).

    LONG:  stop < entry_low <= entry_high < tp1 < tp2 ...
    SHORT: ... tp2 < tp1 < entry_low <= entry_high < stop"""
    vals = {"entry_low": entry_low, "entry_high": entry_high, "stop": stop, **{f"tp{i + 1}": t for i, t in enumerate(tps)}}
    bad = [k for k, v in vals.items() if not _finite_positive(v)]
    if bad:
        return [f"non-finite or non-positive price: {bad}"]
    if not tps:
        return ["no take-profit level"]
    out = []
    if not entry_low <= entry_high:
        out.append(f"entry zone inverted ({entry_low} > {entry_high})")
    if side == PlanSide.LONG:
        if not stop < entry_low:
            out.append(f"LONG stop {stop} must be below entry_low {entry_low} (stop distance > 0)")
        if not entry_high < tps[0]:
            out.append(f"LONG tp1 {tps[0]} must be above entry_high {entry_high}")
        out += [f"LONG tp{i + 2} {b} must be above tp{i + 1} {a}" for i, (a, b) in enumerate(zip(tps, tps[1:])) if not a < b]
    else:
        if not entry_high < stop:
            out.append(f"SHORT stop {stop} must be above entry_high {entry_high} (stop distance > 0)")
        if not tps[0] < entry_low:
            out.append(f"SHORT tp1 {tps[0]} must be below entry_low {entry_low}")
        out += [f"SHORT tp{i + 2} {b} must be below tp{i + 1} {a}" for i, (a, b) in enumerate(zip(tps, tps[1:])) if not b < a]
    return out


def content_id(prefix: str, model: StrictModel, exclude: str) -> str:
    """Deterministic identity of a model's content (everything except its own id field)."""
    return prefix + hashlib.sha256(model.model_dump_json(exclude={exclude}).encode()).hexdigest()[:20]


class PriceLevel(StrictModel):
    kind: str  # SWING_LOW | SWING_HIGH
    price: Decimal
    pivot_time: UTCDateTime  # open time of the pivot bar
    confirmed_at: UTCDateTime  # when the right-side confirmation bars were all available (never before)
    timeframe_seconds: int

    @model_validator(mode="after")
    def _check(self) -> "PriceLevel":
        if not _finite_positive(self.price):
            raise ValueError("level price must be finite and positive")
        if self.confirmed_at <= self.pivot_time:
            raise ValueError("a pivot cannot be confirmed before its own bar")
        return self


class PlanCheck(StrictModel):
    name: str
    outcome: CheckOutcome
    detail: str


class EntryCondition(StrictModel):
    condition_id: str
    text: str
    status: ConditionStatus
    observed: str = ""


class TakeProfit(StrictModel):
    label: str  # TP1 / TP2
    price: Decimal
    basis: str
    net_reward_fraction: float | None = None  # after round-trip fee + slippage, conservative entry; None = costs unknown
    net_rr: float | None = None  # net reward / net loss — geometry only, NOT a probability or expected value


class CostAssumptions(StrictModel):
    taker_fee_bps: Decimal  # per side
    slippage_bps: Decimal  # per side, adverse on entry and exit
    source: str
    funding_included: bool = False
    note: str = "funding not included: the holding time is not known in advance"

    @model_validator(mode="after")
    def _check(self) -> "CostAssumptions":
        for v in (self.taker_fee_bps, self.slippage_bps):
            if not (v.is_finite() and v >= 0):
                raise ValueError("cost assumptions must be finite and non-negative")
        return self


class InstrumentSpec(StrictModel):
    instrument: InstrumentKey
    tick_size: Decimal
    source_id: str
    available_at: UTCDateTime

    @model_validator(mode="after")
    def _check(self) -> "InstrumentSpec":
        if not _finite_positive(self.tick_size):
            raise ValueError("tick_size must be finite and positive")
        return self


class PlanCandidate(StrictModel):
    candidate_id: str  # content hash: same inputs + config -> same id
    instrument: InstrumentKey
    as_of: UTCDateTime
    horizon_seconds: int
    side: PlanSide
    timeframe_seconds: int
    entry_low: Decimal
    entry_high: Decimal
    conservative_entry: Decimal  # LONG: entry_high, SHORT: entry_low (the RR never uses the favourable end)
    entry_basis: str
    entry_conditions: tuple[EntryCondition, ...]
    stop: Decimal
    stop_basis: str
    take_profits: tuple[TakeProfit, ...]
    net_loss_fraction: float | None = None  # at the stop, after costs; None = costs unknown
    levels: tuple[PriceLevel, ...]  # the observed levels the prices come from
    checks: tuple[PlanCheck, ...]
    invalidation: tuple[str, ...]
    source_evidence_ids: tuple[str, ...] = ()  # bundle evidence items computed from the same bars (shared context)
    available_at: UTCDateTime  # latest input availability (<= as_of)
    valid_until: UTCDateTime
    label: str = CANDIDATE_LABEL

    @model_validator(mode="after")
    def _check(self) -> "PlanCandidate":
        probs = price_problems(self.side, self.entry_low, self.entry_high, self.stop, [t.price for t in self.take_profits])
        if probs:
            raise ValueError("; ".join(probs))
        want = self.entry_high if self.side == PlanSide.LONG else self.entry_low
        if self.conservative_entry != want:
            raise ValueError("conservative_entry must be entry_high (LONG) / entry_low (SHORT)")
        if self.available_at > self.as_of:
            raise ValueError("candidate uses data not available at as_of")
        if any(lv.confirmed_at > self.as_of for lv in self.levels):
            raise ValueError("candidate uses a level confirmed after as_of")
        if self.valid_until <= self.as_of:
            raise ValueError("valid_until must be after as_of")
        if self.candidate_id != content_id("pc-", self, "candidate_id"):
            raise ValueError("candidate_id does not match the candidate content")
        return self

    @property
    def selectable(self) -> bool:
        return not any(c.outcome == CheckOutcome.FAIL for c in self.checks)

    @property
    def verified(self) -> bool:
        return all(c.outcome == CheckOutcome.PASS for c in self.checks)


class HorizonCandidates(StrictModel):
    horizon_seconds: int
    timeframe_seconds: int | None = None
    atr: Decimal | None = None
    reference_price: Decimal | None = None  # close of the last completed base bar at as_of
    reference_time: UTCDateTime | None = None
    candidates: tuple[PlanCandidate, ...] = ()  # at most one LONG and one SHORT
    no_candidate_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "HorizonCandidates":
        sides = [c.side for c in self.candidates]
        if len(sides) != len(set(sides)):
            raise ValueError("at most one candidate per side and horizon")
        if any(c.horizon_seconds != self.horizon_seconds for c in self.candidates):
            raise ValueError("candidate horizon differs from its horizon block")
        return self


class PlanCandidateSet(StrictModel):
    """One point-in-time candidate snapshot. Included in the EvidenceBundle (hashed), selected from by the debate,
    and referenced by the final plan — prices are never recomputed for an earlier selection."""

    snapshot_id: str  # content hash
    algorithm_version: str
    config_hash: str
    input_hash: str
    instrument: InstrumentKey
    as_of: UTCDateTime
    input_description: str
    last_bar_close: UTCDateTime | None = None
    last_bar_available_at: UTCDateTime | None = None
    costs: CostAssumptions | None = None
    instrument_spec: InstrumentSpec | None = None
    spec_note: str = ""
    horizons: tuple[HorizonCandidates, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> "PlanCandidateSet":
        hs = [h.horizon_seconds for h in self.horizons]
        if len(hs) != len(set(hs)):
            raise ValueError("duplicate horizons in candidate snapshot")
        ids = [c.candidate_id for h in self.horizons for c in h.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate candidate ids")
        for h in self.horizons:
            for c in h.candidates:
                if c.instrument != self.instrument or c.as_of != self.as_of:
                    raise ValueError("candidate instrument/as_of differs from its snapshot")
        if self.last_bar_available_at is not None and self.last_bar_available_at > self.as_of:
            raise ValueError("snapshot input not available at as_of")
        if self.instrument_spec is not None and (self.instrument_spec.available_at > self.as_of
                                                 or self.instrument_spec.instrument != self.instrument):
            raise ValueError("instrument spec not available at as_of / other instrument")
        if self.snapshot_id != content_id("ps-", self, "snapshot_id"):
            raise ValueError("snapshot_id does not match the snapshot content")
        return self

    def candidate(self, candidate_id: str) -> PlanCandidate | None:
        return next((c for h in self.horizons for c in h.candidates if c.candidate_id == candidate_id), None)

    def horizon(self, horizon_seconds: int) -> HorizonCandidates | None:
        return next((h for h in self.horizons if h.horizon_seconds == horizon_seconds), None)


class PlanSelection(StrictModel):
    """What the synthesis may say about a plan: an id from the list (or null = wait) and why. No price fields exist —
    an answer carrying any extra field (e.g. an entry price) is a contract violation."""

    candidate_id: str | None
    reason: str
    evidence_ids: tuple[str, ...] = ()
    counter_evidence_ids: tuple[str, ...] = ()


class PlanSelectionRecord(StrictModel):
    """Code verdict on a horizon's selection, stored in the DebateRecord and committed through the result outbox."""

    horizon_seconds: int
    snapshot_id: str
    status: SelectionStatus
    candidate_id: str | None = None
    side: PlanSide | None = None
    reason: str = ""
    evidence_ids: tuple[str, ...] = ()
    counter_evidence_ids: tuple[str, ...] = ()
    detail: str = ""


class TradePlan(StrictModel):
    """Per-horizon review proposal built at analysis time from a committed selection and the risk decision."""

    plan_id: str
    instrument: InstrumentKey
    horizon_seconds: int
    as_of: UTCDateTime | None  # as_of of the candidate snapshot used (None = no snapshot)
    decision_time: UTCDateTime
    valid_until: UTCDateTime | None = None
    algorithm_version: str | None = None
    config_hash: str | None = None
    input_hash: str | None = None
    snapshot_id: str | None = None
    status: PlanStatus
    proposal_side: PlanSide | None = None  # side of the selected candidate (analysis proposal) — NOT the decision
    decision: PlanDecision
    candidates: tuple[PlanCandidate, ...] = ()  # technical candidates shown separately from the decision
    no_candidate_reasons: tuple[str, ...] = ()
    selected_candidate_id: str | None = None
    selected: PlanCandidate | None = None
    selection_source: SelectionSource
    selection_status: SelectionStatus
    selection_reason: str | None = None
    evidence_ids: tuple[str, ...] = ()
    counter_evidence_ids: tuple[str, ...] = ()
    hypothesis_ids: tuple[str, ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    wait_reasons: tuple[str, ...] = ()
    risk_disposition: Disposition
    approval_required: Literal[True] = True
    execution_enabled: Literal[False] = False

    @model_validator(mode="after")
    def _check(self) -> "TradePlan":
        s = self.selected
        if (s is None) != (self.selected_candidate_id is None) or (s is not None and s.candidate_id != self.selected_candidate_id):
            raise ValueError("selected candidate and selected_candidate_id disagree")
        if s is not None and self.proposal_side != s.side:
            raise ValueError("proposal_side must be the selected candidate's side")
        if self.decision == PlanDecision.WAIT:
            if not self.wait_reasons:
                raise ValueError("a WAIT plan must state its reasons")
            return self
        # LONG/SHORT is a manual-review proposal and requires every gate; nothing can lift a restriction
        problems = []
        if s is None or s.side.value != self.decision.value:
            problems.append("decision must equal the selected candidate's side")
        if self.risk_disposition != Disposition.REVIEW_CANDIDATE:
            problems.append(f"risk disposition {self.risk_disposition.value} does not allow a LONG/SHORT proposal")
        if self.selection_source != SelectionSource.LLM or self.selection_status != SelectionStatus.ACCEPTED:
            problems.append("needs a committed (ACCEPTED) selection from a real LLM synthesis")
        if self.wait_reasons:
            problems.append("wait reasons present")
        if s is not None and (not s.verified or any(c.status != ConditionStatus.MET for c in s.entry_conditions)):
            problems.append("all checks must PASS and all entry conditions must be MET")
        if s is not None and s.valid_until <= self.decision_time:
            problems.append("selected candidate expired")
        if problems:
            raise ValueError("; ".join(problems))
        return self
