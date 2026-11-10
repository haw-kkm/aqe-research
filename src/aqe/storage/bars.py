"""Normalized OHLCV bar storage (Parquet, monthly partitions) and as-of selection (B04, B05.4).

Columns (market_bars):
  venue, market_type, instrument_id, interval_seconds,
  open_time, close_time (= open_time + interval, exclusive boundary, UTC),
  open, high, low, close, volume, quote_volume, trade_count, taker_buy_base, taker_buy_quote,
  is_closed, provenance, received_at, available_at, source_id (open time ms), ingest_run_id
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from aqe.contracts.core import InstrumentKey, Provenance

BAR_COLUMNS = [
    "venue", "market_type", "instrument_id", "interval_seconds",
    "open_time", "close_time", "open", "high", "low", "close", "volume", "quote_volume",
    "trade_count", "taker_buy_base", "taker_buy_quote", "is_closed", "provenance",
    "received_at", "available_at", "source_id", "ingest_run_id",
]

INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


def bars_dir(data_root: Path, inst: InstrumentKey, interval: str) -> Path:
    return Path(data_root) / "normalized" / "bars" / inst.venue.value / inst.market_type.value / inst.instrument_id / interval


def write_bars(df: pd.DataFrame, data_root: Path, inst: InstrumentKey, interval: str) -> list[Path]:
    """Upsert bars into monthly Parquet partitions, de-duplicating on (instrument, open_time).

    A later record for the same open_time replaces an earlier one only if the earlier one was
    not closed (an open candle is never final)."""
    if df.empty:
        return []
    out_dir = bars_dir(data_root, inst, interval)
    out_dir.mkdir(parents=True, exist_ok=True)
    df = df[BAR_COLUMNS].copy()
    df["month"] = df["open_time"].dt.strftime("%Y-%m")
    written = []
    for month, part in df.groupby("month"):
        path = out_dir / f"{month}.parquet"
        part = part.drop(columns=["month"])
        if path.exists():
            old = pd.read_parquet(path)
            merged = pd.concat([old, part], ignore_index=True)
            merged = merged.sort_values(["open_time", "is_closed", "received_at"])
            merged = merged.drop_duplicates(subset=["instrument_id", "open_time"], keep="last")
        else:
            merged = part.sort_values("open_time").drop_duplicates(subset=["instrument_id", "open_time"], keep="last")
        tmp = path.with_suffix(".parquet.tmp")
        merged.reset_index(drop=True).to_parquet(tmp, index=False)
        tmp.replace(path)
        written.append(path)
    return written


def read_bars(data_root: Path, inst: InstrumentKey, interval: str, start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
    d = bars_dir(data_root, inst, interval)
    files = sorted(d.glob("*.parquet"))
    if not files:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    if start is not None:
        df = df[df["open_time"] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df["open_time"] < pd.Timestamp(end)]
    return df.sort_values("open_time").reset_index(drop=True)


def bars_as_of(df: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    """Only closed bars whose available_at <= as_of (T01). Revisions with later available_at are excluded."""
    ts = pd.Timestamp(as_of)
    sel = df[(df["is_closed"]) & (df["available_at"] <= ts) & (df["close_time"] <= ts)]
    return sel.sort_values(["open_time", "available_at"]).drop_duplicates("open_time", keep="last").reset_index(drop=True)


def to_grid(df: pd.DataFrame, interval_seconds: int) -> pd.DataFrame:
    """Reindex closed bars onto a complete interval grid (index = open_time). Missing bars -> NaN rows."""
    if df.empty:
        return df.set_index("open_time")
    df = df[df["is_closed"]].drop_duplicates("open_time", keep="last").set_index("open_time").sort_index()
    full = pd.date_range(df.index[0], df.index[-1], freq=pd.Timedelta(seconds=interval_seconds), tz="UTC")
    grid = df.reindex(full)
    grid.index.name = "open_time"
    grid["present"] = grid["close"].notna()
    return grid


def load_fixture_bars(csv_path: Path, inst: InstrumentKey, assumed_latency_seconds: float = 2.0) -> pd.DataFrame:
    raw = pd.read_csv(csv_path)
    df = pd.DataFrame({
        "venue": inst.venue.value,
        "market_type": inst.market_type.value,
        "instrument_id": inst.instrument_id,
        "interval_seconds": 300,
        "open_time": pd.to_datetime(raw["open_time"], utc=True),
    })
    for c in ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]:
        df[c] = raw[c].astype("float64")
    df["trade_count"] = raw["trade_count"].astype("int64")
    df["close_time"] = df["open_time"] + pd.Timedelta(seconds=300)
    df["is_closed"] = True
    df["provenance"] = Provenance.SYNTHETIC_FIXTURE.value
    df["available_at"] = df["close_time"] + pd.Timedelta(seconds=assumed_latency_seconds)
    df["received_at"] = df["available_at"]
    df["source_id"] = (df["open_time"].astype("int64") // 1_000_000).astype("int64")
    df["ingest_run_id"] = "fixture"
    return df[BAR_COLUMNS]


def make_synthetic_bars(n: int, start: datetime, seed: int = 7) -> pd.DataFrame:
    """Deterministic synthetic 5m bars (fixture only; no market meaning)."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, 0.0015, n)
    close = 50000 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[50000.0], close[:-1]])
    spread = np.abs(rng.normal(0, 0.001, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    vol = rng.lognormal(5, 0.5, n)
    tb = vol * rng.uniform(0.35, 0.65, n)
    t0 = pd.Timestamp(start)
    return pd.DataFrame({
        "open_time": [(t0 + pd.Timedelta(minutes=5 * i)).isoformat() for i in range(n)],
        "open": open_.round(2), "high": high.round(2), "low": low.round(2), "close": close.round(2),
        "volume": vol.round(3), "quote_volume": (vol * close).round(2), "trade_count": rng.integers(500, 5000, n),
        "taker_buy_base": tb.round(3), "taker_buy_quote": (tb * close).round(2),
    })


_ = timedelta
