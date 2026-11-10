"""T03: aggTrade aggressor direction, de-duplication, gap accounting, CVD units, instrument checks."""

from decimal import Decimal

import pytest

from aqe.ingestion.trades import AggTrade, AggTradeTracker


def tr(a, q="0.5", p="100", m=False, s="BTCUSDT", T=None):
    return AggTrade.from_payload({"e": "aggTrade", "E": 1, "s": s, "a": a, "p": p, "q": q, "f": a * 10, "l": a * 10 + 1,
                                  "T": T or 1_000 + a, "m": m}, "BTCUSDT")


def test_buyer_maker_true_is_sell_aggressor():
    sell = tr(1, m=True)
    buy = tr(2, m=False)
    assert sell.aggressor == "SELL" and sell.sign == -1
    assert buy.aggressor == "BUY" and buy.sign == 1


def test_cvd_units_base_and_quote():
    t = AggTradeTracker("BTCUSDT")
    t.on_trade(tr(1, q="2", p="100", m=False))   # +2 BTC, 200 USDT bought
    t.on_trade(tr(2, q="0.5", p="102", m=True))  # -0.5 BTC, 51 USDT sold
    assert t.cvd_base == Decimal("1.5")
    iv = t.pop_interval()
    assert iv.buy_base == Decimal("2") and iv.sell_base == Decimal("0.5")
    assert iv.buy_quote == Decimal("200") and iv.sell_quote == Decimal("51.0")
    assert iv.cvd_delta_base == Decimal("1.5") and iv.n_trades == 2
    assert t.pop_interval().n_trades == 0  # interval reset, cumulative CVD kept
    assert t.cvd_base == Decimal("1.5")
    assert t.cvd_start_ms == 1_001


def test_duplicate_trade_id_not_double_counted():
    t = AggTradeTracker("BTCUSDT")
    assert t.on_trade(tr(10, q="1"))
    assert not t.on_trade(tr(10, q="1"))
    assert t.cvd_base == Decimal("1") and t.duplicates == 1 and t.accepted == 1


def test_gap_counting_and_late_arrival():
    t = AggTradeTracker("BTCUSDT")
    t.on_trade(tr(100))
    t.on_trade(tr(104))  # 101..103 missing
    assert t.missing == 3 and t.gaps == 1
    t.on_trade(tr(102))  # late arrival reduces missing
    assert t.missing == 2 and t.late == 1
    assert t.last_id == 104
    iv = t.pop_interval()
    assert iv.missing == 3 and iv.late == 1


def test_cvd_reset_is_recorded():
    t = AggTradeTracker("BTCUSDT")
    t.on_trade(tr(1, q="3"))
    t.reset_cvd(5_000, "operator reset")
    assert t.cvd_base == 0 and t.cvd_start_ms == 5_000
    assert t.resets[-1]["reason"] == "operator reset" and t.resets[-1]["cvd_before"] == "3"


def test_restart_floor_dedups_already_processed_ids():
    t = AggTradeTracker("BTCUSDT")
    t.resume_from_checkpoint(last_id=500, cvd_start_ms=1, cvd_base="4", reason="restart")
    assert not t.on_trade(tr(499))
    assert not t.on_trade(tr(500))
    assert t.on_trade(tr(505))
    assert t.missing == 4  # 501..504 were not captured while down
    assert t.cvd_base == Decimal("0.5")  # CVD continuity across downtime is not assumed


def test_instrument_mismatch_rejected():
    with pytest.raises(ValueError):
        tr(1, s="ETHUSDT")
    t = AggTradeTracker("ETHUSDT")
    with pytest.raises(ValueError):
        t.on_trade(tr(1))


def test_invalid_payloads_rejected():
    with pytest.raises(ValueError):
        AggTrade.from_payload({"e": "aggTrade", "s": "BTCUSDT", "a": 1, "p": "1", "q": "0", "f": 1, "l": 1, "T": 1, "m": True})
    with pytest.raises(ValueError):
        AggTrade.from_payload({"e": "aggTrade", "s": "BTCUSDT", "a": 1, "p": "1", "q": "1", "f": 1, "l": 1, "T": 1, "m": "true"})
