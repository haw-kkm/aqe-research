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


def calendar_events(cfg: LocalConfig, as_of: datetime) -> tuple[list[ScheduledEvent], SourceHealth]:
    d = cfg.paths.data_root / "imports" / "calendar"
    files = sorted(d.glob("*.csv")) if d.exists() else []
    if not files:
        return [], SourceHealth(source_kind=SourceKind.MACRO_CALENDAR, status=SourceHealthStatus.NOT_CONFIGURED,
                                detail="no verified calendar import; macro/earnings schedule unknown")
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    for c in ("scheduled_time", "available_at", "actual_available_at"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True)
    df = df[df["available_at"] <= pd.Timestamp(as_of)]
    df = df.sort_values(["event_id", "revision"]).groupby("event_id").tail(1)
    win = (df["scheduled_time"] >= pd.Timestamp(as_of - CALENDAR_LOOKBACK)) & (df["scheduled_time"] <= pd.Timestamp(as_of + CALENDAR_LOOKAHEAD))
    out = []
    for _, r in df[win].iterrows():
        actual_ok = "actual_available_at" in r and pd.notna(r.get("actual_available_at")) and r["actual_available_at"] <= pd.Timestamp(as_of)
        out.append(ScheduledEvent(event_id=str(r["event_id"]), name=str(r["name"]), source_id=str(r["source_id"]),
                                  scheduled_time=r["scheduled_time"].to_pydatetime(), importance=str(r["importance"]).upper(),
                                  revision=int(r["revision"]), available_at=r["available_at"].to_pydatetime(),
                                  actual_value=str(r["actual_value"]) if actual_ok and pd.notna(r.get("actual_value")) else None,
                                  actual_available_at=r["actual_available_at"].to_pydatetime() if actual_ok else None))
    return out, SourceHealth(source_kind=SourceKind.MACRO_CALENDAR, status=SourceHealthStatus.AVAILABLE, detail=f"{len(out)} events in window")


def news_items(cfg: LocalConfig, as_of: datetime) -> tuple[list[EvidenceItem], SourceHealth]:
    d = cfg.paths.data_root / "imports" / "news"
    files = sorted(d.glob("*.jsonl")) if d.exists() else []
    if not files:
        return [], SourceHealth(source_kind=SourceKind.NEWS, status=SourceHealthStatus.NOT_CONFIGURED,
                                detail="no verified news import/API configured")
    items = []
    for f in files:
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
    return items, SourceHealth(source_kind=SourceKind.NEWS, status=SourceHealthStatus.AVAILABLE if items else SourceHealthStatus.STALE,
                               detail=f"{len(items)} items in last 3 days")


def build_bundle(cfg: LocalConfig, inst: InstrumentKey, as_of: datetime) -> EvidenceBundle:
    m_items, m_health = market_evidence(cfg, inst, as_of)
    cal, cal_health = calendar_events(cfg, as_of)
    n_items, n_health = news_items(cfg, as_of)
    health = [m_health, cal_health, n_health] + [
        SourceHealth(source_kind=k, status=SourceHealthStatus.NOT_CONFIGURED, detail="no provider connected")
        for k in (SourceKind.SOCIAL, SourceKind.CROSS_ASSET, SourceKind.ONCHAIN, SourceKind.CRYPTO_FUNDAMENTAL)
    ]
    items = tuple(m_items + n_items)
    h = compute_bundle_hash((inst,), as_of, items, tuple(cal), tuple(health))
    return EvidenceBundle(bundle_id=f"bundle-{inst.instrument_id}-{to_ms(as_of)}-{h[:10]}", instruments=(inst,), as_of=as_of,
                          items=items, calendar_snapshot=tuple(cal), source_health=tuple(health), bundle_hash=h)


_ = (utc_now, Path)
