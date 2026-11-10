"""Independent, deterministic risk judgement (B10). Pure functions; no LLM calls.

The context assessment is read only for event flags/coverage; it can only make the
outcome *more* restrictive and its contextual_rating is ignored (T11).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from aqe.contracts.context import ContextAssessment, ContextStatus
from aqe.contracts.core import (
    READINESS_RANK,
    Disposition,
    FeatureProfile,
    ModelReadiness,
    QualityStatus,
    RuleOutcome,
    RuntimeMode,
    most_restrictive,
)
from aqe.contracts.data import FeatureSnapshot
from aqe.contracts.predictions import EnsemblePrediction, EnsembleStatus, ModelPrediction, OODStatus, PredictionStatus
from aqe.contracts.risk import PortfolioSnapshot, RiskDecision, RuleResult


class PortfolioRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_gross_exposure_x_equity: float = 1.0
    max_net_exposure_x_equity: float = 1.0
    max_daily_loss_fraction: float = 0.02
    max_snapshot_age_seconds: int = 3600
    unknown_caps_at: Disposition = Disposition.RESEARCH_ONLY


class RiskRuleset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ruleset_id: str
    version: int
    min_readiness_for_review: ModelReadiness = ModelReadiness.OOS_EVALUATED
    require_calibration_validated: bool = True
    require_oos_skill: bool = True  # E3 must beat the prior baseline on the locked test (block-bootstrap CI)
    require_cost_adjusted_edge: bool = True  # locked-test fixed-policy virtual trading must be net-positive after costs
    max_clock_skew_ms: float = 1000.0
    future_tolerance_seconds: float = 1.0
    max_bar_staleness_seconds: int = 900
    # flow_v1 requires continuous L2; ohlcv_v0 must not be failed for lacking L2 (B10)
    required_sources: dict[str, list[str]] = {
        "ohlcv_v0": ["bars"],
        "flow_v1": ["bars", "l2", "trades"],
    }
    high_event_caps_at: Disposition = Disposition.RESEARCH_ONLY
    event_info_unknown_caps_at: Disposition = Disposition.RESEARCH_ONLY
    disagreement_exceeded_caps_at: Disposition = Disposition.RESEARCH_ONLY
    disagreement_unknown_caps_at: Disposition = Disposition.RESEARCH_ONLY
    ood_caps_at: Disposition = Disposition.RESEARCH_ONLY
    min_quote_volume_24h: float = 5.0e7
    required_cost_assumptions: list[str] = ["taker_fee_bps", "slippage_bps"]
    portfolio: PortfolioRules = PortfolioRules()


def load_ruleset(path: Path) -> tuple[RiskRuleset, str]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    rs = RiskRuleset.model_validate(raw)
    h = hashlib.sha256(json.dumps(rs.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    return rs, h


@dataclass
class RiskInputs:
    analysis_id: str
    mode: RuntimeMode
    decision_time: datetime  # wall clock (shadow/research) or simulated time (replay)
    horizon_seconds: int
    snapshot: FeatureSnapshot | None
    predictions: list[ModelPrediction]
    ensemble: EnsemblePrediction | None
    # artifact metadata for each model used: {"artifact_id","feature_profile","feature_schema_hash","horizon_seconds","instrument_id"}
    artifact_meta: list[dict[str, Any]]
    context: ContextAssessment | None
    clock_skew_ms: float | None
    source_quality: dict[str, QualityStatus]
    quote_volume_24h: float | None
    cost_assumptions: dict[str, float]
    portfolio: PortfolioSnapshot | None
    last_price: float | None
    notes: list[str] = field(default_factory=list)


def _rule(rid: str, order: int, outcome: RuleOutcome, reason: str, cap: Disposition | None = None, recovery: str | None = None) -> RuleResult:
    return RuleResult(
        rule_id=rid,
        order=order,
        outcome=outcome,
        reason=reason,
        recovery_condition=recovery,
        caps_disposition_at=cap.value if cap else None,
    )


def _r1_readiness(inp: RiskInputs, rs: RiskRuleset) -> RuleResult:
    rid = "R1_READINESS_ARTIFACT"
    if inp.snapshot is None:
        return _rule(rid, 1, RuleOutcome.FAIL, "no feature snapshot", Disposition.ABSTAIN, "build features from available data")
    snap = inp.snapshot
    for meta in inp.artifact_meta:
        if meta.get("feature_profile") != snap.feature_profile.value:
            return _rule(rid, 1, RuleOutcome.FAIL, f"artifact {meta.get('artifact_id')} profile {meta.get('feature_profile')} != snapshot {snap.feature_profile.value}", Disposition.ABSTAIN, "use artifact trained on the same feature profile")
        if meta.get("feature_schema_hash") != snap.feature_schema_hash:
            return _rule(rid, 1, RuleOutcome.FAIL, f"artifact {meta.get('artifact_id')} feature schema hash mismatch", Disposition.ABSTAIN, "retrain or rebuild features with the artifact's schema")
        if int(meta.get("horizon_seconds", -1)) != inp.horizon_seconds:
            return _rule(rid, 1, RuleOutcome.FAIL, f"artifact {meta.get('artifact_id')} horizon mismatch", Disposition.ABSTAIN)
        if meta.get("instrument_id") != snap.instrument.instrument_id:
            return _rule(rid, 1, RuleOutcome.FAIL, f"artifact {meta.get('artifact_id')} instrument mismatch", Disposition.ABSTAIN)
    for p in inp.predictions:
        if p.key.instrument != snap.instrument or p.key.feature_profile != snap.feature_profile:
            return _rule(rid, 1, RuleOutcome.FAIL, f"prediction {p.prediction_id} identity mismatch", Disposition.ABSTAIN)
    if inp.ensemble is None or inp.ensemble.status != EnsembleStatus.OK:
        why = "no ensemble" if inp.ensemble is None else "; ".join(inp.ensemble.abstain_reasons)
        return _rule(rid, 1, RuleOutcome.FAIL, f"no usable combined prediction: {why}", Disposition.ABSTAIN, "train base+meta+calibrator artifacts for this horizon/profile")
    min_rank = READINESS_RANK[rs.min_readiness_for_review]
    readiness = [p.readiness for p in inp.predictions] + [inp.ensemble.readiness]
    low = [r.value for r in readiness if READINESS_RANK[r] < min_rank]
    if low:
        return _rule(rid, 1, RuleOutcome.FAIL, f"model readiness below {rs.min_readiness_for_review.value}: {sorted(set(low))}", Disposition.RESEARCH_ONLY, "complete OOS evaluation")
    if rs.require_calibration_validated and not inp.ensemble.calibration_validated:
        return _rule(rid, 1, RuleOutcome.FAIL, "calibration not validated", Disposition.RESEARCH_ONLY, "fit and validate calibrator on an independent segment")
    if rs.require_oos_skill and inp.ensemble.extra.get("oos_verdict") != "SKILL_DEMONSTRATED":
        return _rule(rid, 1, RuleOutcome.FAIL, f"locked-test verdict: {inp.ensemble.extra.get('oos_verdict') or 'not evaluated'} (no demonstrated skill over the prior baseline)", Disposition.RESEARCH_ONLY, "a new model version must show skill on a fresh locked segment / forward test")
    return _rule(rid, 1, RuleOutcome.PASS, "artifacts match snapshot identity/profile/horizon; readiness sufficient")


def _r2_time_quality(inp: RiskInputs, rs: RiskRuleset) -> RuleResult:
    rid = "R2_TIME_FRESHNESS_QUALITY"
    now = inp.decision_time
    snap = inp.snapshot
    if snap is None:
        return _rule(rid, 2, RuleOutcome.FAIL, "no snapshot", Disposition.ABSTAIN)
    if snap.as_of > now + timedelta(seconds=rs.future_tolerance_seconds):
        return _rule(rid, 2, RuleOutcome.FAIL, f"snapshot as_of {snap.as_of} is in the future of decision time {now}", Disposition.ABSTAIN, "check clock / replay time")
    for p in inp.predictions:
        if p.status != PredictionStatus.OK:
            return _rule(rid, 2, RuleOutcome.FAIL, f"prediction {p.model_kind} status {p.status}: {p.status_reason}", Disposition.ABSTAIN)
        if p.ready_at > now:
            return _rule(rid, 2, RuleOutcome.FAIL, f"prediction {p.model_kind} not ready until {p.ready_at}", Disposition.ABSTAIN, "wait until ready_at")
        if p.valid_until <= now:
            return _rule(rid, 2, RuleOutcome.FAIL, f"prediction {p.model_kind} expired at {p.valid_until}", Disposition.ABSTAIN, "recompute with fresh data")
    if inp.ensemble is not None and inp.ensemble.valid_until <= now:
        return _rule(rid, 2, RuleOutcome.FAIL, "ensemble expired", Disposition.ABSTAIN, "recompute")
    if snap.quality == QualityStatus.INVALID or snap.quality == QualityStatus.MISSING:
        return _rule(rid, 2, RuleOutcome.FAIL, f"snapshot quality {snap.quality}: {list(snap.quality_reasons)}", Disposition.ABSTAIN, "repair source data")
    required = rs.required_sources.get(snap.feature_profile.value, [])
    for src in required:
        q = inp.source_quality.get(src)
        if q is None:
            return _rule(rid, 2, RuleOutcome.UNKNOWN, f"required source '{src}' quality unknown", Disposition.ABSTAIN, f"provide {src} quality status")
        if q != QualityStatus.OK:
            return _rule(rid, 2, RuleOutcome.FAIL, f"required source '{src}' quality {q}", Disposition.ABSTAIN, f"restore continuous {src}")
        wm = snap.source_watermarks.get(src)
        if wm is not None and (now - wm).total_seconds() > rs.max_bar_staleness_seconds and inp.mode != RuntimeMode.REPLAY:
            return _rule(rid, 2, RuleOutcome.FAIL, f"source '{src}' stale: last available {wm}", Disposition.ABSTAIN, "resume collection")
    if inp.mode != RuntimeMode.REPLAY:
        if inp.clock_skew_ms is None:
            return _rule(rid, 2, RuleOutcome.UNKNOWN, "clock skew unknown", Disposition.ABSTAIN, "measure clock offset against exchange server time")
        if abs(inp.clock_skew_ms) > rs.max_clock_skew_ms:
            return _rule(rid, 2, RuleOutcome.FAIL, f"clock skew {inp.clock_skew_ms:.0f} ms exceeds {rs.max_clock_skew_ms:.0f} ms", Disposition.ABSTAIN, "synchronise the system clock (NTP)")
    cap = Disposition.RESEARCH_ONLY if snap.quality == QualityStatus.DEGRADED else None
    reason = "times consistent; required sources OK" + ("; snapshot DEGRADED" if cap else "")
    if inp.mode == RuntimeMode.REPLAY:
        reason += "; replay: wall-clock skew/staleness checks not applicable"
    return _rule(rid, 2, RuleOutcome.PASS if cap is None else RuleOutcome.FAIL, reason, cap)


def _r3_probability(inp: RiskInputs, rs: RiskRuleset) -> RuleResult:
    rid = "R3_PROBABILITY_CONSISTENCY"
    ens = inp.ensemble
    if ens is None or ens.probabilities is None:
        return _rule(rid, 3, RuleOutcome.FAIL, "no ensemble probabilities", Disposition.ABSTAIN)
    if ens.key.horizon_seconds != inp.horizon_seconds or any(p.key.horizon_seconds != inp.horizon_seconds for p in inp.predictions):
        return _rule(rid, 3, RuleOutcome.FAIL, "horizon mismatch between predictions", Disposition.ABSTAIN)
    if ens.ood_status == OODStatus.OUT_OF_DISTRIBUTION:
        return _rule(rid, 3, RuleOutcome.FAIL, "inputs out of training distribution", rs.ood_caps_at, "review regime; do not rely on probabilities")
    if ens.disagreement_threshold is None:
        return _rule(rid, 3, RuleOutcome.UNKNOWN, "no OOS-validated disagreement threshold", rs.disagreement_unknown_caps_at)
    d = ens.disagreement.get("max_abs_diff")
    if d is None:
        return _rule(rid, 3, RuleOutcome.UNKNOWN, "disagreement not measured", rs.disagreement_unknown_caps_at)
    if d > ens.disagreement_threshold:
        return _rule(rid, 3, RuleOutcome.FAIL, f"base models disagree ({d:.3f} > {ens.disagreement_threshold:.3f}); shown separately, not averaged away", rs.disagreement_exceeded_caps_at)
    if ens.ood_status == OODStatus.UNKNOWN:
        return _rule(rid, 3, RuleOutcome.UNKNOWN, "OOD status unknown", rs.ood_caps_at)
    return _rule(rid, 3, RuleOutcome.PASS, f"probabilities valid; disagreement {d:.3f} <= {ens.disagreement_threshold:.3f}")


def _r4_event_liquidity_cost(inp: RiskInputs, rs: RiskRuleset) -> RuleResult:
    rid = "R4_EVENT_LIQUIDITY_COST"
    missing_costs = [k for k in rs.required_cost_assumptions if k not in inp.cost_assumptions]
    if missing_costs:
        return _rule(rid, 4, RuleOutcome.UNKNOWN, f"cost assumptions missing: {missing_costs}", Disposition.ABSTAIN)
    if inp.quote_volume_24h is None:
        return _rule(rid, 4, RuleOutcome.UNKNOWN, "24h quote volume unknown", Disposition.ABSTAIN)
    if inp.quote_volume_24h < rs.min_quote_volume_24h:
        return _rule(rid, 4, RuleOutcome.FAIL, f"24h quote volume {inp.quote_volume_24h:.3g} below {rs.min_quote_volume_24h:.3g}", Disposition.ABSTAIN)
    if rs.require_cost_adjusted_edge and inp.ensemble is not None and inp.ensemble.extra.get("oos_cost_verdict") != "NET_POSITIVE":
        return _rule(rid, 4, RuleOutcome.FAIL, f"locked-test virtual trading net of fees/slippage/funding: {inp.ensemble.extra.get('oos_cost_verdict') or 'not evaluated'}", Disposition.RESEARCH_ONLY, "show a positive cost-adjusted edge on a fresh locked segment / forward test")
    ctx = inp.context
    if ctx is None or ctx.status not in (ContextStatus.OK, ContextStatus.PARTIAL):
        why = "no context" if ctx is None else f"context {ctx.status.value}"
        return _rule(rid, 4, RuleOutcome.UNKNOWN, f"event/calendar risk unknown ({why}); flow-based and news evidence absent", rs.event_info_unknown_caps_at, "provide calendar/news evidence via context worker")
    horizon_end = inp.snapshot.as_of + timedelta(seconds=inp.horizon_seconds) if inp.snapshot else None
    high = []
    for f in ctx.event_flags:
        if f.severity.upper() != "HIGH":
            continue
        if f.window_start is None or horizon_end is None or f.window_start <= horizon_end:
            high.append(f.name)
    if high:
        return _rule(rid, 4, RuleOutcome.FAIL, f"high-severity events within horizon: {high}", rs.high_event_caps_at, "re-evaluate after the event")
    return _rule(rid, 4, RuleOutcome.PASS, "liquidity sufficient; cost assumptions present; no high-severity event flags")


def _r5_portfolio(inp: RiskInputs, rs: RiskRuleset) -> RuleResult:
    rid = "R5_PORTFOLIO_LIMITS"
    pr = rs.portfolio
    pf = inp.portfolio
    if pf is None:
        return _rule(rid, 5, RuleOutcome.UNKNOWN, "portfolio not provided (treated as UNKNOWN, not flat)", pr.unknown_caps_at, "supply a portfolio snapshot")
    if (inp.decision_time - pf.as_of).total_seconds() > pr.max_snapshot_age_seconds:
        return _rule(rid, 5, RuleOutcome.UNKNOWN, f"portfolio snapshot stale ({pf.as_of})", pr.unknown_caps_at, "refresh portfolio snapshot")
    if inp.last_price is None or pf.equity_quote <= 0:
        return _rule(rid, 5, RuleOutcome.UNKNOWN, "cannot value exposure (no price or equity)", pr.unknown_caps_at)
    gross = sum(abs(p.quantity_base) * inp.last_price for p in pf.positions)
    net = sum(p.quantity_base * inp.last_price for p in pf.positions)
    if gross > pr.max_gross_exposure_x_equity * pf.equity_quote:
        return _rule(rid, 5, RuleOutcome.FAIL, f"gross exposure {gross:.2f} exceeds limit", Disposition.ABSTAIN, "reduce exposure")
    if abs(net) > pr.max_net_exposure_x_equity * pf.equity_quote:
        return _rule(rid, 5, RuleOutcome.FAIL, f"net exposure {net:.2f} exceeds limit", Disposition.ABSTAIN, "reduce exposure")
    if pf.daily_loss_quote > pr.max_daily_loss_fraction * pf.equity_quote:
        return _rule(rid, 5, RuleOutcome.FAIL, "daily loss budget exhausted", Disposition.ABSTAIN, "wait for next budget period")
    return _rule(rid, 5, RuleOutcome.PASS, "portfolio within limits (valued at last price; tick/step rules not applied: no sizing output)")


def evaluate_risk(inp: RiskInputs, rs: RiskRuleset, ruleset_hash: str) -> RiskDecision:
    results = [
        _r1_readiness(inp, rs),
        _r2_time_quality(inp, rs),
        _r3_probability(inp, rs),
        _r4_event_liquidity_cost(inp, rs),
        _r5_portfolio(inp, rs),
        _rule("R6_MANUAL_REVIEW_OUTPUT", 6, RuleOutcome.PASS, "output is advisory: approval_required=true, execution_enabled=false"),
    ]
    outcomes = [r.outcome for r in results]
    if RuleOutcome.FAIL in outcomes:
        overall = RuleOutcome.FAIL
    elif RuleOutcome.UNKNOWN in outcomes:
        overall = RuleOutcome.UNKNOWN
    else:
        overall = RuleOutcome.PASS
    caps = [Disposition(r.caps_disposition_at) for r in results if r.caps_disposition_at]
    disposition = Disposition.REVIEW_CANDIDATE if overall == RuleOutcome.PASS else Disposition.RESEARCH_ONLY
    if caps:
        disposition = most_restrictive(disposition, *caps)
    reasons = tuple(f"{r.rule_id}: {r.outcome.value} - {r.reason}" for r in results if r.outcome != RuleOutcome.PASS)
    valid_until = inp.decision_time + timedelta(seconds=min(inp.horizon_seconds, 3600))
    if inp.ensemble is not None:
        valid_until = min(valid_until, inp.ensemble.valid_until)
    if valid_until <= inp.decision_time:
        valid_until = inp.decision_time + timedelta(seconds=1)
    return RiskDecision(
        analysis_id=inp.analysis_id,
        horizon_seconds=inp.horizon_seconds,
        ruleset_id=rs.ruleset_id,
        ruleset_hash=ruleset_hash,
        rules=tuple(results),
        risk_outcome=overall,
        disposition=disposition,
        reasons=reasons,
        portfolio_status="PROVIDED" if inp.portfolio is not None else "UNKNOWN",
        decided_at=inp.decision_time,
        valid_until=valid_until,
    )


_ = FeatureProfile
