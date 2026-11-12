"""Trade-plan candidates and review proposals (docs/TRADE_PLAN.md).

Synthetic inputs only (explicit 5m bar paths, temp roots, scripted LLM). These are contract / invariant / failure tests
— not a simulation, not a backtest and not evidence of profitability or of an AI's analysis quality.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from aqe.config import PlanningConfig, PlanningProfile
from aqe.context.debate import DebateBackend, activation, active_priors, build_prompts, check_debate, debate_summary, load_debate, load_ledger
from aqe.context.evidence import flow_summary_item
from aqe.context.store import assessments_dir, save_bundle
from aqe.context.worker import process_one
from aqe.contracts.context import (
    ContextAssessment,
    EvidenceBundle,
    SourceHealth,
    SourceHealthStatus,
    SourceKind,
    compute_bundle_hash,
    validate_against_bundle,
)
from aqe.contracts.core import Disposition, InstrumentKey, RuleOutcome
from aqe.contracts.debate import AnalystRole, DebateRecord
from aqe.contracts.plan import (
    CheckOutcome,
    ConditionStatus,
    InstrumentSpec,
    PlanCandidate,
    PlanDecision,
    PlanSelectionRecord,
    PlanSide,
    PlanStatus,
    SelectionSource,
    SelectionStatus,
    TradePlan,
    price_problems,
)
from aqe.contracts.risk import RiskDecision
from aqe.planning.candidates import (
    compute_candidates,
    cost_assumptions,
    dec,
    net_outcomes,
    prompt_text,
    timeframe_bars,
)
from aqe.planning.plan import build_plan, plans_for_analysis
from aqe.planning.selection import check_plan_selections
from aqe.reporting.report import render_trade_plans
from aqe.storage.bars import BAR_COLUMNS
from aqe.storage.metadb import JobQueue, connect
from tests.conftest import ROOT, utc
from tests.test_debate_layer import DB, WORKER_PY, _cfg, _item, hyp, needs_worker, report, result

INST = InstrumentKey(venue="binance", market_type="usdm_perpetual", instrument_id="BTCUSDT")
T0 = utc(2026, 1, 10, 0, 0, 0)
# small explicit dev profile for synthetic paths (1h horizon on 5m levels)
PCFG = PlanningConfig(enabled=True, profiles={3600: PlanningProfile(timeframe_seconds=300, lookback_bars=60, pivot_width=2, atr_period=5)},
                      entry_buffer_atr=1.0, stop_buffer_atr=0.5)
COSTS = cost_assumptions(5.0, 2.0, "test fee/slippage")
# up to a swing high (110.3), down to a swing low (99.7), price back inside the long zone at as_of
LONG_PATH = [105, 106, 107, 108, 109, 110, 109, 108, 107, 106, 105, 104, 103, 102, 101, 100, 100.2, 100.0]
SHORT_PATH = [200 - x for x in LONG_PATH]  # mirrored: swing low 89.7, swing high 100.3, price inside the short zone


def bars_from(closes, start=T0, latency=2.0, inst=INST) -> pd.DataFrame:
    rows = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        ot = pd.Timestamp(start) + pd.Timedelta(minutes=5 * i)
        rows.append({"venue": inst.venue.value, "market_type": inst.market_type.value, "instrument_id": inst.instrument_id,
                     "interval_seconds": 300, "open_time": ot, "close_time": ot + pd.Timedelta(minutes=5), "open": float(o),
                     "high": max(o, c) + 0.3, "low": min(o, c) - 0.3, "close": float(c), "volume": 1.0, "quote_volume": float(c),
                     "trade_count": 1, "taker_buy_base": 0.5, "taker_buy_quote": c / 2, "is_closed": True,
                     "provenance": "SYNTHETIC_FIXTURE", "received_at": ot + pd.Timedelta(minutes=5, seconds=latency),
                     "available_at": ot + pd.Timedelta(minutes=5, seconds=latency), "source_id": int(ot.value // 1_000_000),
                     "ingest_run_id": "t"})
    return pd.DataFrame(rows)[BAR_COLUMNS]


def close_time(i: int):
    return T0 + timedelta(minutes=5 * (i + 1))


AS_OF = close_time(len(LONG_PATH) - 1) + timedelta(seconds=10)


def spec(tick="0.1", at=AS_OF) -> InstrumentSpec:
    return InstrumentSpec(instrument=INST, tick_size=Decimal(tick), source_id="test import", available_at=at - timedelta(days=1))


def snap(path=LONG_PATH, as_of=AS_OF, costs=COSTS, sp=None, pcfg=PCFG, horizons=(3600,), bars=None):
    return compute_candidates(INST, as_of, bars if bars is not None else bars_from(path), list(horizons), pcfg, costs, sp,
                              "test tick" if sp else "", ("mkt-bars-1",))


def only(s, side=None):
    cs = [c for h in s.horizons for c in h.candidates if side is None or c.side == side]
    assert len(cs) == 1, (cs, [h.no_candidate_reasons for h in s.horizons])
    return cs[0]


def risk(disp=Disposition.REVIEW_CANDIDATE, at=AS_OF):
    ok = disp == Disposition.REVIEW_CANDIDATE
    return RiskDecision(analysis_id="a", horizon_seconds=3600, ruleset_id="t", ruleset_hash="h", rules=(),
                        risk_outcome=RuleOutcome.PASS if ok else RuleOutcome.FAIL, disposition=disp,
                        reasons=() if ok else (f"R1_READINESS_ARTIFACT: FAIL - test restriction {disp.value}",),
                        portfolio_status="UNKNOWN", decided_at=at, valid_until=at + timedelta(hours=1))


def accepted(c: PlanCandidate, s) -> PlanSelectionRecord:
    return PlanSelectionRecord(horizon_seconds=c.horizon_seconds, snapshot_id=s.snapshot_id, status=SelectionStatus.ACCEPTED,
                               candidate_id=c.candidate_id, side=c.side, reason="synthetic", evidence_ids=("mkt-bars-1",))


# ------------------------------------------------------------------ 1. price relations, conservative entry, costs, tick
def test_price_relations_are_strict_for_long_and_short():
    D = Decimal
    assert price_problems(PlanSide.LONG, D("100"), D("101"), D("99"), [D("105"), D("107")]) == []
    assert price_problems(PlanSide.SHORT, D("99"), D("100"), D("101"), [D("95"), D("93")]) == []
    assert any("inverted" in p for p in price_problems(PlanSide.LONG, D("101"), D("100"), D("99"), [D("105")]))
    assert any("stop distance" in p for p in price_problems(PlanSide.LONG, D("100"), D("101"), D("100"), [D("105")]))  # zero distance
    assert any("stop distance" in p for p in price_problems(PlanSide.SHORT, D("99"), D("100"), D("100"), [D("95")]))
    assert any("tp1" in p for p in price_problems(PlanSide.LONG, D("100"), D("101"), D("99"), [D("101")]))
    assert any("tp2" in p for p in price_problems(PlanSide.LONG, D("100"), D("101"), D("99"), [D("105"), D("104")]))
    assert any("tp2" in p for p in price_problems(PlanSide.SHORT, D("99"), D("100"), D("101"), [D("95"), D("96")]))
    for bad in (D("NaN"), D("Infinity"), D("-1"), D("0")):
        assert any("non-finite or non-positive" in p for p in price_problems(PlanSide.LONG, bad, D("101"), D("99"), [D("105")]))
    for x in (float("nan"), float("inf"), Decimal("NaN")):
        with pytest.raises(ValueError):
            dec(x)
    c = only(snap(), PlanSide.LONG)
    with pytest.raises(ValidationError):  # the contract enforces the same relations (here: stop above the zone)
        PlanCandidate.model_validate({**c.model_dump(), "stop": Decimal("101")})


def test_rr_uses_the_conservative_zone_end_and_round_trip_costs():
    from aqe.evaluation.costs import trade_return

    s = snap()
    lc, sc = only(s, PlanSide.LONG), only(snap(SHORT_PATH), PlanSide.SHORT)
    assert lc.conservative_entry == lc.entry_high and sc.conservative_entry == sc.entry_low
    for c, side in ((lc, 1), (sc, -1)):
        tp = c.take_profits[0]
        loss, rewards = net_outcomes(c.side, c.conservative_entry, c.stop, [tp.price], COSTS, AS_OF, 3600)
        assert tp.net_rr == round(rewards[0] / loss, 4) and c.net_loss_fraction == round(loss, 8)
        # the favourable end would show a larger RR — it is never used
        fav = c.entry_low if side == 1 else c.entry_high
        l2, r2 = net_outcomes(c.side, fav, c.stop, [tp.price], COSTS, AS_OF, 3600)
        assert r2[0] / l2 > rewards[0] / loss
        # fees + adverse slippage on both legs, via the existing cost function (no second implementation)
        t0, t1 = pd.Timestamp(AS_OF), pd.Timestamp(AS_OF) + pd.Timedelta(hours=1)
        gross = trade_return(side, float(c.conservative_entry), float(tp.price), t0, t1, 0.0, 0.0, None).net_return
        assert rewards[0] == trade_return(side, float(c.conservative_entry), float(tp.price), t0, t1, 5.0, 2.0, None).net_return < gross
    assert "funding not included" in lc.checks[[k.name for k in lc.checks].index("costs")].detail


def test_non_positive_net_reward_after_costs_is_rejected():
    c = only(snap(costs=cost_assumptions(500.0, 200.0, "absurd test costs")), PlanSide.LONG)  # costs eat the whole target
    checks = {k.name: k.outcome for k in c.checks}
    assert checks["net_reward_tp1"] == CheckOutcome.FAIL and checks["net_rr_tp1"] == CheckOutcome.FAIL
    assert not c.selectable and c.take_profits[0].net_reward_fraction <= 0


def test_tick_rounding_is_directional_and_relations_are_rechecked():
    raw = only(snap(), PlanSide.LONG)
    c = only(snap(sp=spec("0.1")), PlanSide.LONG)
    tick = Decimal("0.1")
    assert all((x / tick) == (x / tick).to_integral_value() for x in (c.entry_low, c.entry_high, c.stop, c.take_profits[0].price))
    assert c.entry_low <= raw.entry_low and c.entry_high >= raw.entry_high  # zone widened
    assert c.stop <= raw.stop and c.take_profits[0].price <= raw.take_profits[0].price  # farther stop, closer target
    sc_raw, sc = only(snap(SHORT_PATH), PlanSide.SHORT), only(snap(SHORT_PATH, sp=spec("0.1")), PlanSide.SHORT)
    assert sc.stop >= sc_raw.stop and sc.take_profits[0].price >= sc_raw.take_profits[0].price
    assert {k.name: k.outcome for k in c.checks}["tick_size"] == CheckOutcome.PASS
    # a coarse tick collapses the relations after rounding -> no candidate, with the reason (never a "fixed" price)
    s = snap(sp=spec("5"))
    assert not s.horizons[0].candidates
    assert any("after tick rounding" in r for r in s.horizons[0].no_candidate_reasons)


def test_missing_costs_or_tick_stay_unknown_and_the_plan_waits():
    s = snap(costs=None)
    c = only(s, PlanSide.LONG)
    checks = {k.name: k for k in c.checks}
    assert checks["costs"].outcome == CheckOutcome.UNKNOWN and checks["tick_size"].outcome == CheckOutcome.UNKNOWN
    assert c.net_loss_fraction is None and all(t.net_rr is None and t.net_reward_fraction is None for t in c.take_profits)
    assert c.selectable and not c.verified  # selectable, but it can never become a LONG/SHORT decision
    p = build_plan(inst=INST, horizon_seconds=3600, decision_time=AS_OF, risk=risk(), snapshot=s, selection=accepted(c, s),
                   source=SelectionSource.LLM, source_note="synthetic")
    assert p.decision == PlanDecision.WAIT and p.proposal_side == PlanSide.LONG
    assert any("check costs UNKNOWN" in w for w in p.wait_reasons) and any("check tick_size UNKNOWN" in w for w in p.wait_reasons)


# ------------------------------------------------------------------ 2. no candidate / missing data -> WAIT with reasons
def test_missing_data_gives_no_candidate_with_an_explicit_reason():
    empty = compute_candidates(INST, AS_OF, pd.DataFrame(columns=BAR_COLUMNS), [3600], PCFG, COSTS)
    assert empty.horizons[0].no_candidate_reasons == ("no completed bars available at as_of",) and empty.last_bar_close is None
    stale = snap(as_of=AS_OF + timedelta(hours=2))
    assert stale.horizons[0].no_candidate_reasons[0].startswith("STALE input")
    other = snap(horizons=(3600, 86400))
    assert other.horizon(86400).no_candidate_reasons == ("no planning profile for horizon 86400s",)
    flat = snap(path=[100.0] * 18)  # no swing levels at all
    assert not flat.horizons[0].candidates and any("no intact confirmed" in r for r in flat.horizons[0].no_candidate_reasons)
    for s in (empty, stale, flat):
        p = build_plan(inst=INST, horizon_seconds=3600, decision_time=AS_OF, risk=risk(), snapshot=s, selection=None,
                       source=SelectionSource.UNAVAILABLE, source_note="no debate")
        assert p.decision == PlanDecision.WAIT and p.status == PlanStatus.NO_CANDIDATES and p.selected is None
        assert any(w.startswith("no candidate:") for w in p.wait_reasons)


def test_a_plan_carries_no_account_information_and_unknown_portfolio_stays_unknown():
    tokens = {t for f in (*TradePlan.model_fields, *PlanCandidate.model_fields) for t in f.split("_")}
    for word in ("balance", "equity", "quantity", "size", "leverage", "position", "notional", "order", "account"):
        assert word not in tokens
    s = snap()
    r = risk(Disposition.RESEARCH_ONLY)
    p = build_plan(inst=INST, horizon_seconds=3600, decision_time=AS_OF, risk=r, snapshot=s, selection=None,
                   source=SelectionSource.UNAVAILABLE, source_note="x")
    assert p.risk_disposition == Disposition.RESEARCH_ONLY and r.portfolio_status == "UNKNOWN"


# ------------------------------------------------------------------ 3. point in time
def test_future_or_revised_bars_never_change_a_past_snapshot():
    bars = bars_from(LONG_PATH)
    base = snap(bars=bars)
    later = bars_from(LONG_PATH + [95.0, 90.0, 85.0, 80.0])  # the support would be broken later
    revised = bars.iloc[[-1]].copy()
    revised[["close", "low"]] = [80.0, 79.7]  # a revision of the last bar published after as_of
    revised["available_at"] = revised["received_at"] = pd.Timestamp(AS_OF) + pd.Timedelta(seconds=30)
    for b in (later, pd.concat([bars, revised], ignore_index=True)):
        again = snap(bars=b)
        assert again.snapshot_id == base.snapshot_id and only(again).candidate_id == only(base).candidate_id
    assert snap(bars=bars).snapshot_id == base.snapshot_id  # deterministic


def test_a_pivot_is_used_only_after_its_confirmation_bars_are_available():
    last = len(LONG_PATH) - 1
    bars = bars_from(LONG_PATH)
    before = snap(bars=bars, as_of=close_time(last) + timedelta(seconds=1))  # bar 17 (2nd right-side bar) not yet available
    assert not any(c.side == PlanSide.LONG for h in before.horizons for c in h.candidates)
    at = snap(bars=bars, as_of=close_time(last) + timedelta(seconds=2))
    c = only(at, PlanSide.LONG)
    support = c.levels[0]
    assert support.kind == "SWING_LOW" and support.confirmed_at == close_time(last) + timedelta(seconds=2)
    assert support.pivot_time == T0 + timedelta(minutes=5 * 15) and support.confirmed_at > support.pivot_time
    assert all(lv.confirmed_at <= c.as_of for lv in c.levels) and c.available_at <= c.as_of


def test_other_instruments_unavailable_and_partial_bars_are_not_used():
    eth = InstrumentKey(venue="binance", market_type="usdm_perpetual", instrument_id="ETHUSDT")
    mixed = pd.concat([bars_from(LONG_PATH), bars_from([10.0, 11.0], inst=eth)], ignore_index=True)
    with pytest.raises(ValueError, match="another instrument"):
        snap(bars=mixed)
    b = bars_from(LONG_PATH)  # 18 bars = 1 complete hour + a partial hour
    hourly = timeframe_bars(b, 3600)
    assert len(hourly) == 1 and hourly.index[0] == pd.Timestamp(T0)  # the partial hour is never a high/low
    assert hourly["high"].iloc[0] == b["high"].iloc[:12].max()
    unclosed = b.copy()
    unclosed.loc[unclosed.index[-1], "is_closed"] = False
    assert snap(bars=unclosed).last_bar_close == close_time(len(LONG_PATH) - 2)


# ------------------------------------------------------------------ 4. selection checks (synthesis -> code)
def plan_bundle(s, as_of=AS_OF, ids=("mkt-bars-1", "mkt-daily-1", "mkt-funding-1", "aqe-num-h3600-1")) -> EvidenceBundle:
    items = tuple(_item(i, SourceKind.AQE_NUMERIC if i.startswith("aqe-num") else SourceKind.MARKET_FLOW, as_of) for i in ids)
    health = (SourceHealth(source_kind=SourceKind.MARKET_FLOW, status=SourceHealthStatus.AVAILABLE),
              SourceHealth(source_kind=SourceKind.NEWS, status=SourceHealthStatus.NOT_CONFIGURED))
    h = compute_bundle_hash((INST,), as_of, items, (), health, s)
    return EvidenceBundle(bundle_id=f"b-{h[:8]}", instruments=(INST,), as_of=as_of, items=items, source_health=health,
                          bundle_hash=h, plan_candidates=s)


def syn(sel=None, horizons=(3600,)):
    out = {"horizons": [{"horizon_seconds": h, "primary": hyp(f"p{h}", h), "opposing": [], "unresolved": [], "changed_since_previous": ""}
                        for h in horizons], "prior_decisions": [], "limitations": []}
    if sel is not None:
        out["horizons"][0]["plan_selection"] = sel
    return out


def sel(cid, reason="synthetic", ev=("mkt-bars-1",), **extra):
    return {"candidate_id": cid, "reason": reason, "evidence_ids": list(ev), "counter_evidence_ids": [], **extra}


def checked(b, sel_obj, horizons=(3600,)):
    return check_debate(result(synth=syn(sel_obj, horizons), drafts={"CHART": report("CHART", "mkt-bars-1")}), b,
                        [AnalystRole.CHART], list(horizons), [])


def test_a_valid_selection_or_an_explicit_wait_is_accepted():
    s = snap()
    b = plan_bundle(s)
    c = only(s)
    ok = checked(b, sel(c.candidate_id))
    rec = ok["plan_selections"][0]
    assert rec.status == SelectionStatus.ACCEPTED and rec.side == PlanSide.LONG and ok["synthesis"].horizons[0].plan_selection.candidate_id == c.candidate_id
    wait = checked(b, sel(None, "range-bound; wait"))["plan_selections"][0]
    assert wait.status == SelectionStatus.ACCEPTED and wait.candidate_id is None
    absent = checked(b, None)["plan_selections"][0]
    assert absent.status == SelectionStatus.ABSENT


@pytest.mark.parametrize("case", ["ghost", "other_bundle", "other_horizon", "price_field", "price_text", "no_evidence",
                                  "unknown_evidence", "unselectable", "expired"])
def test_the_llm_cannot_pick_foreign_ids_or_create_prices(case):
    s = snap(horizons=(3600,))
    c = only(s)
    raw = sel(c.candidate_id)
    b = plan_bundle(s)
    if case == "ghost":
        raw = sel("pc-000000000000deadbeef")
    elif case == "other_bundle":  # a real candidate of another snapshot (other as_of) is not in this bundle
        raw = sel(only(snap(as_of=AS_OF - timedelta(seconds=5))).candidate_id)
    elif case == "other_horizon":
        p2 = PlanningConfig(enabled=True, profiles={**PCFG.profiles, 7200: PCFG.profiles[3600]}, entry_buffer_atr=1.0, stop_buffer_atr=0.5)
        s = snap(pcfg=p2, horizons=(3600, 7200))
        b = plan_bundle(s)
        raw = sel(next(c2.candidate_id for c2 in s.horizon(7200).candidates))
    elif case == "price_field":
        raw = sel(c.candidate_id, entry_price=99.5)
    elif case == "price_text":
        raw = sel(c.candidate_id, reason="buy at 100.45 and sell at 111.2")
    elif case == "no_evidence":
        raw = sel(c.candidate_id, ev=())
    elif case == "unknown_evidence":
        raw = sel(c.candidate_id, ev=("ghost-ev",))
    elif case == "unselectable":
        s = snap(costs=cost_assumptions(500.0, 200.0, "absurd"))
        b = plan_bundle(s)
        raw = sel(only(s).candidate_id)
    elif case == "expired":  # a committed selection shown after its candidate expired is not re-attached to anything newer
        p = build_plan(inst=INST, horizon_seconds=3600, decision_time=c.valid_until, risk=risk(), snapshot=s,
                       selection=accepted(c, s), source=SelectionSource.LLM, source_note="x")
        assert p.status == PlanStatus.EXPIRED and p.decision == PlanDecision.WAIT and p.selected is None
        assert any("new analysis is required" in w for w in p.wait_reasons)
        return
    out = checked(b, raw, horizons=tuple(h.horizon_seconds for h in s.horizons) if case == "other_horizon" else (3600,))
    assert out["synthesis"] is not None  # a bad selection never voids the hypotheses ...
    rec = out["plan_selections"][0]
    assert rec.status == SelectionStatus.REJECTED  # ... the selection alone is rejected
    assert any("plan selection" in f for f in out["failures"]) and out["status"] == "PARTIAL"
    expect = {"ghost": "unknown candidate id", "other_bundle": "unknown candidate id", "other_horizon": "belongs to horizon",
              "price_field": "entry_price", "price_text": "neither candidate prices", "no_evidence": "must cite evidence",
              "unknown_evidence": "unknown evidence ids", "unselectable": "not selectable"}[case]
    assert expect in rec.detail
    # and a rejected selection is never replaced by another candidate in the plan
    p = build_plan(inst=INST, horizon_seconds=3600, decision_time=AS_OF, risk=risk(), snapshot=s, selection=rec,
                   source=SelectionSource.LLM, source_note="x")
    assert p.decision == PlanDecision.WAIT and p.selected is None and p.status == PlanStatus.SELECTION_REJECTED


def test_one_candidate_cannot_be_selected_for_two_horizons_and_a_repair_cannot_add_a_selection():
    s = snap()
    b = plan_bundle(s)
    c = only(s)
    _, recs, _ = check_plan_selections({3600: sel(c.candidate_id)}, b, before_fix="broken {no selection here", format_fixed=True)
    assert recs[0].status == SelectionStatus.REJECTED and "format-only repair" in recs[0].detail
    _, recs, _ = check_plan_selections({3600: sel(c.candidate_id)}, b, before_fix=f'... "{c.candidate_id}" ...', format_fixed=True)
    assert recs[0].status == SelectionStatus.ACCEPTED  # a repair that only re-formats keeps it
    _, _, fails = check_plan_selections({3600: sel(c.candidate_id), 86400: sel(c.candidate_id)}, b)
    assert any("without a candidate block" in f for f in fails)


def test_debate_never_changes_candidate_prices_or_numeric_estimates(fixture_cfg, tmp_path):
    s = snap(sp=spec())
    b = plan_bundle(s)
    before = b.model_dump_json()
    cfg = _cfg(fixture_cfg, tmp_path)
    be = DebateBackend(cfg, JobQueue(DB(tmp_path)), project_root=ROOT, horizons=[3600])
    c = only(s)
    st, notes = activation(b, [r.value for r in AnalystRole])
    a = be.finish(b, result(synth=syn(sel(c.candidate_id)), drafts={"CHART": report("CHART", "mkt-bars-1")}), "debate-p",
                  [AnalystRole.CHART], st, notes, [3600], [], [], {"provider": "offline_scripted", "model_id": "t"}, 0.0, "ph")
    held, a = be.commit(validate_against_bundle(a, b))
    assert held and b.model_dump_json() == before  # bundle (incl. numeric item and candidate snapshot) unchanged
    rec = load_debate(tmp_path, INST, a.assessment_id)
    assert rec.plan_snapshot_id == s.snapshot_id and rec.plan_selections[0].candidate_id == c.candidate_id
    assert rec.plan_selections[0].status == SelectionStatus.ACCEPTED and "committed with the ledger batch" in rec.plan_selections[0].detail
    assert set(rec.plan_selections[0].model_dump()) == set(PlanSelectionRecord.model_fields)  # an id + text, no price fields
    assert any(r.startswith("trade-plan selection: 3600s=" + c.candidate_id) for r in a.status_reasons)
    assert a.contextual_rating is None
    prompts = build_prompts(b, [AnalystRole.CHART, AnalystRole.FLOW], [3600], [])
    assert prompt_text(s) in prompts["synthesis"]["user"] and "plan_selection" in prompts["synthesis"]["system"]
    assert prompt_text(s) in prompts["roles"][0]["user"] and "NOT independent evidence" in prompts["roles"][0]["user"]


# ------------------------------------------------------------------ 5. risk gating: nothing lifts a restriction
def _gated(**kw):
    s = snap(SHORT_PATH if kw.pop("short", False) else LONG_PATH, sp=spec())
    c = only(s)
    args = dict(inst=INST, horizon_seconds=3600, decision_time=AS_OF, risk=risk(), snapshot=s, selection=accepted(c, s),
                source=SelectionSource.LLM, source_note="synthetic real-LLM stand-in")
    args.update(kw)
    return build_plan(**args), c


@pytest.mark.parametrize("short", [False, True])
def test_explicit_synthetic_success_path_is_a_review_proposal_only(short):
    p, c = _gated(short=short)
    assert c.verified and all(e.status == ConditionStatus.MET for e in c.entry_conditions)
    assert p.decision == PlanDecision(c.side.value) and p.status == PlanStatus.SELECTED and p.wait_reasons == ()
    assert p.approval_required is True and p.execution_enabled is False
    with pytest.raises(ValidationError):
        TradePlan.model_validate({**p.model_dump(), "execution_enabled": True})
    with pytest.raises(ValidationError):  # the contract refuses LONG/SHORT under any risk restriction
        TradePlan.model_validate({**p.model_dump(), "risk_disposition": "RESEARCH_ONLY"})
    with pytest.raises(ValidationError):
        TradePlan.model_validate({**p.model_dump(), "selection_source": "SCRIPTED_TEST"})


@pytest.mark.parametrize("case", ["research_only", "abstain", "scripted", "unavailable", "not_committed", "entry_not_met",
                                  "primary_conflict", "llm_wait"])
def test_restrictions_and_test_responses_always_wait(case):
    s = snap(sp=spec())
    c = only(s)
    kw = {}
    if case == "research_only":
        kw = {"risk": risk(Disposition.RESEARCH_ONLY)}
    elif case == "abstain":
        kw = {"risk": risk(Disposition.ABSTAIN)}
    elif case == "scripted":
        kw = {"source": SelectionSource.SCRIPTED_TEST}
    elif case == "unavailable":
        kw = {"source": SelectionSource.UNAVAILABLE, "selection": None}
    elif case == "not_committed":
        kw = {"selection": accepted(c, s).model_copy(update={"status": SelectionStatus.NOT_COMMITTED, "detail": "late"})}
    elif case == "entry_not_met":  # price moved above the zone: the zone touch is not met -> no proposal (and no 'fill')
        s2 = snap(LONG_PATH[:-1] + [100.9, 101.6], sp=spec(), as_of=AS_OF + timedelta(minutes=5))
        c2 = only(s2)
        assert {e.condition_id: e.status for e in c2.entry_conditions}["close_in_zone"] == ConditionStatus.NOT_MET
        kw = {"snapshot": s2, "selection": accepted(c2, s2), "decision_time": AS_OF + timedelta(minutes=5)}
    elif case == "primary_conflict":
        kw = {"primary": ("hyp-1", "DOWN", ("x",))}
    elif case == "llm_wait":
        kw = {"selection": accepted(c, s).model_copy(update={"candidate_id": None, "side": None, "reason": "wait"})}
    p, _ = _gated(**kw)
    assert p.decision == PlanDecision.WAIT and p.wait_reasons and p.execution_enabled is False


# ------------------------------------------------------------------ 6. queue -> worker -> commit -> outbox -> report
class _PlanDebate(DebateBackend):
    """Real DebateBackend; the worker run is replaced by a fixed scripted result that selects a candidate."""

    def __init__(self, *a, bundle_sel=None, **kw):
        super().__init__(*a, **kw)
        self.calls = 0
        self.bundle_sel = bundle_sel

    def assess(self, bundle, leased):
        self.calls += 1
        recs = load_ledger(self.db, INST)
        st, notes = activation(bundle, [r.value for r in AnalystRole])
        cid = only(bundle.plan_candidates).candidate_id if self.bundle_sel is None else self.bundle_sel
        return self.finish(bundle, result(synth=syn(sel(cid)), drafts={"CHART": report("CHART", "mkt-bars-1")}),
                           f"debate-{leased['attempts']}", [AnalystRole.CHART], st, notes, [3600], active_priors(recs, bundle.as_of),
                           recs, {"provider": "offline_scripted", "model_id": "t"}, 0.0, "ph", job=leased)


def _queued(fixture_cfg, root, as_of=AS_OF):
    cfg = _cfg(fixture_cfg, root)
    cfg.planning = PCFG
    s = snap(as_of=as_of, sp=spec())
    b = plan_bundle(s, as_of=as_of)
    save_bundle(root, b)
    q = JobQueue(DB(root), max_attempts=2)
    job = q.enqueue(f"k-{as_of.timestamp()}", "BTCUSDT", as_of, b.bundle_id, "aqe_debate")
    return cfg, q, job, b, _PlanDebate(cfg, q, project_root=ROOT, horizons=[3600])


def test_outbox_recovery_restores_the_same_selection_without_rerun(fixture_cfg, tmp_path, monkeypatch):
    import aqe.context.store as store_mod

    cfg, q, job, b, be = _queued(fixture_cfg, tmp_path)
    real = store_mod.atomic_write_json

    def failing(path, obj):
        if str(path).endswith(".debate.json") or f"{os.sep}assessments{os.sep}" in str(path):
            raise OSError("simulated disk failure after the queue COMMIT")
        return real(path, obj)

    monkeypatch.setattr(store_mod, "atomic_write_json", failing)
    first = process_one(cfg, q, be, "w1")
    assert first["result"].startswith("committed; result files pending") and q.get(job["job_id"])["status"] == "DONE"
    monkeypatch.setattr(store_mod, "atomic_write_json", real)
    aid = q.get(job["job_id"])["result_assessment_id"]
    pending = json.loads(q.outbox_pending()[0]["files_json"])[0]["json"]["plan_selections"]
    assert process_one(cfg, q, be, "w2") == {"recovered": [aid]} and be.calls == 1  # backend (LLM) not run again
    rec = load_debate(tmp_path, INST, aid)
    assert [r.model_dump(mode="json") for r in rec.plan_selections] == pending  # identical selection, id and evidence
    c = b.plan_candidates.candidate(rec.plan_selections[0].candidate_id)
    assert rec.plan_selections[0].status == SelectionStatus.ACCEPTED and rec.plan_snapshot_id == b.plan_candidates.snapshot_id
    a = ContextAssessment.model_validate_json((assessments_dir(tmp_path, INST) / f"{aid}.json").read_text(encoding="utf-8"))
    plans, meta = plans_for_analysis(cfg, INST, AS_OF, AS_OF + timedelta(seconds=10), bars_from(LONG_PATH), [(3600, risk())], a)
    p = plans[0]
    assert p.selected == c and p.snapshot_id == b.plan_candidates.snapshot_id  # same prices as the snapshot the debate saw
    assert p.selection_source == SelectionSource.SCRIPTED_TEST and p.decision == PlanDecision.WAIT
    assert meta["context_snapshot_id"] == b.plan_candidates.snapshot_id


def test_lease_loss_and_late_results_never_commit_a_selection(fixture_cfg, tmp_path):
    cfg, q, job, b, _ = _queued(fixture_cfg, tmp_path)
    taken = {}

    class Slow(_PlanDebate):
        def assess(self, bundle, leased):
            with connect(q.path) as c:
                c.execute("UPDATE context_jobs SET lease_expires_at=? WHERE job_id=?", ("2000-01-01T00:00:00Z", leased["job_id"]))
            taken["job"] = q.lease("replacement")
            return super().assess(bundle, leased)

    r = process_one(cfg, q, Slow(cfg, q, project_root=ROOT, horizons=[3600]), "old")
    assert r["result"].startswith("lease lost") and q.outbox_pending() == []
    lost = [DebateRecord.model_validate_json(p.read_text(encoding="utf-8"))
            for p in (tmp_path / "context" / "debates" / INST.slug()).glob("*.debate.json")]
    assert len(lost) == 1 and lost[0].plan_selections[0].status == SelectionStatus.NOT_COMMITTED
    # late: a newer debate is committed first; the older as_of result is not applied and its selection not used
    root2 = tmp_path / "late"
    cfg2, q2, j_new, b_new, be2 = _queued(fixture_cfg, root2, AS_OF)
    assert process_one(cfg2, q2, be2, "w")["status"]
    b_old = plan_bundle(snap(as_of=AS_OF - timedelta(seconds=5), sp=spec()), as_of=AS_OF - timedelta(seconds=5))
    st, notes = activation(b_old, [r.value for r in AnalystRole])
    old = be2.finish(b_old, result(synth=syn(sel(only(b_old.plan_candidates).candidate_id)), drafts={"CHART": report("CHART", "mkt-bars-1")}),
                     "debate-old", [AnalystRole.CHART], st, notes, [3600], [], [], {"provider": "offline_scripted", "model_id": "t"}, 0.0, "ph")
    held, final = be2.commit(validate_against_bundle(old, b_old))
    rec = load_debate(root2, INST, final.assessment_id)
    assert held and not rec.applied_to_ledger and "late arrival" in rec.ledger_note
    assert rec.plan_selections[0].status == SelectionStatus.NOT_COMMITTED


@needs_worker
@pytest.mark.slow
@pytest.mark.parametrize("mode,expect", [("first", SelectionStatus.ACCEPTED), ("none", SelectionStatus.ACCEPTED),
                                         ("ghost", SelectionStatus.REJECTED), ("price_field", SelectionStatus.REJECTED),
                                         ("price_text", SelectionStatus.REJECTED), ("format_fix_adds_selection", SelectionStatus.REJECTED)])
def test_real_worker_selection_path_uses_no_extra_llm_calls(fixture_cfg, tmp_path, mode, expect):
    cfg = _cfg(fixture_cfg, tmp_path)
    be = DebateBackend(cfg, JobQueue(DB(tmp_path)), project_root=ROOT, horizons=[3600])
    b = plan_bundle(snap(sp=spec()))
    opts = {"format_fix_adds_selection": True} if mode == "format_fix_adds_selection" else {"plan_select": mode}
    a = be.assess(b, {"job_id": f"j-{mode}", "idempotency_key": f"k-{mode}", "scripted_options": opts})
    held, a = be.commit(validate_against_bundle(a, b))
    rec = load_debate(tmp_path, INST, a.assessment_id)
    assert held and rec.plan_selections[0].status == expect, rec.plan_selections
    # drafts (CHART, FLOW) + critic + synthesis (+1 format-only repair) — the selection rides on the synthesis call
    assert rec.llm_calls == (5 if mode == "format_fix_adds_selection" else 4) and rec.llm_calls <= rec.max_llm_calls
    if mode == "first":
        assert rec.plan_selections[0].candidate_id == only(b.plan_candidates).candidate_id
        summary = debate_summary(tmp_path, DB(tmp_path), INST, a.assessment_id, b.as_of)
        assert summary["plan_selections"][0]["status"] == "ACCEPTED"


@needs_worker
@pytest.mark.slow
def test_queue_worker_to_report_path_with_the_scripted_llm(fixture_cfg, tmp_path):
    cfg, q, job, b, _ = _queued(fixture_cfg, tmp_path)
    be = DebateBackend(cfg, q, project_root=ROOT, horizons=[3600])
    r = process_one(cfg, q, be, "worker-1")
    assert q.get(job["job_id"])["status"] == "DONE" and Path(r["path"]).exists()
    a = ContextAssessment.model_validate_json(Path(r["path"]).read_text(encoding="utf-8"))
    rec = load_debate(tmp_path, INST, a.assessment_id)
    assert rec.plan_selections[0].status == SelectionStatus.ACCEPTED and rec.applied_to_ledger
    plans, meta = plans_for_analysis(cfg, INST, AS_OF, AS_OF + timedelta(seconds=10), bars_from(LONG_PATH), [(3600, risk())], a)
    p = plans[0]
    assert p.selection_source == SelectionSource.SCRIPTED_TEST and p.decision == PlanDecision.WAIT  # never an AI choice
    assert p.selected is not None and p.selected.candidate_id == rec.plan_selections[0].candidate_id
    md = "\n".join(render_trade_plans(plans, meta))
    assert "4b. 진입·손절·익절 검토안" in md and "결론: **관망**" in md and "분석 후보 방향: 롱" in md
    assert "offline_scripted test response" in md and "execution_enabled=false" in md


# ------------------------------------------------------------------ 7. back-compat, cache identity, feature off
def test_old_bundles_records_and_reports_still_load():
    from tests.test_debate_layer import make_bundle

    old = make_bundle()
    raw = json.loads(old.model_dump_json())
    raw.pop("plan_candidates")
    again = EvidenceBundle.model_validate(raw)  # stored before trade plans: same hash, no snapshot
    assert again.bundle_hash == compute_bundle_hash(old.instruments, old.as_of, old.items, old.calendar_snapshot, old.source_health)
    assert again.plan_candidates is None
    s = snap()
    b = plan_bundle(s)
    out = checked(b, sel(only(s).candidate_id))
    rec = DebateRecord(debate_id="d", assessment_id="a", instrument=INST, as_of=AS_OF, generated_at=AS_OF, valid_until=AS_OF + timedelta(hours=1),
                       bundle_id=b.bundle_id, bundle_hash=b.bundle_hash, horizons_seconds=(3600,), roles={}, synthesis=out["synthesis"],
                       plan_snapshot_id=s.snapshot_id, plan_selections=out["plan_selections"])
    legacy = json.loads(rec.model_dump_json())
    legacy.pop("plan_snapshot_id"), legacy.pop("plan_selections")
    for hz in legacy["synthesis"]["horizons"]:
        hz.pop("plan_selection")
    old_rec = DebateRecord.model_validate(legacy)
    assert old_rec.plan_selections == () and old_rec.synthesis.horizons[0].plan_selection is None


def test_candidate_content_or_config_changes_the_bundle_hash_and_cache_key():
    from aqe.context.backends import context_cache_key

    def key(bundle):
        return context_cache_key(instrument_slug=INST.slug(), as_of=AS_OF, bundle_hash=bundle.bundle_hash, backend="aqe_debate",
                                 upstream_ref=None, provider="p", model_id="m", prompt_h="x", config_h="c", run_id="r", portfolio_hash=None)

    b1, b1_again = plan_bundle(snap()), plan_bundle(snap())
    b_cfg = plan_bundle(snap(pcfg=PCFG.model_copy(update={"stop_buffer_atr": 0.6})))
    b_tick = plan_bundle(snap(sp=spec()))
    b_data = plan_bundle(snap(path=LONG_PATH[:-1] + [100.1]))
    assert key(b1) == key(b1_again)
    assert len({key(b1), key(b_cfg), key(b_tick), key(b_data)}) == 4
    bare = compute_bundle_hash(b1.instruments, b1.as_of, b1.items, (), b1.source_health)
    assert b1.bundle_hash != bare  # the snapshot is part of the identity; old selections are never reused
    with pytest.raises(ValidationError):  # a snapshot of another as_of cannot ride in a bundle
        plan_bundle(snap(as_of=AS_OF - timedelta(seconds=5)), as_of=AS_OF)


def test_feature_disabled_keeps_bundle_prompts_and_report_unchanged(fixture_cfg, tmp_path):
    from aqe.cli.analyze import run_analyze, run_report
    from aqe.storage.runs import RunRecord
    from tests.test_debate_layer import make_bundle

    assert fixture_cfg.planning.enabled is False  # default off
    p = build_prompts(make_bundle(), [AnalystRole.CHART, AnalystRole.FLOW], [3600, 86400], [])
    assert "PLAN_CANDIDATES" not in json.dumps(p) and "plan_selection" not in json.dumps(p)
    rec = RunRecord("analyze", "fixture", "h", tmp_path / "runs")
    run_analyze(fixture_cfg, "h", rec)
    off = json.loads(Path(rec.outputs["report_json"]).read_text(encoding="utf-8"))
    assert "trade_plans" not in off and "4b." not in Path(rec.outputs["report_md"]).read_text(encoding="utf-8")
    on_cfg = fixture_cfg.model_copy(deep=True)
    on_cfg.planning = PlanningConfig(enabled=True)
    rec2 = RunRecord("analyze", "fixture", "h", tmp_path / "runs")
    run_analyze(on_cfg, "h", rec2)
    on = json.loads(Path(rec2.outputs["report_json"]).read_text(encoding="utf-8"))
    assert [h["risk"]["disposition"] for h in on["horizons"]] == [h["risk"]["disposition"] for h in off["horizons"]] == ["ABSTAIN", "ABSTAIN"]
    assert [h["ensemble"] for h in on["horizons"]][0]["status"] == "ABSTAIN"  # numeric path unchanged
    assert {p["decision"] for p in on["trade_plans"]} == {"WAIT"} and all(p["execution_enabled"] is False for p in on["trade_plans"])
    assert on["planning"]["selection_source"] == "UNAVAILABLE" and "SYNTHETIC_FIXTURE" in on["planning"]["input"]
    rec3 = RunRecord("report", "fixture", "h", tmp_path / "runs")
    assert run_report(on_cfg, rec3, on["analysis_id"]) == 0  # stored JSON re-renders with the plans
    assert "4b. 진입·손절·익절 검토안" in Path(rec3.outputs["report_md"]).read_text(encoding="utf-8")


# ------------------------------------------------------------------ 8. order-book / trade-flow evidence (collector schema)
def _flow_rows(start, n, **over):
    rows = []
    for i in range(n):
        s = pd.Timestamp(start) + pd.Timedelta(minutes=i)
        r = {"instrument_id": "BTCUSDT", "interval_start": s, "interval_end": s + pd.Timedelta(minutes=1), "book_state": "SYNCED",
             "book_valid": True, "spread_bps": 0.5, "depth_bid_10bps_quote": 100.0, "depth_ask_10bps_quote": 50.0,
             "depth_bid_25bps_quote": 300.0, "depth_ask_25bps_quote": 200.0, "imbalance_top10": 0.2, "n_trades": 5,
             "buy_base": 2.0, "sell_base": 1.0, "cvd_delta_base": 1.0, "agg_missing": 0,
             "available_at": s + pd.Timedelta(minutes=1, milliseconds=50)}
        r.update({k: (v(i) if callable(v) else v) for k, v in over.items()})
        rows.append(r)
    return rows


def test_flow_evidence_uses_collector_columns_and_separates_book_and_trade_validity(fixture_cfg):
    cfg = fixture_cfg
    d = cfg.paths.data_root / "normalized" / "flow_summaries" / "binance" / "usdm_perpetual" / "BTCUSDT" / "2026-01-10"
    d.mkdir(parents=True)
    rows = _flow_rows(T0, 10,
                      book_valid=lambda i: i not in (3, 4),  # invalid book rows carry junk that must not be averaged
                      depth_bid_10bps_quote=lambda i: 9e9 if i in (3, 4) else 100.0, spread_bps=lambda i: 999.0 if i in (3, 4) else 0.5,
                      agg_missing=lambda i: 7 if i == 6 else 0, buy_base=lambda i: 1000.0 if i == 6 else 2.0)
    pd.DataFrame(rows).to_parquet(d / "00.parquet")
    as_of = T0 + timedelta(minutes=10, seconds=10)
    item, note = flow_summary_item(cfg, INST, as_of, "t")
    ex = item.excerpt
    assert "depth_bid_10bps_quote mean 100 / last 100" in ex and "depth_ask_10bps_quote mean 50" in ex  # collector names
    assert "spread_bps mean 0.5" in ex and "order book valid in 8" in ex
    assert "buy 18.000 / sell 9.000" in ex and "without aggTrade gaps 9" in ex  # the gap interval is excluded from the sums
    assert "do not show how resting orders changed" in ex and "who placed orders" in ex
    assert item.available_at <= as_of
    stale, why = flow_summary_item(cfg, INST, T0 + timedelta(minutes=30), "t")
    assert stale is None and why.startswith("STALE")
    future, _ = flow_summary_item(cfg, INST, T0 + timedelta(minutes=5, seconds=30), "t")
    assert "1-minute intervals 2026-01-10T00:00:00Z..2026-01-10T00:05:00Z" in future.excerpt  # rows after as_of not used
    pd.DataFrame(_flow_rows(T0, 10, book_valid=False)).to_parquet(d / "00.parquet")
    nobook, _ = flow_summary_item(cfg, INST, as_of, "t")
    assert "no valid rows in the window" in nobook.excerpt and "depth_bid" not in nobook.excerpt


_ = (WORKER_PY,)
