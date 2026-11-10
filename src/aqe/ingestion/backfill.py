"""`aqe backfill` (B05.2, M1): public REST history with explicit provenance and limits.

* klines   — full history; provenance ASSUMED_LATENCY_REPLAY (downloaded today, replayed with
             available_at = bar close + assumed latency). NOT a record of historical receipt times.
* funding  — settled funding-rate history (/fapi/v1/fundingRate).
* oi       — /futures/data/openInterestHist; Binance serves only ~the latest 30 days. Older OI
             history is unavailable and is NOT reconstructed from current values.
Raw responses are kept in append-only gzip JSONL; normalized data in Parquet with a manifest.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from aqe.config import LocalConfig
from aqe.contracts.core import InstrumentKey, Provenance, from_ms, iso_utc, to_ms, utc_now
from aqe.ingestion.binance_rest import BinanceRest
from aqe.storage import RawLogWriter, atomic_write_json, sha256_file
from aqe.storage.bars import BAR_COLUMNS, INTERVAL_SECONDS, read_bars, write_bars
from aqe.storage.runs import EXIT_OK, RunRecord

KLINE_LIMIT = 1500  # max per Binance docs (weight 10 for limit > 1000)


def normalize_klines(rows: list[list], inst: InstrumentKey, interval_s: int, server_now_ms: int,
                     received_at: datetime, assumed_latency_s: float, run_id: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=BAR_COLUMNS)
    df = pd.DataFrame(rows, columns=["ot", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbb", "tbq", "ignore"])
    out = pd.DataFrame({
        "venue": inst.venue.value,
        "market_type": inst.market_type.value,
        "instrument_id": inst.instrument_id,
        "interval_seconds": interval_s,
        "open_time": pd.to_datetime(df["ot"].astype("int64"), unit="ms", utc=True),
    })
    out["close_time"] = out["open_time"] + pd.Timedelta(seconds=interval_s)
    for src, dst in [("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"),
                     ("qv", "quote_volume"), ("tbb", "taker_buy_base"), ("tbq", "taker_buy_quote")]:
        out[dst] = df[src].astype("float64")
    out["trade_count"] = df["n"].astype("int64")
    # a candle is final only once its full interval has elapsed on the exchange clock
    out["is_closed"] = (df["ot"].astype("int64") + interval_s * 1000) <= server_now_ms
    out["provenance"] = Provenance.ASSUMED_LATENCY_REPLAY.value
    out["received_at"] = pd.Timestamp(received_at)
    out["available_at"] = out["close_time"] + pd.Timedelta(seconds=assumed_latency_s)
    out["source_id"] = df["ot"].astype("int64")
    out["ingest_run_id"] = run_id
    return out[BAR_COLUMNS]


def find_gaps(open_times: pd.Series, interval_s: int) -> list[dict]:
    t = open_times.sort_values().reset_index(drop=True)
    d = t.diff().dt.total_seconds()
    gaps = []
    for i in d[d > interval_s].index:
        gaps.append({"after": iso_utc(t[i - 1].to_pydatetime()), "before": iso_utc(t[i].to_pydatetime()),
                     "missing_bars": int(d[i] // interval_s) - 1})
    return gaps


def backfill_klines(cfg: LocalConfig, rest: BinanceRest, inst: InstrumentKey, rec: RunRecord) -> dict:
    interval = cfg.backfill.kline_interval
    interval_s = INTERVAL_SECONDS[interval]
    existing = read_bars(cfg.paths.data_root, inst, interval)
    start = cfg.backfill.start
    if not existing.empty:
        closed = existing[existing["is_closed"]]
        if not closed.empty:
            start = closed["open_time"].max().to_pydatetime() + timedelta(seconds=interval_s)
    server_now = rest.get("/fapi/v1/time")["serverTime"]
    end_ms = to_ms(cfg.backfill.end) if cfg.backfill.end else server_now
    cursor = to_ms(start)
    total, requests, batch = 0, 0, []
    while cursor < end_ms:
        rows = rest.get("/fapi/v1/klines", {"symbol": inst.instrument_id, "interval": interval, "startTime": cursor,
                                            "endTime": end_ms, "limit": KLINE_LIMIT}, weight=10)
        requests += 1
        if not rows:
            # nothing before listing date; jump forward
            cursor += KLINE_LIMIT * interval_s * 1000
            continue
        df = normalize_klines(rows, inst, interval_s, server_now, utc_now(), cfg.backfill.assumed_latency_seconds, rec.run_id)
        batch.append(df)
        total += len(df)
        cursor = int(rows[-1][0]) + interval_s * 1000
        if sum(len(b) for b in batch) >= 50_000:
            write_bars(pd.concat(batch, ignore_index=True), cfg.paths.data_root, inst, interval)
            batch = []
            print(f"  klines: {total} rows up to {iso_utc(from_ms(cursor))}", flush=True)
    if batch:
        write_bars(pd.concat(batch, ignore_index=True), cfg.paths.data_root, inst, interval)
    allbars = read_bars(cfg.paths.data_root, inst, interval)
    closed = allbars[allbars["is_closed"]]
    gaps = find_gaps(closed["open_time"], interval_s)
    from aqe.storage.bars import bars_dir

    files = sorted(bars_dir(cfg.paths.data_root, inst, interval).glob("*.parquet"))
    manifest = {
        "dataset": "market_bars", "instrument": inst.model_dump(mode="json"), "interval": interval,
        "provenance": Provenance.ASSUMED_LATENCY_REPLAY.value,
        "provenance_note": "Downloaded via public REST after the fact; available_at = close_time + assumed latency. Not historical receipt times.",
        "assumed_latency_seconds": cfg.backfill.assumed_latency_seconds,
        "first_open_time": iso_utc(closed["open_time"].min().to_pydatetime()) if not closed.empty else None,
        "last_open_time": iso_utc(closed["open_time"].max().to_pydatetime()) if not closed.empty else None,
        "rows_closed": int(len(closed)), "rows_open": int((~allbars["is_closed"]).sum()),
        "gaps": gaps, "n_gaps": len(gaps), "missing_bars_total": int(sum(g["missing_bars"] for g in gaps)),
        "files": [{"path": str(f), "sha256": sha256_file(f)} for f in files],
        "updated_at": iso_utc(utc_now()), "last_run_id": rec.run_id,
    }
    mpath = cfg.paths.data_root / "manifests" / f"bars_{inst.slug()}_{interval}.json"
    atomic_write_json(mpath, manifest)
    return {"fetched_rows": total, "requests": requests, "manifest": str(mpath), "rows_closed": manifest["rows_closed"],
            "n_gaps": len(gaps), "missing_bars_total": manifest["missing_bars_total"]}


def _norm_dir(cfg: LocalConfig, kind: str, inst: InstrumentKey) -> Path:
    return cfg.paths.data_root / "normalized" / kind / inst.venue.value / inst.market_type.value / inst.instrument_id


def backfill_funding(cfg: LocalConfig, rest: BinanceRest, inst: InstrumentKey, rec: RunRecord) -> dict:
    out_dir = _norm_dir(cfg, "funding", inst)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "funding_history.parquet"
    old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    cursor = to_ms(datetime(2019, 9, 1, tzinfo=timezone.utc))  # startTime=0 is treated as "not sent"
    if not old.empty:
        cursor = int(old["funding_time"].max().value // 1_000_000) + 1
    rows_all = []
    while True:
        rows = rest.get("/fapi/v1/fundingRate", {"symbol": inst.instrument_id, "startTime": cursor, "limit": 1000}, weight=1)
        if not rows:
            break
        rows_all.extend(rows)
        cursor = int(rows[-1]["fundingTime"]) + 1
        if len(rows) < 1000:
            break
    received = utc_now()
    if rows_all:
        df = pd.DataFrame(rows_all)
        new = pd.DataFrame({
            "instrument_id": inst.instrument_id,
            "funding_time": pd.to_datetime(df["fundingTime"].astype("int64"), unit="ms", utc=True),
            "funding_rate": df["fundingRate"].astype("float64"),  # fraction per funding interval (not bps)
            "mark_price": pd.to_numeric(df.get("markPrice"), errors="coerce"),
            "kind": "SETTLED",
            "provenance": Provenance.ASSUMED_LATENCY_REPLAY.value,
            "received_at": pd.Timestamp(received),
        })
        new["available_at"] = new["funding_time"] + pd.Timedelta(seconds=cfg.backfill.assumed_latency_seconds)
        new["source_id"] = df["fundingTime"].astype("int64")
        merged = pd.concat([old, new], ignore_index=True).drop_duplicates(["instrument_id", "source_id"], keep="first")
        merged.sort_values("funding_time").reset_index(drop=True).to_parquet(path, index=False)
    final = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    return {"new_rows": len(rows_all), "rows": len(final), "path": str(path),
            "first": iso_utc(final["funding_time"].min().to_pydatetime()) if len(final) else None,
            "last": iso_utc(final["funding_time"].max().to_pydatetime()) if len(final) else None}


def backfill_oi_hist(cfg: LocalConfig, rest: BinanceRest, inst: InstrumentKey, rec: RunRecord) -> dict:
    period = cfg.backfill.oi_hist_period
    period_s = INTERVAL_SECONDS.get(period, 300)
    out_dir = _norm_dir(cfg, "oi_hist", inst)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"oi_hist_{period}.parquet"
    old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    server_now = rest.get("/fapi/v1/time")["serverTime"]
    earliest_allowed = server_now - 29 * 86400 * 1000  # docs: only the latest 1 month is available
    cursor = earliest_allowed
    if not old.empty:
        cursor = max(cursor, int(old["timestamp"].max().value // 1_000_000) + 1)
    rows_all = []
    while cursor < server_now:
        end = min(server_now, cursor + 500 * period_s * 1000)
        rows = rest.get("/futures/data/openInterestHist", {"symbol": inst.instrument_id, "period": period,
                                                             "startTime": cursor, "endTime": end, "limit": 500}, weight=1)
        rows_all.extend(rows or [])
        cursor = end + 1
    received = utc_now()
    if rows_all:
        df = pd.DataFrame(rows_all)
        new = pd.DataFrame({
            "instrument_id": inst.instrument_id,
            "timestamp": pd.to_datetime(df["timestamp"].astype("int64"), unit="ms", utc=True),
            "sum_open_interest_base": df["sumOpenInterest"].astype("float64"),
            "sum_open_interest_value_quote": df["sumOpenInterestValue"].astype("float64"),
            "period": period,
            "provenance": Provenance.ASSUMED_LATENCY_REPLAY.value,
            "received_at": pd.Timestamp(received),
        })
        new["available_at"] = new["timestamp"] + pd.Timedelta(seconds=period_s + cfg.backfill.assumed_latency_seconds)
        new["source_id"] = df["timestamp"].astype("int64")
        merged = pd.concat([old, new], ignore_index=True).drop_duplicates(["instrument_id", "source_id"], keep="first")
        merged.sort_values("timestamp").reset_index(drop=True).to_parquet(path, index=False)
    final = pd.read_parquet(path) if path.exists() else pd.DataFrame()
    return {"new_rows": len(rows_all), "rows": len(final), "path": str(path),
            "limitation": "Binance serves only ~30 days of OI statistics; older OI history is not available and was not fabricated.",
            "first": iso_utc(final["timestamp"].min().to_pydatetime()) if len(final) else None,
            "last": iso_utc(final["timestamp"].max().to_pydatetime()) if len(final) else None}


def run_backfill(cfg: LocalConfig, rec: RunRecord, what: list[str], dry_run: bool = False) -> int:
    if dry_run:
        rec.outputs["plan"] = {"instruments": [i.slug() for i in cfg.instruments], "what": what,
                               "klines_from": iso_utc(cfg.backfill.start), "interval": cfg.backfill.kline_interval}
        rec.notes.append("dry-run: nothing fetched")
        return EXIT_OK
    raw = RawLogWriter(cfg.paths.data_root / "raw" / "binance" / "rest", "rest_backfill",
                       max_bytes=cfg.collector.rotate_max_mb << 20, max_seconds=cfg.collector.rotate_max_minutes * 60)
    rest = BinanceRest(cfg.binance, raw=raw)
    try:
        for inst in cfg.instruments:
            res = {}
            if "klines" in what:
                res["klines"] = backfill_klines(cfg, rest, inst, rec)
            if "funding" in what and cfg.backfill.funding_history:
                res["funding"] = backfill_funding(cfg, rest, inst, rec)
            if "oi" in what and cfg.backfill.oi_hist:
                res["oi_hist"] = backfill_oi_hist(cfg, rest, inst, rec)
            rec.outputs[inst.slug()] = res
    finally:
        raw.close()
        rest.close()
    rec.outputs["raw_files"] = [str(p) for p in raw.closed_files[-5:]]
    return EXIT_OK
