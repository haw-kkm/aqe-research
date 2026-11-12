"""Per-horizon review proposals (`TradePlan`) built at analysis time — docs/TRADE_PLAN.md.

Sources are kept apart: the candidate prices come from a code-computed snapshot, the selection from a COMMITTED debate
record (outbox transaction, ADR-0013), the restriction from the independent risk engine. The final decision is WAIT
unless every gate passes; nothing here can loosen a risk restriction, and no account information is invented
(no balance, position or size exists in a plan).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

import pandas as pd

from aqe.contracts.context import ContextAssessment, ContextStatus
from aqe.contracts.core import Disposition, InstrumentKey, iso_utc
from aqe.contracts.debate import DebateRecord
from aqe.contracts.plan import (
    CheckOutcome,
    ConditionStatus,
    PlanCandidateSet,
    PlanDecision,
    PlanSelectionRecord,
    PlanSide,
    PlanStatus,
    SelectionSource,
    SelectionStatus,
    TradePlan,
)
from aqe.contracts.risk import RiskDecision


def primary_of(rec: DebateRecord | None, h: int) -> tuple[str | None, str | None, tuple[str, ...]]:
    """(canonical id, direction, invalidation conditions) of the debate's primary hypothesis for horizon h."""
    if rec is None or rec.synthesis is None:
        return None, None, ()
    hz = next((x for x in rec.synthesis.horizons if x.horizon_seconds == h), None)
    if hz is None or hz.primary is None:
        return None, None, ()
    return rec.hypothesis_id_map.get(hz.primary.hypothesis_id), hz.primary.direction.value, tuple(hz.primary.invalidation_conditions)


def build_plan(*, inst: InstrumentKey, horizon_seconds: int, decision_time: datetime, risk: RiskDecision,
               snapshot: PlanCandidateSet | None, selection: PlanSelectionRecord | None, source: SelectionSource,
               source_note: str, primary: tuple[str | None, str | None, tuple[str, ...]] = (None, None, ())) -> TradePlan:
    h = horizon_seconds
    wait: list[str] = []
    if risk.disposition != Disposition.REVIEW_CANDIDATE:
        wait.append(f"risk disposition {risk.disposition.value} (independent rules R1-R6): " + "; ".join(risk.reasons[:3]))
    block = snapshot.horizon(h) if snapshot is not None else None
    cands = block.candidates if block is not None else ()
    if block is None:
        wait.append("no candidate snapshot for this horizon")
    elif not cands:
        wait += [f"no candidate: {r}" for r in block.no_candidate_reasons]
    if source == SelectionSource.UNAVAILABLE:
        wait.append(f"no AI selection: {source_note}")
    elif source == SelectionSource.SCRIPTED_TEST:
        wait.append("offline_scripted test response — not an AI selection")
    selected = None
    sel_status = selection.status if selection is not None else SelectionStatus.ABSENT
    if source == SelectionSource.UNAVAILABLE or selection is None:
        status = PlanStatus.CANDIDATES_ONLY if cands else PlanStatus.NO_CANDIDATES
        if source != SelectionSource.UNAVAILABLE:
            wait.append("the debate gave no selection for this horizon")
    elif selection.status != SelectionStatus.ACCEPTED:
        status = PlanStatus.SELECTION_REJECTED
        wait.append(f"selection {selection.status.value}: {selection.detail}")
    elif selection.candidate_id is None:
        status = PlanStatus.WAIT_SELECTED
        wait.append(f"the synthesis chose to wait: {selection.reason}")
    else:
        cand = snapshot.candidate(selection.candidate_id) if snapshot is not None else None
        if cand is None or cand.horizon_seconds != h or snapshot.snapshot_id != selection.snapshot_id:
            status = PlanStatus.SELECTION_REJECTED
            wait.append(f"selected candidate {selection.candidate_id} is not in the referenced snapshot (identity mismatch)")
        elif cand.valid_until <= decision_time:
            status = PlanStatus.EXPIRED  # never moved onto newer candidates, even with identical prices
            wait.append(f"selection {cand.candidate_id} expired at {iso_utc(cand.valid_until)} (as_of {iso_utc(cand.as_of)}): "
                        "a new analysis is required")
        else:
            status, selected = PlanStatus.SELECTED, cand
            if snapshot.as_of > decision_time:
                wait.append("candidate snapshot is later than the decision time")
            wait += [f"check {c.name} {c.outcome.value}: {c.detail}" for c in cand.checks if c.outcome != CheckOutcome.PASS]
            wait += [f"entry condition {e.condition_id} {e.status.value}: {e.text} (observed {e.observed})"
                     for e in cand.entry_conditions if e.status != ConditionStatus.MET]
            pdir = primary[1]
            if (cand.side == PlanSide.LONG and pdir == "DOWN") or (cand.side == PlanSide.SHORT and pdir == "UP"):
                wait.append(f"the selected {cand.side.value} candidate conflicts with the primary hypothesis direction {pdir}")
    decision = PlanDecision(selected.side.value) if (selected is not None and not wait) else PlanDecision.WAIT
    if decision == PlanDecision.WAIT and not wait:
        wait.append("no selected candidate")
    key = [snapshot.snapshot_id if snapshot else None, h, selected.candidate_id if selected else None, iso_utc(decision_time),
           risk.disposition.value, decision.value, sel_status.value]
    return TradePlan(
        plan_id="tp-" + hashlib.sha256(json.dumps(key).encode()).hexdigest()[:20], instrument=inst, horizon_seconds=h,
        as_of=snapshot.as_of if snapshot else None, decision_time=decision_time,
        valid_until=selected.valid_until if selected else (cands[0].valid_until if cands else None),
        algorithm_version=snapshot.algorithm_version if snapshot else None, config_hash=snapshot.config_hash if snapshot else None,
        input_hash=snapshot.input_hash if snapshot else None, snapshot_id=snapshot.snapshot_id if snapshot else None,
        status=status, proposal_side=selected.side if selected else None, decision=decision, candidates=tuple(cands),
        no_candidate_reasons=block.no_candidate_reasons if block is not None else (),
        selected_candidate_id=selected.candidate_id if selected else None, selected=selected, selection_source=source,
        selection_status=sel_status, selection_reason=selection.reason if selection is not None else None,
        evidence_ids=selection.evidence_ids if selection is not None else (),
        counter_evidence_ids=selection.counter_evidence_ids if selection is not None else (),
        hypothesis_ids=(primary[0],) if primary[0] else (),
        invalidation_conditions=(*(selected.invalidation if selected else ()), *primary[2]),
        wait_reasons=tuple(wait), risk_disposition=risk.disposition)


def plans_for_analysis(cfg, inst: InstrumentKey, as_of: datetime, decision_time: datetime, bars: pd.DataFrame | None,
                       risks: list[tuple[int, RiskDecision]], context: ContextAssessment | None,
                       input_label: str = "REST 5m bars") -> tuple[list[TradePlan], dict[str, Any]]:
    """Plans for `aqe analyze`. A selection is used only from the committed debate record of the context assessment,
    against the candidate snapshot stored in THAT assessment's bundle (same snapshot id) — prices are never recomputed
    for it. Without a usable selection the plan shows the technical candidates computed at this analysis' as_of."""
    from aqe.context.debate import load_debate
    from aqe.context.store import load_bundle
    from aqe.planning.candidates import candidates_for_config

    now = candidates_for_config(cfg, inst, as_of, bars=bars, input_label=input_label)
    source, note, rec, ctx = SelectionSource.UNAVAILABLE, "no usable aqe_debate assessment (offline_scripted results are never used for analysis)", None, None
    if context is not None and context.backend == "aqe_debate":
        rec = load_debate(cfg.paths.data_root, inst, context.assessment_id)
        bundle = load_bundle(cfg.paths.data_root, context.bundle_id) if context.bundle_id else None
        if context.status not in (ContextStatus.OK, ContextStatus.PARTIAL):
            note = f"context assessment {context.status.value}"
        elif rec is None:
            note = "debate record missing"
        elif bundle is None or bundle.plan_candidates is None or rec.plan_snapshot_id != bundle.plan_candidates.snapshot_id:
            note = "the debate ran without a candidate snapshot, or its snapshot does not match the record"
        else:
            ctx = bundle.plan_candidates
            source = SelectionSource.SCRIPTED_TEST if context.provider == "offline_scripted" else SelectionSource.LLM
            note = f"debate {rec.debate_id} ({context.provider}/{context.model_id}) at as_of {iso_utc(context.as_of)}"
    plans = []
    for h, risk in risks:
        sel = next((r for r in rec.plan_selections if r.horizon_seconds == h), None) if ctx is not None else None
        plans.append(build_plan(inst=inst, horizon_seconds=h, decision_time=decision_time, risk=risk,
                                snapshot=ctx if (ctx is not None and sel is not None) else now, selection=sel, source=source,
                                source_note=note, primary=primary_of(rec if ctx is not None else None, h)))
    meta = {"enabled": True, "algorithm_version": now.algorithm_version, "config_hash": now.config_hash,
            "analysis_snapshot_id": now.snapshot_id, "context_snapshot_id": ctx.snapshot_id if ctx else None,
            "selection_source": source.value, "selection_note": note, "input": now.input_description,
            "last_bar_close": iso_utc(now.last_bar_close) if now.last_bar_close else None,
            "costs": now.costs.model_dump(mode="json") if now.costs else None, "tick": now.spec_note}
    return plans, meta
