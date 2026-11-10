"""Offline collector message handling (no network): raw logging, book sync, flow summaries,
live-bar storage, checkpoint + restart de-duplication (T02/T03/T13 partial)."""

import json
from datetime import datetime, timezone

import pandas as pd

from aqe.config import LocalConfig
from aqe.contracts.core import InstrumentKey, from_ms, iso_utc
from aqe.ingestion.collector import GROUP_MARKET, GROUP_PUBLIC, LIVE_BAR_INTERVAL_KEY, Collector
from aqe.storage import iter_raw_log
from aqe.storage.bars import read_bars

INST = InstrumentKey(venue="binance", market_type="usdm_perpetual", instrument_id="BTCUSDT")


def make_cfg(tmp_path) -> LocalConfig:
    return LocalConfig.model_validate({
        "paths": {"data_root": str(tmp_path / "data"), "artifact_root": str(tmp_path / "artifacts")},
        "instruments": [INST.model_dump(mode="json")],
        "backfill": {"start": "2026-01-01T00:00:00Z"},
        "risk_ruleset": "configs/risk_rules_v1.yaml",
        "analysis": {"experiment_config": "configs/experiment.yaml"},
    })


def wrap(stream, data):
    return json.dumps({"stream": stream, "data": data})


def depth(U, u, pu, bids=(), asks=()):
    return wrap("btcusdt@depth@100ms", {"e": "depthUpdate", "E": u, "T": u, "s": "BTCUSDT", "U": U, "u": u, "pu": pu,
                                         "b": [[str(p), str(q)] for p, q in bids], "a": [[str(p), str(q)] for p, q in asks]})


def agg(a, m=False, q="0.25", p="70000"):
    return wrap("btcusdt@aggTrade", {"e": "aggTrade", "E": 1, "s": "BTCUSDT", "a": a, "p": p, "q": q, "f": a, "l": a, "T": 1_000 + a, "m": m})


def kline(t_ms, closed=True):
    return wrap("btcusdt@kline_5m", {"e": "kline", "E": t_ms + 300_000, "s": "BTCUSDT", "k": {
        "t": t_ms, "T": t_ms + 299_999, "s": "BTCUSDT", "i": "5m", "o": "70000", "c": "70100", "h": "70200", "l": "69900",
        "v": "12.5", "n": 900, "x": closed, "q": "876000", "V": "6.0", "Q": "420000", "B": "0"}})


def mark():
    return wrap("btcusdt@markPrice@1s", {"e": "markPriceUpdate", "E": 1_794_000_000_000, "s": "BTCUSDT", "p": "70010.1",
                                          "i": "70005.2", "P": "70001.0", "r": "0.00010000", "T": 1_794_009_600_000})


def test_message_flow_and_persistence(tmp_path):
    cfg = make_cfg(tmp_path)
    col = Collector(cfg, INST, "run-test-1")
    col.set_clock(offset_ms=-62_000.0, rtt_ms=20.0)  # local clock 62 s ahead of exchange
    col.book.on_connect()
    col.handle_text(GROUP_PUBLIC, depth(90, 99, 89), local_ms=1_794_000_062_000)
    col.handle_text(GROUP_PUBLIC, depth(100, 110, 99, bids=[(70000, 1)], asks=[(70001, 2)]))
    assert col.book.needs_snapshot()
    col.book.begin_snapshot()
    ok = col.apply_snapshot_payload({"lastUpdateId": 105, "bids": [["70000", "0.5"], ["69999", "3"]], "asks": [["70001", "1"]]})
    assert ok and col.book.is_valid
    col.handle_text(GROUP_MARKET, agg(10, m=False, q="1"))
    col.handle_text(GROUP_MARKET, agg(11, m=True, q="0.25"))
    col.handle_text(GROUP_MARKET, agg(11, m=True, q="0.25"))  # duplicate
    col.handle_text(GROUP_MARKET, mark())
    col.handle_text(GROUP_MARKET, kline(1_794_000_000_000, closed=False))  # open candle ignored
    col.handle_text(GROUP_MARKET, kline(1_794_000_000_000, closed=True))
    col.on_oi_payload({"symbol": "BTCUSDT", "openInterest": "100363.492", "time": 1_794_000_001_000})
    row = col.flow_tick(datetime(2026, 11, 10, 19, 0, tzinfo=timezone.utc), datetime(2026, 11, 10, 19, 1, tzinfo=timezone.utc))
    assert row["book_valid"] and row["spread_bps"] > 0
    assert row["n_trades"] == 2 and row["cvd_delta_base"] == 0.75 and row["agg_duplicates"] == 1
    col.close()

    st = json.loads(col.status_path.read_text(encoding="utf-8"))
    assert st["final"] and st["message_counts"]["depth"] == 2 and st["message_counts"]["aggTrade"] == 3
    assert st["book"]["state"] == "SYNCED"
    # raw log keeps original text, local clock and offset-corrected received_at
    raw_files = [p for p in col.raw[GROUP_MARKET].closed_files]
    recs = [r for f in raw_files for r in iter_raw_log(f)]
    assert len(recs) == 6 and all("raw" in r and "offset_ms" in r for r in recs)
    pub = [r for f in col.raw[GROUP_PUBLIC].closed_files for r in iter_raw_log(f)]
    assert pub[0]["local_ms"] == 1_794_000_062_000 and pub[0]["received_at"] == iso_utc(from_ms(1_794_000_000_000))
    bars = read_bars(cfg.paths.data_root, INST, LIVE_BAR_INTERVAL_KEY)
    assert len(bars) == 1 and bars["provenance"].iloc[0] == "LIVE_CAPTURE"
    assert read_bars(cfg.paths.data_root, INST, "5m").empty  # backfill partition untouched
    marks = pd.concat([pd.read_parquet(p) for p in col.mark_dir.rglob("*.parquet")])
    assert marks["kind"].iloc[0] == "ESTIMATED" and marks["funding_rate_est"].iloc[0] == 0.0001
    oi = pd.concat([pd.read_parquet(p) for p in col.oi_dir.rglob("*.parquet")])
    assert oi["kind"].iloc[0] == "CURRENT_OBSERVATION" and len(oi) == 1
    flows = pd.concat([pd.read_parquet(p) for p in col.flow_dir.rglob("*.parquet")])
    assert len(flows) == 1


def test_restart_is_idempotent(tmp_path):
    cfg = make_cfg(tmp_path)
    a = Collector(cfg, INST, "run-a")
    for i in (100, 101, 102):
        a.handle_text(GROUP_MARKET, agg(i))
    a.handle_text(GROUP_MARKET, kline(1_794_000_000_000))
    a.close()
    ck = json.loads(a.ckpt_path.read_text(encoding="utf-8"))
    assert ck["last_agg_id"] == 102 and ck["last_kline_open_ms"] == 1_794_000_000_000

    b = Collector(cfg, INST, "run-b")  # restart: same data root
    assert b.trades.last_id == 102
    b.handle_text(GROUP_MARKET, agg(101))  # re-delivered after restart -> duplicate
    b.handle_text(GROUP_MARKET, agg(102))
    b.handle_text(GROUP_MARKET, agg(105))  # 103,104 missed while down
    b.handle_text(GROUP_MARKET, kline(1_794_000_000_000))  # same candle re-delivered
    assert b.trades.duplicates == 2 and b.trades.missing == 2 and b.trades.accepted == 1
    assert b.book.state.value == "DISCONNECTED" and not b.book.is_valid  # book never resumes without resync
    b.close()
    bars = read_bars(cfg.paths.data_root, INST, LIVE_BAR_INTERVAL_KEY)
    assert len(bars) == 1  # upsert by open_time, no duplicate candle
    st = json.loads(b.status_path.read_text(encoding="utf-8"))
    assert any(q["kind"] == "RESTART" for q in st["quality_events"])


def test_wrong_symbol_payload_is_quality_event_not_crash(tmp_path):
    col = Collector(make_cfg(tmp_path), INST, "run-x")
    bad = wrap("ethusdt@aggTrade", {"e": "aggTrade", "E": 1, "s": "ETHUSDT", "a": 1, "p": "1", "q": "1", "f": 1, "l": 1, "T": 1, "m": False})
    col.handle_text(GROUP_MARKET, bad)
    col.handle_text(GROUP_MARKET, "not json")
    assert col.parse_errors == 2 and col.trades.accepted == 0
    kinds = [q["kind"] for q in col.quality_log]
    assert "PAYLOAD_ERROR" in kinds and "PARSE_ERROR" in kinds
    col.close()


def test_urls_use_public_and_market_paths(tmp_path):
    col = Collector(make_cfg(tmp_path), INST, "run-u")
    u = col.urls()
    assert u["public"] == "wss://fstream.binance.com/public/stream?streams=btcusdt@depth@100ms"
    assert u["market"].startswith("wss://fstream.binance.com/market/stream?streams=")
    assert "btcusdt@aggTrade" in u["market"] and "btcusdt@markPrice@1s" in u["market"] and "btcusdt@kline_5m" in u["market"]
    col.close()
