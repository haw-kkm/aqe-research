"""Point-in-time EvidenceBundle builder (B08.2). Every item satisfies available_at <= as_of.

Sources wired now:
  MARKET_FLOW   our own exchange data: closed bars, settled funding, OI statistics (last ~30 days),
                collector flow summaries / estimated funding when the collector was running.
  MACRO_CALENDAR / EARNINGS_CALENDAR  verified file import ({data_root}/imports/calendar/*.csv)
  NEWS          verified file import ({data_root}/imports/news/*.jsonl)
Not configured (explicit NOT_CONFIGURED health, never "neutral"): SOCIAL, CROSS_ASSET, ONCHAIN,
CRYPTO_FUNDAMENTAL. No X scraping and no paid provider is connected.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from aqe.config import LocalConfig
from aqe.contracts.context import (
    EvidenceBundle,
    EvidenceItem,
    ScheduledEvent,
    SourceHealth,
    SourceHealthStatus,
    SourceKind,
    VerificationStatus,
    compute_bundle_hash,
)
from aqe.contracts.core import InstrumentKey, iso_utc, to_ms, utc_now
from aqe.storage.bars import bars_as_of, read_bars


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _item(eid: str, kind: SourceKind, source_id: str, title: str, excerpt: str, available_at, event_time=None,
          published_at=None, received_at=None, url=None, status=VerificationStatus.VERIFIED, scope=(), max_len: int = 1500) -> EvidenceItem:
    return EvidenceItem(evidence_id=eid, source_kind=kind, source_id=source_id, source_url=url, title=title, excerpt=excerpt[:max_len],
                        content_hash=_h(excerpt), event_time=event_time, published_at=published_at,
                        received_at=received_at or available_at, available_at=available_at, verification_status=status,
                        instrument_scope=tuple(scope))


def market_evidence(cfg: LocalConfig, inst: InstrumentKey, as_of: datetime) -> tuple[list[EvidenceItem], SourceHealth]:
    items: list[EvidenceItem] = []
    tag = f"{inst.instrument_id}-{to_ms(as_of)}"
    bars = read_bars(cfg.paths.data_root, inst, "5m", start=as_of - timedelta(days=8), end=as_of + timedelta(minutes=5))
    avail = bars_as_of(bars, as_of)
    if avail.empty:
        return items, SourceHealth(source_kind=SourceKind.MARKET_FLOW, status=SourceHealthStatus.MISSING, detail="no bars at as_of")
    c = avail.set_index("close_time")["close"]
    last_close_t = avail["close_time"].iloc[-1].to_pydatetime()
    wm = avail["available_at"].iloc[-1].to_pydatetime()

    def ret(hours: int) -> str:
        t = c.index[-1] - pd.Timedelta(hours=hours)
        prev = c[c.index <= t]
        return f"{(c.iloc[-1] / prev.iloc[-1] - 1) * 100:+.2f}%" if len(prev) else "n/a"

    lr = np.log(c).diff().tail(288)
    excerpt = (f"{inst.instrument_id} USDⓈ-M perpetual, completed 5m bars up to {iso_utc(last_close_t)}: last close {c.iloc[-1]:.2f}; "
               f"returns 1h {ret(1)}, 4h {ret(4)}, 24h {ret(24)}, 7d {ret(168)}; realized 5m vol (24h) {lr.std() * 100:.3f}% per bar; "
               f"24h high {avail.tail(288)['high'].max():.2f}, low {avail.tail(288)['low'].min():.2f}; "
               f"24h taker-buy share {avail.tail(288)['taker_buy_base'].sum() / max(avail.tail(288)['volume'].sum(), 1e-9):.3f}; "
               f"provenance {avail['provenance'].iloc[-1]}.")
    items.append(_item(f"mkt-bars-{tag}", SourceKind.MARKET_FLOW, "binance.fapi.klines", "Price/volume summary (completed 5m bars)",
                       excerpt, wm, event_time=last_close_t, scope=[inst.instrument_id]))
    # daily OHLCV table (UTC days) aggregated from 5m bars available at as_of; last row may be a partial day
    a = avail.set_index("open_time")
    daily = a.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().tail(25)
    nl = "\n"
    csv = "Date,Open,High,Low,Close,Volume" + nl + nl.join(
        f"{d.strftime('%Y-%m-%d')},{r.open:.2f},{r.high:.2f},{r.low:.2f},{r.close:.2f},{r.volume:.1f}" for d, r in daily.iterrows())
    items.append(_item(f"mkt-daily-{tag}", SourceKind.MARKET_FLOW, "aqe.daily_from_5m_bars",
                       "Daily OHLCV (UTC days, from completed 5m bars; last day partial up to as_of)",
                       f"Daily OHLCV for {inst.instrument_id}; last row partial through {iso_utc(last_close_t)}" + nl + csv, wm,
                       event_time=last_close_t, scope=[inst.instrument_id], max_len=4000))
    base = cfg.paths.data_root / "normalized"
    fpath = base / "funding" / inst.venue.value / inst.market_type.value / inst.instrument_id / "funding_history.parquet"
    if fpath.exists():
        f = pd.read_parquet(fpath)
        f = f[f["available_at"] <= pd.Timestamp(as_of)]
        if len(f):
            last = f.iloc[-1]
            avg7 = f[f["funding_time"] > f["funding_time"].iloc[-1] - pd.Timedelta(days=7)]["funding_rate"].mean()
            ex = (f"Settled funding (8h) last {last['funding_rate'] * 1e4:+.2f} bps at {iso_utc(last['funding_time'].to_pydatetime())}; "
                  f"7d mean {avg7 * 1e4:+.2f} bps per interval. Positive funding: longs pay shorts.")
            items.append(_item(f"mkt-funding-{tag}", SourceKind.MARKET_FLOW, "binance.fapi.fundingRate", "Settled funding rate", ex,
                               last["available_at"].to_pydatetime(), event_time=last["funding_time"].to_pydatetime(), scope=[inst.instrument_id]))
    opath = base / "oi_hist" / inst.venue.value / inst.market_type.value / inst.instrument_id / "oi_hist_5m.parquet"
    if opath.exists():
        o = pd.read_parquet(opath)
        o = o[o["available_at"] <= pd.Timestamp(as_of)]
        if len(o) and (pd.Timestamp(as_of) - o["timestamp"].iloc[-1]) < pd.Timedelta(minutes=30):
            prev = o[o["timestamp"] <= o["timestamp"].iloc[-1] - pd.Timedelta(hours=24)]
            chg = f"{(o['sum_open_interest_base'].iloc[-1] / prev['sum_open_interest_base'].iloc[-1] - 1) * 100:+.2f}%" if len(prev) else "n/a"
            ex = (f"Open interest (5m statistic) {o['sum_open_interest_base'].iloc[-1]:.1f} BTC "
                  f"({o['sum_open_interest_value_quote'].iloc[-1] / 1e9:.3f}B USDT) at {iso_utc(o['timestamp'].iloc[-1].to_pydatetime())}; 24h change {chg}. "
                  "OI is aggregate; it does not identify who holds positions.")
            items.append(_item(f"mkt-oi-{tag}", SourceKind.MARKET_FLOW, "binance.futures.openInterestHist", "Open interest", ex,
                               o["available_at"].iloc[-1].to_pydatetime(), event_time=o["timestamp"].iloc[-1].to_pydatetime(), scope=[inst.instrument_id]))
    fsdir = base / "flow_summaries" / inst.venue.value / inst.market_type.value / inst.instrument_id
    if fsdir.exists():
        files = sorted(fsdir.rglob("*.parquet"))
        if files:
            fs = pd.concat([pd.read_parquet(p) for p in files[-30:]], ignore_index=True)
            if "available_at" in fs.columns:
                fs = fs[pd.to_datetime(fs["available_at"], utc=True) <= pd.Timestamp(as_of)]
                fs = fs[pd.to_datetime(fs["available_at"], utc=True) > pd.Timestamp(as_of) - pd.Timedelta(hours=1)]
                if len(fs):
                    cols = [c for c in fs.columns if c in ("book_valid", "spread_bps", "bid_depth_10bps_quote", "ask_depth_10bps_quote", "imbalance_top10", "cvd_delta_base", "buy_base", "sell_base")]
                    summ = {c: (float(fs[c].astype(float).mean()) if c != "book_valid" else float(fs[c].astype(float).mean())) for c in cols}
                    ex = f"Live collector flow (last hour, {len(fs)} rows): " + json.dumps({k: round(v, 4) for k, v in summ.items()}) + \
                         ". Order-book walls/imbalance are observations; they do not reveal intent or account identity."
                    items.append(_item(f"mkt-flow-{tag}", SourceKind.MARKET_FLOW, "aqe.collector.flow_summaries", "Order-book / trade-flow summary",
                                       ex, pd.to_datetime(fs["available_at"], utc=True).max().to_pydatetime(), scope=[inst.instrument_id]))
    return items, SourceHealth(source_kind=SourceKind.MARKET_FLOW, status=SourceHealthStatus.AVAILABLE, detail=f"{len(items)} items", last_available_at=wm)


CALENDAR_LOOKBACK = timedelta(days=1)
CALENDAR_LOOKAHEAD = timedelta(days=3)


NEWS_MAX_LAG = timedelta(hours=1)  # verified news coverage must reach at least this close to as_of
MANIFEST_SUFFIX = ".manifest.json"
REQUIRED_MANIFEST_KEYS = ("source_id", "fetched_at", "coverage_start", "coverage_end", "content_sha256")


def load_import_manifests(directory: Path, data_glob: str, as_of: datetime) -> tuple[list[tuple[Path, dict]], list[str]]:
    """Data files usable at as_of together with their manifests (supplement B).

    A data file counts only if `<file>.manifest.json` exists, has the required keys, its
    `content_sha256` matches the file, coverage_start < coverage_end, and the manifest itself was
    available (`available_at`, default `fetched_at`) at or before as_of. Returns (usable, problems)."""
    from aqe.storage import sha256_file

    usable, problems = [], []
    for f in sorted(directory.glob(data_glob)) if directory.exists() else []:
        mp = f.with_name(f.name + MANIFEST_SUFFIX)
        if not mp.exists():
            problems.append(f"{f.name}: no manifest (coverage unverifiable)")
            continue
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
            missing = [k for k in REQUIRED_MANIFEST_KEYS if not m.get(k)]
            if missing:
                problems.append(f"{f.name}: manifest missing {missing}")
                continue
            if m["content_sha256"] != sha256_file(f):
                problems.append(f"{f.name}: content hash differs from manifest")
                continue
            start, end = pd.Timestamp(m["coverage_start"]), pd.Timestamp(m["coverage_end"])
            avail = pd.Timestamp(m.get("available_at") or m["fetched_at"])
            if start.tzinfo is None or end.tzinfo is None or avail.tzinfo is None or not start < end:
                problems.append(f"{f.name}: manifest times must be timezone-aware with coverage_start < coverage_end")
                continue
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            problems.append(f"{f.name}: unreadable manifest ({type(exc).__name__})")
            continue
        if avail > pd.Timestamp(as_of):
            problems.append(f"{f.name}: manifest not yet available at as_of")
            continue
        usable.append((f, {**m, "_start": start, "_end": end, "_avail": avail}))
    return usable, problems


def covering_interval(manifests: list[dict], point: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """Merged coverage interval (overlapping/adjacent manifests chained) that contains `point`."""
    spans = sorted((m["_start"], m["_end"]) for m in manifests)
    merged: list[list[pd.Timestamp]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    for s, e in merged:
        if s <= point <= e:
            return s, e
    return None


def calendar_events(cfg: LocalConfig, as_of: datetime) -> tuple[list[ScheduledEvent], SourceHealth]:
    d = cfg.paths.data_root / "imports" / "calendar"
    if not d.exists() or not any(d.glob("*.csv")):
        return [], SourceHealth(source_kind=SourceKind.MACRO_CALENDAR, status=SourceHealthStatus.NOT_CONFIGURED,
                                detail="no verified calendar import; macro/earnings schedule unknown")
    usable, problems = load_import_manifests(d, "*.csv", as_of)
    if not usable:
        return [], SourceHealth(source_kind=SourceKind.MACRO_CALENDAR, status=SourceHealthStatus.MISSING,
                                detail="no calendar import usable at as_of: " + "; ".join(problems[:5]))
    cover = covering_interval([m for _, m in usable], pd.Timestamp(as_of))
    df = pd.concat([pd.read_csv(f) for f, _ in usable], ignore_index=True)
    for c in ("scheduled_time", "available_at", "actual_available_at"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True)
    if len(df):
        df = df[df["available_at"] <= pd.Timestamp(as_of)]
        df = df.sort_values(["event_id", "revision"]).groupby("event_id").tail(1)
        win = (df["scheduled_time"] >= pd.Timestamp(as_of - CALENDAR_LOOKBACK)) & (df["scheduled_time"] <= pd.Timestamp(as_of + CALENDAR_LOOKAHEAD))
        df = df[win]
    out = []
    for _, r in df.iterrows():
        actual_ok = "actual_available_at" in r and pd.notna(r.get("actual_available_at")) and r["actual_available_at"] <= pd.Timestamp(as_of)
        out.append(ScheduledEvent(event_id=str(r["event_id"]), name=str(r["name"]), source_id=str(r["source_id"]),
                                  scheduled_time=r["scheduled_time"].to_pydatetime(), importance=str(r["importance"]).upper(),
                                  revision=int(r["revision"]), available_at=r["available_at"].to_pydatetime(),
                                  actual_value=str(r["actual_value"]) if actual_ok and pd.notna(r.get("actual_value")) else None,
                                  actual_available_at=r["actual_available_at"].to_pydatetime() if actual_ok else None))
    if cover is None:
        spans = ", ".join(f"{m['_start'].date()}..{m['_end'].date()}" for _, m in usable[:5])
        return out, SourceHealth(source_kind=SourceKind.MACRO_CALENDAR, status=SourceHealthStatus.STALE,
                                 detail=f"verified calendar coverage ({spans}) does not include as_of; {len(out)} known events")
    return out, SourceHealth(source_kind=SourceKind.MACRO_CALENDAR, status=SourceHealthStatus.AVAILABLE,
                             detail=f"{len(out)} events in window; verified coverage {cover[0].isoformat()}..{cover[1].isoformat()}",
                             last_available_at=max(m["_avail"] for _, m in usable).to_pydatetime(),
                             coverage_start=cover[0].to_pydatetime(), coverage_end=cover[1].to_pydatetime())


def news_items(cfg: LocalConfig, as_of: datetime) -> tuple[list[EvidenceItem], SourceHealth]:
    d = cfg.paths.data_root / "imports" / "news"
    if not d.exists() or not any(d.glob("*.jsonl")):
        return [], SourceHealth(source_kind=SourceKind.NEWS, status=SourceHealthStatus.NOT_CONFIGURED,
                                detail="no verified news import/API configured")
    usable, problems = load_import_manifests(d, "*.jsonl", as_of)
    if not usable:
        return [], SourceHealth(source_kind=SourceKind.NEWS, status=SourceHealthStatus.MISSING,
                                detail="no news import usable at as_of: " + "; ".join(problems[:5]))
    items = []
    for f, _ in usable:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            avail = pd.Timestamp(r["available_at"]).to_pydatetime()
            if avail > as_of or avail < as_of - timedelta(days=3):
                continue
            items.append(_item(r["evidence_id"], SourceKind.NEWS, r["source_id"], r["title"], r["excerpt"], avail,
                               published_at=pd.Timestamp(r["published_at"]).to_pydatetime() if r.get("published_at") else None,
                               received_at=pd.Timestamp(r["received_at"]).to_pydatetime(), url=r.get("source_url"),
                               status=VerificationStatus(r.get("verification_status", "UNVERIFIED"))))
    # news coverage can only extend up to when it was fetched; it must reach close to as_of
    cover = covering_interval([m for _, m in usable], pd.Timestamp(as_of - NEWS_MAX_LAG))
    if cover is None or cover[1] < pd.Timestamp(as_of - NEWS_MAX_LAG):
        return items, SourceHealth(source_kind=SourceKind.NEWS, status=SourceHealthStatus.STALE,
                                   detail=f"verified news coverage does not reach as_of - {NEWS_MAX_LAG}; {len(items)} items")
    return items, SourceHealth(source_kind=SourceKind.NEWS, status=SourceHealthStatus.AVAILABLE,
                               detail=f"{len(items)} items in last 3 days; verified coverage {cover[0].isoformat()}..{cover[1].isoformat()}",
                               coverage_start=cover[0].to_pydatetime(), coverage_end=cover[1].to_pydatetime())


def numeric_evidence(cfg: LocalConfig, inst: InstrumentKey, as_of: datetime) -> tuple[list[EvidenceItem], SourceHealth]:
    """Read-only adapter (debate proposal §10-1, §12): AQE's registered numeric predictions at as_of.

    Uses the same point-in-time path as `aqe analyze` (snapshot from bars available at as_of, artifact
    identity/cutoff/lineage checks, validation results only if established by as_of). The output is a
    model estimate with its readiness and restrictions; it is never re-written by the debate layer."""
    from aqe.config import load_experiment_config
    from aqe.contracts.core import RuntimeMode
    from aqe.ensemble.online import predict_horizon
    from aqe.features.ohlcv_v0 import MAX_LOOKBACK_BARS, compute_ohlcv_v0
    from aqe.features.snapshot import build_snapshot
    from aqe.storage.bars import to_grid

    try:
        exp, _ = load_experiment_config(cfg.analysis.experiment_config)
    except (OSError, ValueError) as exc:
        return [], SourceHealth(source_kind=SourceKind.AQE_NUMERIC, status=SourceHealthStatus.NOT_CONFIGURED,
                                detail=f"experiment config unavailable: {type(exc).__name__}")
    bars = read_bars(cfg.paths.data_root, inst, "5m", start=as_of - timedelta(days=12), end=as_of + timedelta(minutes=5))
    sel = bars_as_of(bars, as_of)
    if sel.empty:
        return [], SourceHealth(source_kind=SourceKind.AQE_NUMERIC, status=SourceHealthStatus.MISSING, detail="no bars at as_of")
    snap, _ = build_snapshot(bars, inst, as_of, exp.bar_interval_seconds)
    ff = compute_ohlcv_v0(to_grid(sel.tail(MAX_LOOKBACK_BARS + 600), exp.bar_interval_seconds), exp.bar_interval_seconds)
    items = []
    for h in exp.horizons_seconds:
        preds, ens, _ = predict_horizon(artifact_root=cfg.paths.artifact_root, experiment_id=exp.experiment_id, snapshot=snap,
                                        feature_frame=ff, horizon_seconds=h, mode=RuntimeMode.REPLAY,
                                        interval_seconds=exp.bar_interval_seconds, context=None)

        def fmt(p):
            return "n/a" if p is None else f"down {p.p_down:.3f} / neutral {p.p_neutral:.3f} / up {p.p_up:.3f}"

        base = "; ".join(f"{p.model_kind}: {fmt(p.probabilities)} ({p.status.value}, {p.readiness.value})" for p in preds)
        if ens.probabilities is not None:
            comb = f"calibrated combined: {fmt(ens.probabilities)}"
        else:
            comb = f"combined: ABSTAIN ({'; '.join(ens.abstain_reasons)[:300]})"
        excerpt = (f"AQE numeric model estimate for {inst.instrument_id}, horizon {h // 3600}h, as_of {iso_utc(as_of)} "
                   f"(feature profile {snap.feature_profile.value}, snapshot quality {snap.quality.value}). {comb}. Base models: {base}. "
                   f"Readiness {ens.readiness.value}; calibration validated at as_of: {ens.calibration_validated}; "
                   f"locked-test verdicts known at as_of: skill={ens.extra.get('oos_verdict')}, cost={ens.extra.get('oos_cost_verdict')}. "
                   "Class labels are vol-scaled 3-class directions; these are model probabilities, not facts, and carry no expected return.")
        items.append(_item(f"aqe-num-h{h}-{to_ms(as_of)}", SourceKind.AQE_NUMERIC, f"aqe.{exp.experiment_id}",
                           f"AQE numeric prediction ({h // 3600}h)", excerpt, as_of, event_time=as_of,
                           status=VerificationStatus.VERIFIED, scope=[inst.instrument_id], max_len=2000))
    return items, SourceHealth(source_kind=SourceKind.AQE_NUMERIC, status=SourceHealthStatus.AVAILABLE,
                               detail=f"{len(items)} horizon estimate(s) from {exp.experiment_id}", last_available_at=as_of)


def build_bundle(cfg: LocalConfig, inst: InstrumentKey, as_of: datetime, include_numeric: bool = False) -> EvidenceBundle:
    m_items, m_health = market_evidence(cfg, inst, as_of)
    cal, cal_health = calendar_events(cfg, as_of)
    n_items, n_health = news_items(cfg, as_of)
    health = [m_health, cal_health, n_health] + [
        SourceHealth(source_kind=k, status=SourceHealthStatus.NOT_CONFIGURED, detail="no provider connected")
        for k in (SourceKind.SOCIAL, SourceKind.CROSS_ASSET, SourceKind.ONCHAIN, SourceKind.CRYPTO_FUNDAMENTAL)
    ]
    num_items: list[EvidenceItem] = []
    if include_numeric:
        num_items, num_health = numeric_evidence(cfg, inst, as_of)
        health.append(num_health)
    items = tuple(m_items + n_items + num_items)
    h = compute_bundle_hash((inst,), as_of, items, tuple(cal), tuple(health))
    return EvidenceBundle(bundle_id=f"bundle-{inst.instrument_id}-{to_ms(as_of)}-{h[:10]}", instruments=(inst,), as_of=as_of,
                          items=items, calendar_snapshot=tuple(cal), source_health=tuple(health), bundle_hash=h)


_ = (utc_now, Path)
