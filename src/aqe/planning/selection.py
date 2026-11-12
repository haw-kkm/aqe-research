"""Code checks on the synthesis' plan selection (docs/TRADE_PLAN.md).

The synthesis may name ONE candidate id per horizon from the bundle's own snapshot (or null = wait) and explain why.
Refused (selection REJECTED, the debate's hypotheses are unaffected): extra fields such as prices (contract), ids that
are unknown / of another horizon / of another bundle / expired / not selectable, the same id for two horizons, missing
or unknown evidence ids, a reason that states a price-like number that is neither a candidate price nor a value in
the evidence, and a selection that appears only after the format-only repair. A failed selection is never replaced
by another candidate.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from aqe.contracts.context import EvidenceBundle
from aqe.contracts.plan import PlanSelection, PlanSelectionRecord, SelectionStatus
from aqe.planning.candidates import numbers_in, snapshot_prices

PENDING_COMMIT = "passed code checks (pending commit)"


def check_plan_selections(raw: dict[Any, Any], bundle: EvidenceBundle, before_fix: str | None = None,
                          format_fixed: bool = False) -> tuple[dict[int, PlanSelection], tuple[PlanSelectionRecord, ...], list[str]]:
    """raw = {horizon_seconds: the synthesis' raw plan_selection object}. Returns (contract-valid selections by horizon,
    one verdict per snapshot horizon, failures)."""
    snap = bundle.plan_candidates
    failures: list[str] = []
    if snap is None:
        if raw:
            failures.append("plan_selection given but the bundle has no plan candidates: ignored")
        return {}, (), failures
    raw_by_h: dict[int, Any] = {}
    for k, v in raw.items():
        try:
            raw_by_h[int(k)] = v
        except (TypeError, ValueError):
            failures.append("plan_selection on a horizon without a valid horizon_seconds ignored")
    valid_ev = bundle.evidence_ids()
    allowed = snapshot_prices(snap) + [n for it in bundle.items for n in numbers_in(it.excerpt)]
    refs = [float(h.reference_price) for h in snap.horizons if h.reference_price is not None]

    def invented_prices(text: str) -> list[float]:
        return sorted({x for x in numbers_in(text) if any(0.5 * r <= x <= 1.5 * r for r in refs)
                       and not any(abs(x - a) <= 0.01 for a in allowed)})

    parsed: dict[int, PlanSelection] = {}
    records: list[PlanSelectionRecord] = []
    chosen: dict[str, int] = {}
    for hz in snap.horizons:
        h = hz.horizon_seconds
        base = dict(horizon_seconds=h, snapshot_id=snap.snapshot_id)
        if h not in raw_by_h:
            records.append(PlanSelectionRecord(**base, status=SelectionStatus.ABSENT, detail="the synthesis gave no plan_selection"))
            continue
        try:
            sel = PlanSelection.model_validate(raw_by_h[h])
        except ValidationError as exc:
            extra = sorted({str(e["loc"][-1]) for e in exc.errors() if e["type"] == "extra_forbidden"})
            detail = f"contract violation ({exc.error_count()} errors)" + (
                f": fields {extra} are not allowed — prices are fixed by code, the LLM may only select an id" if extra else "")
            records.append(PlanSelectionRecord(**base, status=SelectionStatus.REJECTED, detail=detail))
            failures.append(f"plan selection {h}s: {detail}")
            continue
        parsed[h] = sel
        problems: list[str] = []
        cand = None
        bad_ev = sorted((set(sel.evidence_ids) | set(sel.counter_evidence_ids)) - valid_ev)
        if bad_ev:
            problems.append(f"unknown evidence ids {bad_ev[:5]}")
        if sel.candidate_id is not None:
            cand = snap.candidate(sel.candidate_id)
            if cand is None:
                problems.append(f"unknown candidate id {sel.candidate_id!r} (not in this bundle's snapshot {snap.snapshot_id})")
            elif cand.horizon_seconds != h:
                problems.append(f"candidate {sel.candidate_id} belongs to horizon {cand.horizon_seconds}s, not {h}s")
            elif cand.instrument != bundle.instruments[0] or cand.as_of != bundle.as_of:
                problems.append(f"candidate {sel.candidate_id} is for another instrument/as_of")
            elif cand.valid_until <= bundle.as_of:
                problems.append(f"candidate {sel.candidate_id} expired at {cand.valid_until.isoformat()}")
            elif not cand.selectable:
                fails = [c.name for c in cand.checks if c.outcome.value == "FAIL"]
                problems.append(f"candidate {sel.candidate_id} failed code checks {fails} and is not selectable")
            if sel.candidate_id in chosen:
                problems.append(f"candidate {sel.candidate_id} is already selected for horizon {chosen[sel.candidate_id]}s")
            chosen.setdefault(sel.candidate_id, h)
            if not sel.evidence_ids:
                problems.append("a selection must cite evidence ids")
            if format_fixed and (before_fix is None or sel.candidate_id not in before_fix):
                problems.append("the selection appears only in the format-only repair (a repair may not add a selection)")
        invented = invented_prices(sel.reason)
        if invented:
            problems.append(f"the reason states price(s) {invented[:3]} that are neither candidate prices nor evidence values "
                            "(the LLM may not create prices)")
        side = cand.side if cand is not None else None
        common = dict(**base, candidate_id=sel.candidate_id, side=side, reason=sel.reason, evidence_ids=sel.evidence_ids,
                      counter_evidence_ids=sel.counter_evidence_ids)
        if problems:
            records.append(PlanSelectionRecord(**common, status=SelectionStatus.REJECTED, detail="; ".join(problems)))
            failures.append(f"plan selection {h}s rejected: {'; '.join(problems)}")
        else:
            records.append(PlanSelectionRecord(**common, status=SelectionStatus.ACCEPTED, detail=PENDING_COMMIT))
    stray = sorted(set(raw_by_h) - {hz.horizon_seconds for hz in snap.horizons})
    if stray:
        failures.append(f"plan_selection for horizons {stray} without a candidate block ignored")
    return parsed, tuple(records), failures


def finalize_selections(records: tuple[PlanSelectionRecord, ...], applied: bool, note: str) -> tuple[PlanSelectionRecord, ...]:
    """At commit (inside the ledger/outbox transaction): a selection counts only if the debate's ledger batch is applied.
    A late / stale / refused result never overrides a newer plan."""
    out = []
    for r in records:
        if r.status != SelectionStatus.ACCEPTED:
            out.append(r)
        elif applied:
            out.append(r.model_copy(update={"detail": "passed code checks; committed with the ledger batch"}))
        else:
            out.append(r.model_copy(update={"status": SelectionStatus.NOT_COMMITTED,
                                            "detail": f"debate result not applied ({note}); selection not used"}))
    return tuple(out)
