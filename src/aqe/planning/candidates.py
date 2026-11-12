"""Trade-plan candidate calculator v0 (`swing_atr_v0`): pure, deterministic, point-in-time (docs/TRADE_PLAN.md).

Input: base bars (5m). Only closed bars with available_at <= as_of are used (`bars_as_of`); bars of another
instrument are refused. Per horizon profile (config `planning.profiles`):
  1. completed bars of the profile timeframe — a higher timeframe is built only from COMPLETE groups of closed 5m bars
     (a partial hour is never a high/low), on a full time grid so data gaps break pivots and ATR;
  2. ATR = simple mean true range of the last `atr_period` completed bars;
  3. swing high/low = a bar whose high (low) is beyond the `pivot_width` bars on each side; usable only from
     `confirmed_at` = availability of its last right-side bar (never earlier than the data that confirms it);
  4. LONG: nearest intact swing low at/below the reference close (no completed close below it since confirmation);
     zone [low, low + entry_buffer x ATR], stop low - stop_buffer x ATR, targets = the next INTACT swing highs above the
     zone (TP1, TP2; a high already exceeded by a later completed close is not a target). SHORT mirrored. No level / too far / no observed target -> no candidate, with the reason —
     prices are never filled in;
  5. if a tick size was available at as_of, prices are rounded away from the favourable side and the relations are
     re-checked; net reward / loss / RR use the conservative zone end and the existing cost function
     (`evaluation.costs.trade_return`: taker fee + adverse slippage on both legs; funding NOT included).
Output is a rule-based, unvalidated technical candidate set — not a forecast and not a validated profit target.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

import numpy as np
import pandas as pd
from pydantic import ValidationError

from aqe.config import PlanningConfig, PlanningProfile
from aqe.contracts.core import InstrumentKey, iso_utc
from aqe.contracts.plan import (
    CheckOutcome,
    ConditionStatus,
    CostAssumptions,
    EntryCondition,
    HorizonCandidates,
    InstrumentSpec,
    PlanCandidate,
    PlanCandidateSet,
    PlanCheck,
    PlanSide,
    PriceLevel,
    TakeProfit,
    content_id,
    price_problems,
)
from aqe.storage.bars import bars_as_of

ALGORITHM_VERSION = "swing_atr_v0"
BASE_INTERVAL_SECONDS = 300
LIVE_LABEL = "REST 5m bars (5m_live not merged: G01)"
_Q = Decimal("1e-8")


def planning_config_hash(pcfg: PlanningConfig) -> str:
    d = pcfg.model_dump(mode="json", exclude={"enabled", "instrument_specs_dir"})
    return hashlib.sha256(json.dumps({"algorithm": ALGORITHM_VERSION, **d}, sort_keys=True, default=str).encode()).hexdigest()


def dec(x) -> Decimal:
    """Finite Decimal from a Decimal/float/int (NaN and infinities are refused, never coerced)."""
    if isinstance(x, Decimal):
        if not x.is_finite():
            raise ValueError(f"non-finite value {x}")
        return x
    f = float(x)
    if not math.isfinite(f):
        raise ValueError(f"non-finite value {x!r}")
    return Decimal(repr(f))


def round_price(p: Decimal, tick: Decimal, up: bool) -> Decimal:
    return (p / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * tick


def _utc(ts) -> datetime:
    return pd.Timestamp(ts).to_pydatetime().astimezone(timezone.utc)


def _ms(ts) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def cost_assumptions(taker_fee_bps, slippage_bps, source: str) -> CostAssumptions | None:
    if taker_fee_bps is None or slippage_bps is None:
        return None
    return CostAssumptions(taker_fee_bps=dec(taker_fee_bps), slippage_bps=dec(slippage_bps), source=source)


def load_instrument_spec(data_root: Path, rel_dir: str, inst: InstrumentKey, as_of: datetime) -> tuple[InstrumentSpec | None, str]:
    """Latest tick-size revision available at as_of from `{data_root}/{rel_dir}/{slug}.json` (docs/IMPORT_FORMATS.md).
    Missing/unreadable/not-yet-available -> (None, reason): the tick is never assumed."""
    p = Path(data_root) / rel_dir / f"{inst.slug()}.json"
    if not p.exists():
        return None, f"no instrument spec import ({rel_dir}/{inst.slug()}.json): tick size unknown, prices not rounded"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if d.get("instrument_id") != inst.instrument_id:
            return None, f"instrument spec {p.name} is for {d.get('instrument_id')}: tick size unknown"
        revs = [InstrumentSpec(instrument=inst, tick_size=Decimal(str(r["tick_size"])), source_id=str(r["source_id"]),
                               available_at=r["available_at"]) for r in d["revisions"]]
    except (OSError, ValueError, KeyError, TypeError, ValidationError, ArithmeticError) as exc:
        return None, f"instrument spec unreadable ({type(exc).__name__}): tick size unknown"
    usable = [r for r in revs if r.available_at <= as_of]
    if not usable:
        return None, "no instrument spec revision available at as_of: tick size unknown"
    best = max(usable, key=lambda r: r.available_at)
    return best, f"tick size {best.tick_size} from {best.source_id} (available {iso_utc(best.available_at)})"


def usable_bars(bars: pd.DataFrame | None, inst: InstrumentKey, as_of: datetime) -> pd.DataFrame:
    """Closed base bars available at as_of. Rows of another instrument are an error, not something to filter."""
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["open_time", "close_time", "open", "high", "low", "close", "available_at"])
    for col, want in (("venue", inst.venue.value), ("market_type", inst.market_type.value), ("instrument_id", inst.instrument_id)):
        if col in bars.columns and (bars[col].astype(str) != want).any():
            raise ValueError(f"bars contain another instrument ({col} != {want}); candidates are computed per instrument")
    if "interval_seconds" in bars.columns and (bars["interval_seconds"].astype(int) != BASE_INTERVAL_SECONDS).any():
        raise ValueError(f"base bars must be {BASE_INTERVAL_SECONDS}s")
    return bars_as_of(bars, as_of)


def timeframe_bars(base: pd.DataFrame, tf: int) -> pd.DataFrame:
    """Completed `tf`-second bars on a full grid (index = open time; gaps are NaN rows)."""
    b = base.set_index("open_time").sort_index()
    cols = ["open", "high", "low", "close", "available_at", "close_time"]
    if tf == BASE_INTERVAL_SECONDS:
        out = b[cols].copy()
    else:
        g = b.groupby(b.index.floor(f"{tf}s"))
        out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
                            "available_at": g["available_at"].max(), "close_time": g["close_time"].max(), "n": g.size()})
        out = out[(out["n"] == tf // BASE_INTERVAL_SECONDS) & (out["close_time"] == out.index + pd.Timedelta(seconds=tf))][cols]
    if out.empty:
        return out
    grid = pd.date_range(out.index[0], out.index[-1], freq=pd.Timedelta(seconds=tf), tz="UTC")
    return out.reindex(grid)


def _atr(win: pd.DataFrame, period: int) -> Decimal | None:
    tail = win.iloc[-(period + 1):]
    if len(tail) < period + 1 or tail[["high", "low", "close"]].isna().any().any():
        return None
    prev = tail["close"].shift(1)
    tr = pd.concat([tail["high"] - tail["low"], (tail["high"] - prev).abs(), (tail["low"] - prev).abs()], axis=1).max(axis=1)
    return dec(float(tr.iloc[1:].mean())).quantize(_Q)


@dataclass(frozen=True)
class _Pivot:
    kind: str  # SWING_LOW | SWING_HIGH
    price: Decimal
    fprice: float
    idx: int
    pivot_time: datetime
    confirmed_at: datetime


def _pivots(win: pd.DataFrame, w: int) -> list[_Pivot]:
    hi, lo, av = win["high"].to_numpy(float), win["low"].to_numpy(float), win["available_at"]
    out = []
    for i in range(w, len(win) - w):
        if np.isnan(hi[i - w:i + w + 1]).any() or np.isnan(lo[i - w:i + w + 1]).any():
            continue  # a gap inside the window: no pivot (never compare across missing bars)
        conf = _utc(av.iloc[i:i + w + 1].max())
        t = _utc(win.index[i])
        if hi[i] > hi[i - w:i].max() and hi[i] >= hi[i + 1:i + w + 1].max():
            out.append(_Pivot("SWING_HIGH", dec(hi[i]), float(hi[i]), i, t, conf))
        if lo[i] < lo[i - w:i].min() and lo[i] <= lo[i + 1:i + w + 1].min():
            out.append(_Pivot("SWING_LOW", dec(lo[i]), float(lo[i]), i, t, conf))
    return out


def net_outcomes(side: PlanSide, entry: Decimal, stop: Decimal, tps: list[Decimal], costs: CostAssumptions,
                 as_of: datetime, horizon_seconds: int) -> tuple[float, list[float]]:
    """(net loss at the stop as a positive fraction, net reward per target) via the existing cost function."""
    from aqe.evaluation.costs import trade_return

    s = 1 if side == PlanSide.LONG else -1
    t0 = pd.Timestamp(as_of)
    t1 = t0 + pd.Timedelta(seconds=horizon_seconds)
    fee, slip = float(costs.taker_fee_bps), float(costs.slippage_bps)
    loss = -trade_return(s, float(entry), float(stop), t0, t1, fee, slip, None).net_return
    rewards = [trade_return(s, float(entry), float(t), t0, t1, fee, slip, None).net_return for t in tps]
    return loss, rewards


def _level(p: _Pivot, tf: int) -> PriceLevel:
    return PriceLevel(kind=p.kind, price=p.price, pivot_time=p.pivot_time, confirmed_at=p.confirmed_at, timeframe_seconds=tf)


def _candidate(side: PlanSide, h: int, prof: PlanningProfile, win: pd.DataFrame, piv: list[_Pivot], atr: Decimal, ref: Decimal,
               ref_time: datetime, age: float, as_of: datetime, inst: InstrumentKey, pcfg: PlanningConfig,
               costs: CostAssumptions | None, spec: InstrumentSpec | None, spec_note: str, src_ids: tuple[str, ...],
               available_at: datetime) -> tuple[PlanCandidate | None, str]:
    long = side == PlanSide.LONG
    tf, w = prof.timeframe_seconds, prof.pivot_width
    closes = win["close"].to_numpy(float)
    name = "support (swing low)" if long else "resistance (swing high)"

    def intact(p: _Pivot) -> bool:  # no completed close beyond the level after its confirmation (support/resistance holds)
        after = closes[p.idx + w + 1:]
        after = after[~np.isnan(after)]
        return not len(after) or (after.min() >= p.fprice if p.kind == "SWING_LOW" else after.max() <= p.fprice)

    near = [p for p in piv if p.kind == ("SWING_LOW" if long else "SWING_HIGH") and (p.price <= ref if long else p.price >= ref) and intact(p)]
    if not near:
        return None, (f"{side.value}: no intact confirmed {name} {'at/below' if long else 'at/above'} the reference close {ref} "
                      f"in the last {len(win)} completed {tf}s bars")
    lv = max(near, key=lambda p: (p.price, p.confirmed_at)) if long else min(near, key=lambda p: (p.price, -p.confirmed_at.timestamp()))
    dist = (ref - lv.price) if long else (lv.price - ref)
    if dist > dec(pcfg.max_entry_distance_atr) * atr:
        return None, (f"{side.value}: nearest {name} {lv.price} is {float(dist / atr):.2f} ATR from the reference close {ref} "
                      f"(> {pcfg.max_entry_distance_atr} ATR)")
    eb, sb = (dec(pcfg.entry_buffer_atr) * atr).quantize(_Q), (dec(pcfg.stop_buffer_atr) * atr).quantize(_Q)
    lo, hi, stop = (lv.price, lv.price + eb, lv.price - sb) if long else (lv.price - eb, lv.price, lv.price + sb)
    targets: dict[Decimal, _Pivot] = {}
    for p in sorted(piv, key=lambda p: p.confirmed_at):
        if p.kind == ("SWING_HIGH" if long else "SWING_LOW") and (p.price > hi if long else p.price < lo) and intact(p):
            targets.setdefault(p.price, p)
    order = sorted(targets, reverse=not long)[:2]
    if not order:
        return None, (f"{side.value}: no intact confirmed swing {'high above' if long else 'low below'} the entry zone in the "
                      "lookback (no observed target)")
    tp_levels = [targets[x] for x in order]
    tps = [p.price for p in tp_levels]
    if spec is not None:  # away from the favourable side: wider zone, farther stop, closer targets
        t = spec.tick_size
        lo, hi = round_price(lo, t, up=False), round_price(hi, t, up=True)
        stop = round_price(stop, t, up=not long)
        tps = [round_price(x, t, up=not long) for x in tps]
    else:
        lo, hi, stop, tps = lo.quantize(_Q), hi.quantize(_Q), stop.quantize(_Q), [x.quantize(_Q) for x in tps]
    probs = price_problems(side, lo, hi, stop, tps)
    if probs:
        return None, f"{side.value}: price relation broken{' after tick rounding' if spec else ''}: {'; '.join(probs)}"
    entry = hi if long else lo
    sign = "+" if long else "-"
    checks = [PlanCheck(name="input_fresh", outcome=CheckOutcome.PASS,
                        detail=f"last completed bar closed {age:.0f}s before as_of (<= {pcfg.max_input_age_seconds}s)"),
              PlanCheck(name="tick_size", outcome=CheckOutcome.PASS if spec else CheckOutcome.UNKNOWN,
                        detail=(f"prices rounded to tick {spec.tick_size} away from the favourable side ({spec_note})" if spec
                                else f"{spec_note} — cannot be verified against the exchange tick"))]
    tp_objs, net_loss = [], None
    tp_basis = [f"observed swing {'high' if long else 'low'} {p.price} (pivot bar {iso_utc(p.pivot_time)}, confirmed "
                f"{iso_utc(p.confirmed_at)})" for p in tp_levels]
    if costs is None:
        checks.append(PlanCheck(name="costs", outcome=CheckOutcome.UNKNOWN, detail="fee/slippage assumptions missing: net reward, loss and RR not computed"))
        tp_objs = [TakeProfit(label=f"TP{k + 1}", price=x, basis=tp_basis[k]) for k, x in enumerate(tps)]
        checks.append(PlanCheck(name="net_rr_tp1", outcome=CheckOutcome.UNKNOWN, detail="not computed (costs unknown)"))
    else:
        loss, rewards = net_outcomes(side, entry, stop, tps, costs, as_of, h)
        net_loss = round(loss, 8)
        checks.append(PlanCheck(name="costs", outcome=CheckOutcome.PASS,
                                detail=f"round trip: taker fee {costs.taker_fee_bps} bps + adverse slippage {costs.slippage_bps} bps on "
                                       f"entry and exit ({costs.source}); {costs.note}"))
        for k, (x, r) in enumerate(zip(tps, rewards)):
            rr = round(r / loss, 4) if loss > 0 else None
            tp_objs.append(TakeProfit(label=f"TP{k + 1}", price=x, basis=tp_basis[k], net_reward_fraction=round(r, 8), net_rr=rr))
            checks.append(PlanCheck(name=f"net_reward_tp{k + 1}", outcome=CheckOutcome.PASS if r > 0 else CheckOutcome.FAIL,
                                    detail=f"net reward {r * 100:+.4f}% from the conservative entry {entry} after costs"
                                           + ("" if r > 0 else " — not positive: rejected")))
        rr1, ok = tp_objs[0].net_rr, loss > 0 and rewards[0] > 0
        checks.append(PlanCheck(name="net_rr_tp1", outcome=CheckOutcome.PASS if ok and rr1 >= pcfg.min_net_rr else CheckOutcome.FAIL,
                                detail=f"net RR(TP1) {rr1} vs development minimum {pcfg.min_net_rr} "
                                       "(reward/loss geometry after costs — not a probability or expected return)"))
    valid_until = as_of + timedelta(seconds=min(h, pcfg.max_valid_seconds))
    in_zone = lo <= ref <= hi
    conds = (EntryCondition(condition_id="close_in_zone", text=f"the latest completed 5m bar closed inside the entry zone [{lo}, {hi}]",
                            status=ConditionStatus.MET if in_zone else ConditionStatus.NOT_MET, observed=f"close {ref} at {iso_utc(ref_time)}"),
             EntryCondition(condition_id="level_intact", text=f"no completed {tf}s bar closed {'below' if long else 'above'} the {name} "
                                                              f"{lv.price} after its confirmation",
                            status=ConditionStatus.MET, observed=f"level confirmed at {iso_utc(lv.confirmed_at)}"))
    fields = dict(
        instrument=inst, as_of=as_of, horizon_seconds=h, side=side, timeframe_seconds=tf, entry_low=lo, entry_high=hi,
        conservative_entry=entry,
        entry_basis=(f"{name} {lv.price} (pivot bar {iso_utc(lv.pivot_time)}, confirmed {iso_utc(lv.confirmed_at)}) {sign} "
                     f"{pcfg.entry_buffer_atr}xATR({prof.atr_period}, {tf}s) {atr}"),
        entry_conditions=conds, stop=stop,
        stop_basis=f"{name} {lv.price} {'-' if long else '+'} {pcfg.stop_buffer_atr}xATR({prof.atr_period}, {tf}s) {atr}",
        take_profits=tuple(tp_objs), net_loss_fraction=net_loss,
        levels=(_level(lv, tf), *(_level(p, tf) for p in tp_levels)), checks=tuple(checks),
        invalidation=(f"a completed {tf}s bar closes {'below' if long else 'above'} the stop {stop}",
                      f"the entry condition is not met before valid_until {iso_utc(valid_until)}"),
        source_evidence_ids=tuple(src_ids), available_at=available_at, valid_until=valid_until)
    draft = PlanCandidate.model_construct(candidate_id="", **fields)
    return PlanCandidate(candidate_id=content_id("pc-", draft, "candidate_id"), **fields), ""


def _horizon(h: int, base: pd.DataFrame, ref: Decimal, ref_time: datetime, ref_avail: datetime, as_of: datetime,
             inst: InstrumentKey, pcfg: PlanningConfig, costs, spec, spec_note, src_ids) -> HorizonCandidates:
    prof = pcfg.profiles.get(h)
    if prof is None:
        return HorizonCandidates(horizon_seconds=h, no_candidate_reasons=(f"no planning profile for horizon {h}s",))
    age = (as_of - ref_time).total_seconds()
    head = dict(horizon_seconds=h, timeframe_seconds=prof.timeframe_seconds, reference_price=ref, reference_time=ref_time)
    if age > pcfg.max_input_age_seconds:
        return HorizonCandidates(**head, no_candidate_reasons=(
            f"STALE input: last completed bar closed {iso_utc(ref_time)}, {age:.0f}s before as_of (> {pcfg.max_input_age_seconds}s)",))
    tfb = timeframe_bars(base, prof.timeframe_seconds)
    win = tfb.iloc[-prof.lookback_bars:] if len(tfb) else tfb
    atr = _atr(win, prof.atr_period) if len(win) else None
    if atr is None or atr <= 0:
        return HorizonCandidates(**head, no_candidate_reasons=(
            f"insufficient contiguous completed {prof.timeframe_seconds}s bars for ATR({prof.atr_period})",))
    piv = _pivots(win, prof.pivot_width)
    avail = max(_utc(win["available_at"].dropna().max()), ref_avail)
    cands, reasons = [], []
    for side in (PlanSide.LONG, PlanSide.SHORT):
        c, why = _candidate(side, h, prof, win, piv, atr, ref, ref_time, age, as_of, inst, pcfg, costs, spec, spec_note, src_ids, avail)
        if c is not None:
            cands.append(c)
        else:
            reasons.append(why)
    return HorizonCandidates(**head, atr=atr, candidates=tuple(cands), no_candidate_reasons=tuple(reasons))


def _hash_bars(base: pd.DataFrame) -> str:
    h = hashlib.sha256()
    for r in base[["open_time", "open", "high", "low", "close", "available_at"]].itertuples(index=False):
        h.update(f"{_ms(r.open_time)},{float(r.open)!r},{float(r.high)!r},{float(r.low)!r},{float(r.close)!r},{_ms(r.available_at)}\n".encode())
    return h.hexdigest()


def compute_candidates(inst: InstrumentKey, as_of: datetime, bars: pd.DataFrame | None, horizons: list[int], pcfg: PlanningConfig,
                       costs: CostAssumptions | None = None, spec: InstrumentSpec | None = None, spec_note: str = "",
                       source_evidence_ids: tuple[str, ...] = (), input_label: str = "REST 5m bars") -> PlanCandidateSet:
    """Pure: the candidate snapshot at `as_of` (same inputs + config -> identical snapshot and ids)."""
    as_of = _utc(as_of)
    if spec is not None and (spec.available_at > as_of or spec.instrument != inst):
        spec, spec_note = None, "instrument spec not available at as_of: tick size unknown"
    if spec is None and not spec_note:
        spec_note = "tick size unknown"
    base = usable_bars(bars, inst, as_of)
    span = max((p.timeframe_seconds * (p.lookback_bars + p.atr_period + 2) for p in pcfg.profiles.values()), default=0)
    if len(base):
        base = base[base["open_time"] >= pd.Timestamp(as_of) - pd.Timedelta(seconds=span)].reset_index(drop=True)
    if base.empty:
        blocks = tuple(HorizonCandidates(horizon_seconds=h, no_candidate_reasons=("no completed bars available at as_of",)) for h in horizons)
        last_close = last_avail = None
        desc = f"{input_label}: no completed bar available at as_of"
    else:
        last = base.iloc[-1]
        ref, last_close, last_avail = dec(last["close"]), _utc(last["close_time"]), _utc(last["available_at"])
        blocks = tuple(_horizon(h, base, ref, last_close, last_avail, as_of, inst, pcfg, costs, spec, spec_note, tuple(source_evidence_ids))
                       for h in horizons)
        desc = (f"{input_label}: {len(base)} completed bars (closed, available_at <= as_of) up to {iso_utc(last_close)}; "
                "order-book/trade-flow data is not used for prices in v0")
    fields = dict(algorithm_version=ALGORITHM_VERSION, config_hash=planning_config_hash(pcfg), input_hash=_hash_bars(base), instrument=inst,
                  as_of=as_of, input_description=desc, last_bar_close=last_close, last_bar_available_at=last_avail, costs=costs,
                  instrument_spec=spec, spec_note=spec_note, horizons=blocks)
    draft = PlanCandidateSet.model_construct(snapshot_id="", **fields)
    return PlanCandidateSet(snapshot_id=content_id("ps-", draft, "snapshot_id"), **fields)


# ---------------------------------------------------------------- text for the debate prompts (complete, never truncated)
def prompt_text(s: PlanCandidateSet) -> str:
    costs = (f"taker fee {s.costs.taker_fee_bps} bps + slippage {s.costs.slippage_bps} bps per side ({s.costs.source}); {s.costs.note}"
             if s.costs else "UNKNOWN (net reward/RR not computed)")
    lines = [f"PLAN_CANDIDATES snapshot {s.snapshot_id} (algorithm {s.algorithm_version}, config {s.config_hash[:12]}, as_of "
             f"{iso_utc(s.as_of)}). Computed by code from the SAME completed bars as the price evidence: shared context, NOT "
             "independent evidence. Rule-based, unvalidated levels. RR = net reward / net loss after costs from the conservative "
             "zone end: a price-geometry ratio, not a probability or an expected return.",
             f"Costs: {costs}. Tick: {s.spec_note}. Input: {s.input_description}."]
    for hz in s.horizons:
        lines.append(f"HORIZON {hz.horizon_seconds}s: timeframe {hz.timeframe_seconds}s, ATR {hz.atr}, reference close "
                     f"{hz.reference_price} at {iso_utc(hz.reference_time) if hz.reference_time else 'n/a'}")
        for c in hz.candidates:
            tps = "; ".join(f"{t.label} {t.price} [{t.basis}] net RR {t.net_rr if t.net_rr is not None else 'n/a'}" for t in c.take_profits)
            conds = "; ".join(f"{e.condition_id}={e.status.value} ({e.text}; observed {e.observed})" for e in c.entry_conditions)
            checks = "; ".join(f"{k.name}={k.outcome.value}" for k in c.checks)
            lines.append(f"  [plan:{c.candidate_id}] horizon {c.horizon_seconds}s {c.side.value} selectable={'yes' if c.selectable else 'no'}: "
                         f"entry zone {c.entry_low}-{c.entry_high} (conservative entry {c.conservative_entry}; {c.entry_basis}); "
                         f"stop {c.stop} ({c.stop_basis}); {tps}; entry conditions: {conds}; checks: {checks}; "
                         f"invalidation: {' | '.join(c.invalidation)}; valid_until {iso_utc(c.valid_until)}")
        for r in hz.no_candidate_reasons:
            lines.append(f"  no candidate: {r}")
    return "\n".join(lines)


_NUM = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    return [float(m.group(0).replace(",", "")) for m in _NUM.finditer(text or "")]


def snapshot_prices(s: PlanCandidateSet) -> list[float]:
    out = []
    for hz in s.horizons:
        out += [float(x) for x in (hz.reference_price, hz.atr) if x is not None]
        for c in hz.candidates:
            out += [float(c.entry_low), float(c.entry_high), float(c.stop), *(float(t.price) for t in c.take_profits),
                    *(float(lv.price) for lv in c.levels)]
    return out


def candidates_for_config(cfg, inst: InstrumentKey, as_of: datetime, bars: pd.DataFrame | None = None,
                          source_evidence_ids: tuple[str, ...] = (), input_label: str = LIVE_LABEL) -> PlanCandidateSet:
    """Snapshot with the configured profiles, the experiment's horizons and cost assumptions (the same fee/slippage the
    risk engine and the locked evaluation use) and the tick size imported for as_of. `bars=None` reads the REST 5m
    partition (G01 — merging `5m_live` — is not part of this)."""
    from aqe.config import load_experiment_config
    from aqe.storage.bars import read_bars

    pcfg: PlanningConfig = cfg.planning
    try:
        exp, _ = load_experiment_config(cfg.analysis.experiment_config)
        horizons = list(exp.horizons_seconds)
        costs = cost_assumptions(exp.evaluation.taker_fee_bps, exp.evaluation.slippage_bps,
                                 f"experiment {exp.experiment_id} evaluation costs")
    except (OSError, ValueError) as exc:
        horizons, costs = sorted(pcfg.profiles), None
        input_label += f" (experiment config unavailable: {type(exc).__name__}; costs unknown)"
    if bars is None:
        span = max((p.timeframe_seconds * (p.lookback_bars + p.atr_period + 2) for p in pcfg.profiles.values()), default=0)
        bars = read_bars(cfg.paths.data_root, inst, "5m", start=as_of - timedelta(seconds=span + 86400), end=as_of + timedelta(minutes=5))
    spec, note = load_instrument_spec(cfg.paths.data_root, pcfg.instrument_specs_dir, inst, as_of)
    return compute_candidates(inst, as_of, bars, horizons, pcfg, costs, spec, note, source_evidence_ids, input_label)
