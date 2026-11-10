"""`aqe collect` — continuous public-data collection for one USDⓈ-M instrument (B05.2–B05.4, M1).

Connections (2026 Binance path split, see docs/binance notes):
  {ws_public_base}/stream?streams=<sym>@depth@<speed>                       -> local L2 book
  {ws_market_base}/stream?streams=<sym>@aggTrade/<sym>@markPrice@1s/<sym>@kline_5m

Guarantees / non-guarantees:
* every WS message and REST response is written raw (append-only gzip JSONL) with the local
  raw clock, the measured exchange clock offset and the offset-corrected received_at;
* at-least-once processing with de-duplication by source id; exactly-once is NOT claimed;
* the L2 book follows the USDⓈ-M U/u/pu procedure; after any (re)connect it is BUFFERING and no
  book metrics are produced until resynchronised (orderbook.py);
* a silent connection (wrong path, stalled feed) is detected by a no-data watchdog and reconnected;
* open interest is polled as current observations only (never turned into history);
* markPrice `r` is stored as an ESTIMATED funding observation (distinct from settled funding);
* closed live klines are stored as LIVE_CAPTURE bars in a separate `5m_live` partition so they do
  not interfere with the REST backfill (ASSUMED_LATENCY_REPLAY) partition `5m`.
Public endpoints only — no order or signed endpoint exists here (T14).
"""

from __future__ import annotations

import asyncio
import json
import random
import shutil
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from aqe.config import LocalConfig
from aqe.contracts.core import InstrumentKey, Provenance, from_ms, iso_utc
from aqe.ingestion.orderbook import DepthEvent, UsdmOrderBook
from aqe.ingestion.trades import AggTrade, AggTradeTracker
from aqe.storage import RawLogWriter, atomic_write_json, read_json
from aqe.storage.bars import BAR_COLUMNS, write_bars
from aqe.storage.runs import EXIT_OK, RunRecord

GROUP_PUBLIC = "public"
GROUP_MARKET = "market"
WATCHDOG_SECONDS = 30.0
MAX_CONNECTION_SECONDS = 23.5 * 3600  # Binance drops connections at 24h; reconnect proactively
SNAPSHOT_MIN_INTERVAL_SECONDS = 5.0
LIVE_BAR_INTERVAL_KEY = "5m_live"


def _upsert_parquet(path: Path, rows: list[dict[str, Any]], keys: list[str]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame(rows)
    if path.exists():
        old = pd.read_parquet(path)
        new = pd.concat([old, new], ignore_index=True)
    new = new.drop_duplicates(subset=keys, keep="last").sort_values(keys).reset_index(drop=True)
    tmp = path.with_suffix(".parquet.tmp")
    new.to_parquet(tmp, index=False)
    tmp.replace(path)


class Collector:
    def __init__(self, cfg: LocalConfig, inst: InstrumentKey, run_id: str):
        self.cfg = cfg
        self.inst = inst
        self.run_id = run_id
        self.symbol = inst.instrument_id
        self.sym_l = self.symbol.lower()
        root = Path(cfg.paths.data_root)
        self.root = root
        cc = cfg.collector
        mk = lambda g: RawLogWriter(root / "raw" / "binance" / "ws" / g, f"{g}_{self.symbol}",  # noqa: E731
                                    max_bytes=cc.rotate_max_mb << 20, max_seconds=cc.rotate_max_minutes * 60)
        self.raw = {GROUP_PUBLIC: mk(GROUP_PUBLIC), GROUP_MARKET: mk(GROUP_MARKET), "rest": mk("rest")}
        self.book = UsdmOrderBook(self.symbol)
        self.trades = AggTradeTracker(self.symbol)
        self.offset_ms = 0.0  # server - local
        self.offset_measured = False
        self.clock_rtt_ms: float | None = None
        self.clock_measured_at: str | None = None
        self.counts: Counter = Counter()
        self.last_msg: dict[str, str] = {}
        self.conn: dict[str, dict[str, Any]] = {
            g: {"state": "DISCONNECTED", "connects": 0, "messages": 0, "last_connect_at": None, "last_message_at": None,
                "watchdog_reconnects": 0, "errors": 0, "last_error": None, "url": None}
            for g in (GROUP_PUBLIC, GROUP_MARKET)
        }
        self.quality_log: deque = deque(maxlen=300)
        self.flow_rows: list[dict] = []
        self.mark_rows: list[dict] = []
        self.oi_rows: list[dict] = []
        self.bar_rows: list[dict] = []
        self.snapshots_ok = 0
        self.snapshots_failed = 0
        self.parse_errors = 0
        self.last_kline_open_ms: int | None = None
        self._conn_gen = 0
        self._last_snapshot_attempt = 0.0
        self._closed = False
        self.started_local_ms = time.time() * 1000
        self.written_files: set[str] = set()
        base = (inst.venue.value, inst.market_type.value, self.symbol)
        self.flow_dir = root.joinpath("normalized", "flow_summaries", *base)
        self.mark_dir = root.joinpath("normalized", "mark_funding_estimates", *base)
        self.oi_dir = root.joinpath("normalized", "oi_observations", *base)
        self.status_path = root / "status" / f"collector_{self.symbol}.json"
        self.ckpt_path = root / "status" / f"collector_{self.symbol}.checkpoint.json"
        self.load_checkpoint()

    # ------------------------------------------------------------------ clock
    def now_local_ms(self) -> float:
        return time.time() * 1000

    def corrected(self, local_ms: float) -> datetime:
        return from_ms(int(round(local_ms + self.offset_ms)))

    def set_clock(self, offset_ms: float, rtt_ms: float) -> None:
        self.offset_ms = offset_ms
        self.clock_rtt_ms = rtt_ms
        self.offset_measured = True
        self.clock_measured_at = iso_utc(self.corrected(self.now_local_ms()))

    # ------------------------------------------------------------------ quality
    def quality(self, kind: str, detail: str, **extra: Any) -> None:
        self.quality_log.append({"at": iso_utc(self.corrected(self.now_local_ms())), "kind": kind, "detail": detail, **extra})

    def _drain_book_quality(self) -> None:
        for q in self.book.drain_quality_events():
            if q.kind == "DROPPED_DUPLICATE":
                continue
            self.quality("BOOK_" + q.kind, q.reason, from_state=q.from_state, to_state=q.to_state, update_id=q.update_id)

    # ------------------------------------------------------------------ message handling (network-free)
    def handle_text(self, group: str, text: str, local_ms: float | None = None) -> str:
        local_ms = self.now_local_ms() if local_ms is None else local_ms
        recv = self.corrected(local_ms)
        self.raw[group].write({"g": group, "local_ms": int(local_ms), "offset_ms": round(self.offset_ms, 1),
                               "offset_measured": self.offset_measured, "received_at": iso_utc(recv), "raw": text})
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            self.parse_errors += 1
            self.quality("PARSE_ERROR", text[:200])
            return "invalid"
        if isinstance(msg, dict) and "id" in msg and "result" in msg:
            return "control"
        data = msg.get("data", msg) if isinstance(msg, dict) else msg
        stream = msg.get("stream", "") if isinstance(msg, dict) else ""
        kind = self._classify(stream, data)
        self.counts[kind] += 1
        self.last_msg[kind] = iso_utc(recv)
        if group in self.conn:
            self.conn[group]["messages"] += 1
            self.conn[group]["last_message_at"] = iso_utc(recv)
        try:
            if kind == "depth":
                self.book.on_event(DepthEvent.from_payload(data))
                self._drain_book_quality()
            elif kind == "aggTrade":
                self.trades.on_trade(AggTrade.from_payload(data, self.symbol))
            elif kind == "markPrice":
                self._on_mark(data, recv)
            elif kind == "kline":
                self._on_kline(data, recv)
        except (ValueError, KeyError, TypeError) as exc:
            self.parse_errors += 1
            self.quality("PAYLOAD_ERROR", f"{kind}: {type(exc).__name__}: {exc}")
        return kind

    @staticmethod
    def _classify(stream: str, data: Any) -> str:
        e = data.get("e") if isinstance(data, dict) else None
        if e == "depthUpdate" or "@depth" in stream:
            return "depth"
        if e == "aggTrade" or stream.endswith("@aggTrade"):
            return "aggTrade"
        if e == "markPriceUpdate" or "@markPrice" in stream:
            return "markPrice"
        if e == "kline" or "@kline_" in stream:
            return "kline"
        return "other"

    def _on_mark(self, d: dict, recv: datetime) -> None:
        if str(d["s"]).upper() != self.symbol:
            raise ValueError(f"markPrice for {d['s']} on {self.symbol} collector")
        self.mark_rows.append({
            "instrument_id": self.symbol, "event_time": pd.Timestamp(from_ms(int(d["E"]))),
            "mark_price": float(d["p"]), "index_price": float(d["i"]),
            "est_settle_price": float(d["P"]) if d.get("P") not in (None, "") else None,
            "funding_rate_est": float(d["r"]) if d.get("r") not in (None, "") else None,  # fraction per funding interval
            "next_funding_time": pd.Timestamp(from_ms(int(d["T"]))) if d.get("T") else None,
            "kind": "ESTIMATED", "provenance": Provenance.LIVE_CAPTURE.value,
            "received_at": pd.Timestamp(recv), "available_at": pd.Timestamp(recv), "source_id": int(d["E"]),
        })

    def _on_kline(self, d: dict, recv: datetime) -> None:
        k = d["k"]
        if str(k.get("s", d.get("s"))).upper() != self.symbol:
            raise ValueError("kline symbol mismatch")
        if k.get("i") != "5m" or not k.get("x"):
            return  # only completed 5m candles are stored
        ot = int(k["t"])
        self.bar_rows.append({
            "venue": self.inst.venue.value, "market_type": self.inst.market_type.value, "instrument_id": self.symbol,
            "interval_seconds": 300, "open_time": pd.Timestamp(from_ms(ot)), "close_time": pd.Timestamp(from_ms(ot + 300_000)),
            "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]), "close": float(k["c"]),
            "volume": float(k["v"]), "quote_volume": float(k["q"]), "trade_count": int(k["n"]),
            "taker_buy_base": float(k["V"]), "taker_buy_quote": float(k["Q"]), "is_closed": True,
            "provenance": Provenance.LIVE_CAPTURE.value, "received_at": pd.Timestamp(recv), "available_at": pd.Timestamp(recv),
            "source_id": ot, "ingest_run_id": self.run_id,
        })
        self.last_kline_open_ms = max(ot, self.last_kline_open_ms or 0)

    def apply_snapshot_payload(self, payload: dict, local_ms: float | None = None) -> bool:
        local_ms = self.now_local_ms() if local_ms is None else local_ms
        self.raw["rest"].write({"g": "rest", "path": "/fapi/v1/depth", "local_ms": int(local_ms), "offset_ms": round(self.offset_ms, 1),
                                "received_at": iso_utc(self.corrected(local_ms)), "raw": json.dumps(payload, separators=(",", ":"))})
        ok = self.book.apply_snapshot(int(payload["lastUpdateId"]), payload.get("bids", []), payload.get("asks", []))
        self.snapshots_ok += 1
        self._drain_book_quality()
        return ok

    def on_oi_payload(self, payload: dict, local_ms: float | None = None) -> None:
        local_ms = self.now_local_ms() if local_ms is None else local_ms
        recv = self.corrected(local_ms)
        self.raw["rest"].write({"g": "rest", "path": "/fapi/v1/openInterest", "local_ms": int(local_ms),
                                "offset_ms": round(self.offset_ms, 1), "received_at": iso_utc(recv),
                                "raw": json.dumps(payload, separators=(",", ":"))})
        if str(payload.get("symbol", "")).upper() != self.symbol:
            raise ValueError("openInterest symbol mismatch")
        self.counts["openInterest"] += 1
        self.last_msg["openInterest"] = iso_utc(recv)
        self.oi_rows.append({
            "instrument_id": self.symbol, "exchange_time": pd.Timestamp(from_ms(int(payload["time"]))),
            "open_interest_base": float(payload["openInterest"]), "kind": "CURRENT_OBSERVATION",
            "provenance": Provenance.LIVE_CAPTURE.value, "received_at": pd.Timestamp(recv),
            "available_at": pd.Timestamp(recv), "source_id": int(payload["time"]),
        })

    def flow_tick(self, interval_start: datetime, interval_end: datetime) -> dict[str, Any]:
        """Close one flow-summary interval. Trade flow = trades received in the interval; book metrics
        are a point-in-time snapshot at interval end and only when the book is SYNCED."""
        m = self.book.metrics(depth_bps=(10.0, 25.0), top_n=10)
        self._drain_book_quality()
        iv = self.trades.pop_interval()
        now = self.corrected(self.now_local_ms())
        row: dict[str, Any] = {
            "instrument_id": self.symbol, "interval_start": pd.Timestamp(interval_start), "interval_end": pd.Timestamp(interval_end),
            "book_state": self.book.state.value, "book_valid": m is not None,
            "book_invalid_reason": None if m is not None else f"book state {self.book.state.value}",
            "best_bid": float(m.best_bid) if m else None, "best_ask": float(m.best_ask) if m else None,
            "mid": float(m.mid) if m else None, "spread_bps": m.spread_bps if m else None,
            "depth_bid_10bps_quote": m.depth_quote.get("bid_10bps") if m else None,
            "depth_ask_10bps_quote": m.depth_quote.get("ask_10bps") if m else None,
            "depth_bid_25bps_quote": m.depth_quote.get("bid_25bps") if m else None,
            "depth_ask_25bps_quote": m.depth_quote.get("ask_25bps") if m else None,
            "imbalance_top10": m.imbalance_top_n if m else None,
            "book_last_update_id": m.last_update_id if m else None,
            "n_trades": iv.n_trades, "buy_base": float(iv.buy_base), "sell_base": float(iv.sell_base),
            "buy_quote": float(iv.buy_quote), "sell_quote": float(iv.sell_quote),
            "cvd_delta_base": float(iv.cvd_delta_base), "cvd_cum_base": float(self.trades.cvd_base),
            "cvd_start": pd.Timestamp(from_ms(self.trades.cvd_start_ms)) if self.trades.cvd_start_ms else None,
            "agg_missing": iv.missing, "agg_duplicates": iv.duplicates, "agg_late": iv.late,
            "provenance": Provenance.LIVE_CAPTURE.value, "received_at": pd.Timestamp(now), "available_at": pd.Timestamp(now),
        }
        self.flow_rows.append(row)
        return row

    # ------------------------------------------------------------------ persistence
    def flush(self) -> None:
        if self.flow_rows:
            rows, self.flow_rows = self.flow_rows, []
            df = pd.DataFrame(rows)
            for (day, hour), part in df.groupby([df["interval_start"].dt.strftime("%Y-%m-%d"), df["interval_start"].dt.strftime("%H")]):
                p = self.flow_dir / day / f"{hour}.parquet"
                _upsert_parquet(p, part.to_dict("records"), ["instrument_id", "interval_start"])
                self.written_files.add(str(p))
        if self.mark_rows:
            rows, self.mark_rows = self.mark_rows, []
            df = pd.DataFrame(rows)
            for (day, hour), part in df.groupby([df["event_time"].dt.strftime("%Y-%m-%d"), df["event_time"].dt.strftime("%H")]):
                p = self.mark_dir / day / f"{hour}.parquet"
                _upsert_parquet(p, part.to_dict("records"), ["instrument_id", "source_id"])
                self.written_files.add(str(p))
        if self.oi_rows:
            rows, self.oi_rows = self.oi_rows, []
            df = pd.DataFrame(rows)
            for day, part in df.groupby(df["received_at"].dt.strftime("%Y-%m-%d")):
                p = self.oi_dir / f"{day}.parquet"
                _upsert_parquet(p, part.to_dict("records"), ["instrument_id", "received_at"])
                self.written_files.add(str(p))
        if self.bar_rows:
            rows, self.bar_rows = self.bar_rows, []
            for p in write_bars(pd.DataFrame(rows)[BAR_COLUMNS], self.root, self.inst, LIVE_BAR_INTERVAL_KEY):
                self.written_files.add(str(p))
        for w in self.raw.values():
            w.flush()

    def save_checkpoint(self) -> None:
        atomic_write_json(self.ckpt_path, {
            "instrument_id": self.symbol, "run_id": self.run_id,
            "saved_at": iso_utc(self.corrected(self.now_local_ms())),
            "last_agg_id": self.trades.last_id, "last_depth_u": self.book.last_update_id,
            "last_kline_open_ms": self.last_kline_open_ms,
            "cvd_start_ms": self.trades.cvd_start_ms, "cvd_base": str(self.trades.cvd_base),
        })

    def load_checkpoint(self) -> None:
        if not self.ckpt_path.exists():
            return
        ck = read_json(self.ckpt_path)
        self.trades.resume_from_checkpoint(ck.get("last_agg_id"), ck.get("cvd_start_ms"), ck.get("cvd_base"),
                                           f"restart from checkpoint of run {ck.get('run_id')}")
        self.last_kline_open_ms = ck.get("last_kline_open_ms")
        # the depth stream cannot be resumed: the book always resynchronises from a fresh snapshot
        self.quality("RESTART", f"resumed checkpoint saved_at={ck.get('saved_at')} last_agg_id={ck.get('last_agg_id')}")

    def status(self, final: bool = False) -> dict[str, Any]:
        try:
            free_gb = round(shutil.disk_usage(self.root).free / 2**30, 1)
        except OSError:
            free_gb = None
        return {
            "instrument": self.inst.model_dump(mode="json"), "run_id": self.run_id, "final": final,
            "updated_at": iso_utc(self.corrected(self.now_local_ms())),
            "started_at": iso_utc(self.corrected(self.started_local_ms)),
            "clock": {"offset_ms_server_minus_local": round(self.offset_ms, 1), "measured": self.offset_measured,
                      "rtt_ms": None if self.clock_rtt_ms is None else round(self.clock_rtt_ms, 1), "measured_at": self.clock_measured_at},
            "connections": self.conn, "message_counts": dict(self.counts), "last_message_at": self.last_msg,
            "book": self.book.status(), "trades": self.trades.status(),
            "snapshots": {"ok": self.snapshots_ok, "failed": self.snapshots_failed},
            "parse_errors": self.parse_errors, "disk_free_gb": free_gb,
            "raw_files_closed": [str(p) for w in self.raw.values() for p in w.closed_files][-20:],
            "quality_events": list(self.quality_log)[-40:],
        }

    def write_status(self, final: bool = False) -> None:
        atomic_write_json(self.status_path, self.status(final))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.flush()
        finally:
            self.save_checkpoint()
            for w in self.raw.values():
                w.close()
            self.write_status(final=True)

    def summary(self) -> dict[str, Any]:
        return {
            "message_counts": dict(self.counts), "book": self.book.status(), "trades": self.trades.status(),
            "snapshots": {"ok": self.snapshots_ok, "failed": self.snapshots_failed},
            "connections": {g: {k: v for k, v in c.items() if k in ("connects", "messages", "watchdog_reconnects", "errors")} for g, c in self.conn.items()},
            "clock_offset_ms_server_minus_local": round(self.offset_ms, 1),
            "status_file": str(self.status_path), "checkpoint_file": str(self.ckpt_path),
            "normalized_files": sorted(self.written_files),
            "raw_files": [str(p) for w in self.raw.values() for p in w.closed_files],
        }

    # ------------------------------------------------------------------ network
    def urls(self) -> dict[str, str]:
        b = self.cfg.binance
        speed = self.cfg.collector.depth_speed
        depth = f"{self.sym_l}@depth" + ("" if speed in ("", "250ms") else f"@{speed}")
        market = "/".join([f"{self.sym_l}@aggTrade", f"{self.sym_l}@markPrice@1s", f"{self.sym_l}@kline_5m"])
        return {GROUP_PUBLIC: f"{b.ws_public_base.rstrip('/')}/stream?streams={depth}",
                GROUP_MARKET: f"{b.ws_market_base.rstrip('/')}/stream?streams={market}"}

    async def _http_get(self, client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
        attempt = 0
        while True:
            try:
                r = await client.get(path, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                attempt += 1
                if attempt > self.cfg.binance.max_retries:
                    raise
                await asyncio.sleep(min(30.0, 0.5 * 2 ** attempt) * (0.5 + random.random()))
                self.quality("REST_RETRY", f"{path}: {type(exc).__name__}")
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code in (418, 429) or r.status_code >= 500:
                attempt += 1
                if attempt > self.cfg.binance.max_retries:
                    r.raise_for_status()
                ra = r.headers.get("Retry-After")
                delay = float(ra) if ra and ra.isdigit() else min(60.0, 1.0 * 2 ** attempt) * (0.5 + random.random())
                self.quality("REST_BACKOFF", f"{path}: HTTP {r.status_code}, sleeping {delay:.1f}s")
                await asyncio.sleep(delay)
                continue
            r.raise_for_status()

    async def measure_clock(self, client: httpx.AsyncClient) -> None:
        best = None
        for _ in range(3):
            t0 = time.time()
            data = await self._http_get(client, "/fapi/v1/time", {})
            t1 = time.time()
            off = data["serverTime"] - (t0 + t1) / 2 * 1000
            rtt = (t1 - t0) * 1000
            if best is None or rtt < best[1]:
                best = (off, rtt)
        assert best is not None
        prev = self.offset_ms if self.offset_measured else None
        self.set_clock(*best)
        self.quality("CLOCK", f"server-local offset {best[0]:.1f} ms (rtt {best[1]:.1f} ms)", previous_offset_ms=prev)

    async def _ws_loop(self, group: str, url: str) -> None:
        from websockets.asyncio.client import connect

        attempt = 0
        self.conn[group]["url"] = url
        while True:
            self.conn[group]["state"] = "CONNECTING"
            reason = "closed"
            try:
                async with connect(url, ping_interval=20, ping_timeout=20, open_timeout=15, close_timeout=5,
                                   max_size=2 ** 23, user_agent_header="aqe-research/0.1") as ws:
                    attempt = 0
                    c = self.conn[group]
                    c.update(state="CONNECTED", connects=c["connects"] + 1,
                             last_connect_at=iso_utc(self.corrected(self.now_local_ms())))
                    if group == GROUP_PUBLIC:
                        self._conn_gen += 1
                        self.book.on_connect()
                        self._drain_book_quality()
                    self.quality("WS_CONNECTED", group, url=url)
                    opened = time.monotonic()
                    while True:
                        if time.monotonic() - opened > MAX_CONNECTION_SECONDS:
                            reason = "proactive reconnect before 24h limit"
                            break
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=WATCHDOG_SECONDS)
                        except asyncio.TimeoutError:
                            c["watchdog_reconnects"] += 1
                            reason = f"watchdog: no data for {WATCHDOG_SECONDS:.0f}s"
                            break
                        local_ms = self.now_local_ms()
                        text = msg.decode() if isinstance(msg, (bytes, bytearray)) else msg
                        self.handle_text(group, text, local_ms)
            except asyncio.CancelledError:
                reason = "shutdown"
                raise
            except Exception as exc:  # noqa: BLE001 - any connection failure leads to reconnect
                self.conn[group]["errors"] += 1
                self.conn[group]["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                reason = f"error {type(exc).__name__}"
            finally:
                self.conn[group]["state"] = "DISCONNECTED"
                if group == GROUP_PUBLIC:
                    self.book.on_disconnect(reason)
                    self._drain_book_quality()
                self.quality("WS_DISCONNECTED", f"{group}: {reason}")
            attempt += 1
            delay = min(60.0, 0.5 * 2 ** attempt) * (0.5 + random.random())
            self.conn[group]["state"] = f"BACKOFF {delay:.1f}s"
            await asyncio.sleep(delay)

    async def _snapshot_loop(self, client: httpx.AsyncClient) -> None:
        while True:
            await asyncio.sleep(0.25)
            if not self.book.needs_snapshot():
                continue
            if time.monotonic() - self._last_snapshot_attempt < SNAPSHOT_MIN_INTERVAL_SECONDS:
                continue
            await asyncio.sleep(1.0)  # keep buffering so the snapshot falls inside the buffered range
            if not self.book.needs_snapshot():
                continue
            gen = self._conn_gen
            self.book.begin_snapshot()
            self._last_snapshot_attempt = time.monotonic()
            try:
                payload = await self._http_get(client, "/fapi/v1/depth",
                                               {"symbol": self.symbol, "limit": self.cfg.collector.snapshot_limit})
                local_ms = self.now_local_ms()
                if gen != self._conn_gen:
                    self.quality("SNAPSHOT_DISCARDED", "stream reconnected while fetching snapshot")
                    self.snapshots_failed += 1
                    continue
                self.apply_snapshot_payload(payload, local_ms)
            except Exception as exc:  # noqa: BLE001
                self.snapshots_failed += 1
                self.book.snapshot_failed(f"{type(exc).__name__}: {exc}"[:200])
                self._drain_book_quality()

    async def _oi_loop(self, client: httpx.AsyncClient) -> None:
        while True:
            try:
                payload = await self._http_get(client, "/fapi/v1/openInterest", {"symbol": self.symbol})
                self.on_oi_payload(payload)
            except Exception as exc:  # noqa: BLE001
                self.quality("OI_POLL_FAILED", f"{type(exc).__name__}: {exc}"[:200])
            await asyncio.sleep(self.cfg.collector.oi_poll_seconds)

    async def _clock_loop(self, client: httpx.AsyncClient) -> None:
        while True:
            await asyncio.sleep(self.cfg.collector.clock_check_seconds)
            try:
                await self.measure_clock(client)
            except Exception as exc:  # noqa: BLE001
                self.quality("CLOCK_FAILED", f"{type(exc).__name__}")

    async def _flow_loop(self) -> None:
        step = self.cfg.collector.flow_summary_seconds
        start_ms = (int(self.now_local_ms() + self.offset_ms) // (step * 1000)) * step * 1000
        while True:
            end_ms = start_ms + step * 1000
            await asyncio.sleep(max(0.0, (end_ms - (self.now_local_ms() + self.offset_ms)) / 1000))
            self.flow_tick(from_ms(start_ms), from_ms(end_ms))
            self.flush()
            self.save_checkpoint()
            start_ms = end_ms

    async def _status_loop(self) -> None:
        while True:
            self.write_status()
            await asyncio.sleep(self.cfg.collector.status_interval_seconds)

    async def run(self, duration_seconds: float | None) -> None:
        b = self.cfg.binance
        async with httpx.AsyncClient(base_url=b.rest_base_url, timeout=b.http_timeout_seconds,
                                     headers={"User-Agent": "aqe-research/0.1"}) as client:
            try:
                try:
                    await self.measure_clock(client)
                except Exception as exc:  # noqa: BLE001
                    self.quality("CLOCK_FAILED", f"initial clock measurement failed: {type(exc).__name__}")
                urls = self.urls()
                tasks = [
                    asyncio.create_task(self._ws_loop(GROUP_PUBLIC, urls[GROUP_PUBLIC])),
                    asyncio.create_task(self._ws_loop(GROUP_MARKET, urls[GROUP_MARKET])),
                    asyncio.create_task(self._snapshot_loop(client)),
                    asyncio.create_task(self._oi_loop(client)),
                    asyncio.create_task(self._clock_loop(client)),
                    asyncio.create_task(self._flow_loop()),
                    asyncio.create_task(self._status_loop()),
                ]
                try:
                    if duration_seconds is None:
                        await asyncio.gather(*tasks)
                    else:
                        done, _ = await asyncio.wait(tasks, timeout=duration_seconds, return_when=asyncio.FIRST_EXCEPTION)
                        for t in done:
                            if t.exception() is not None:
                                raise t.exception()
                finally:
                    # close the last partial flow interval (while the book is still connected) so its
                    # trades and book state are not lost, then stop the network tasks
                    try:
                        now_ms = int(self.now_local_ms() + self.offset_ms)
                        step = self.cfg.collector.flow_summary_seconds * 1000
                        self.flow_tick(from_ms((now_ms // step) * step), from_ms(now_ms))
                    finally:
                        for t in tasks:
                            t.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self.close()


def run_collect(cfg: LocalConfig, rec: RunRecord, duration_seconds: float | None = None, instrument: str | None = None) -> int:
    insts = [i for i in cfg.instruments if instrument is None or i.instrument_id == instrument.upper()]
    if not insts:
        raise ValueError(f"instrument {instrument} not configured")
    inst = insts[0]
    col = Collector(cfg, inst, rec.run_id)
    rec.inputs.update({"instrument": inst.slug(), "duration_seconds": duration_seconds, "urls": col.urls()})
    try:
        asyncio.run(col.run(duration_seconds))
    except KeyboardInterrupt:
        rec.notes.append("interrupted by user (Ctrl+C); buffers flushed")
        col.close()
    rec.outputs.update(col.summary())
    return EXIT_OK
