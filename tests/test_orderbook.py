"""T02: USDⓈ-M local order book never reports a valid state across gaps, duplicates,
out-of-order events or reconnects."""

from decimal import Decimal

import pytest

from aqe.ingestion.orderbook import BookState, DepthEvent, UsdmOrderBook


def ev(U, u, pu, bids=(), asks=(), sym="BTCUSDT", E=0):
    return DepthEvent.from_payload({
        "e": "depthUpdate", "E": E or u, "T": E or u, "s": sym, "U": U, "u": u, "pu": pu,
        "b": [[str(p), str(q)] for p, q in bids], "a": [[str(p), str(q)] for p, q in asks],
    })


SNAP_BIDS = [["100.0", "1.0"], ["99.0", "2.0"], ["98.0", "3.0"]]
SNAP_ASKS = [["101.0", "1.5"], ["102.0", "2.5"]]


def synced_book() -> UsdmOrderBook:
    b = UsdmOrderBook("BTCUSDT")
    b.on_connect()
    b.on_event(ev(90, 95, 89))            # stale: u < L -> dropped
    b.on_event(ev(96, 105, 95, bids=[(100.0, 1.2)]))  # brackets L=100
    assert b.needs_snapshot()
    assert b.apply_snapshot(100, SNAP_BIDS, SNAP_ASKS) is True
    return b


def test_snapshot_first_event_rule_and_stale_drop():
    b = synced_book()
    assert b.state == BookState.SYNCED and b.is_valid
    assert b.stale_dropped == 1
    assert b.last_update_id == 105
    assert b.bids[Decimal("100.0")] == Decimal("1.2")  # absolute quantity replaced, not added


def test_not_valid_before_snapshot():
    b = UsdmOrderBook("BTCUSDT")
    assert b.state == BookState.DISCONNECTED and not b.is_valid
    b.on_connect()
    b.on_event(ev(1, 5, 0))
    assert b.state == BookState.BUFFERING and not b.is_valid
    assert b.metrics() is None


def test_first_event_after_snapshot_gap_resyncs():
    b = UsdmOrderBook("BTCUSDT")
    b.on_connect()
    b.on_event(ev(150, 160, 149))  # U > L: snapshot too old
    assert b.apply_snapshot(100, SNAP_BIDS, SNAP_ASKS) is False
    assert b.state == BookState.BUFFERING and not b.is_valid
    assert b.desync_count == 1
    assert b.needs_snapshot()  # the event was re-buffered for the next snapshot
    assert b.apply_snapshot(155, SNAP_BIDS, SNAP_ASKS) is True
    assert b.last_update_id == 160


def test_snapshot_newer_than_all_buffered_events_waits_for_bracketing_event():
    b = UsdmOrderBook("BTCUSDT")
    b.on_connect()
    b.on_event(ev(10, 20, 9))
    assert b.apply_snapshot(100, SNAP_BIDS, SNAP_ASKS) is False
    assert b.state == BookState.SNAPSHOT_SYNC and not b.is_valid
    b.on_event(ev(95, 99, 94))  # still stale
    assert not b.is_valid
    b.on_event(ev(99, 102, 98))  # U <= 100 <= u
    assert b.is_valid and b.last_update_id == 102


def test_pu_gap_invalidates_immediately_and_resyncs():
    b = synced_book()
    b.on_event(ev(106, 110, 105, asks=[(101.0, 0)]))
    assert b.is_valid
    b.on_event(ev(115, 120, 112))  # pu != previous u (110): missed event
    assert not b.is_valid
    assert b.state == BookState.BUFFERING
    assert b.desync_count == 1
    assert b.metrics() is None
    assert len(b.bids) == 0 and len(b.asks) == 0  # stale book discarded


def test_out_of_order_event_desyncs():
    b = synced_book()
    b.on_event(ev(111, 115, 110))  # arrives before (106..110): pu 110 != 105
    assert not b.is_valid and b.desync_count == 1
    b.on_event(ev(106, 110, 105))  # late event just buffered
    assert b.state == BookState.BUFFERING


def test_duplicate_event_is_dropped_not_reapplied():
    b = synced_book()
    e1 = ev(106, 110, 105, bids=[(97.0, 4.0)])
    b.on_event(e1)
    b.on_event(ev(111, 115, 110, bids=[(97.0, 0)]))  # level removed
    b.on_event(e1)  # duplicate delivery must not resurrect the level
    assert b.is_valid
    assert Decimal("97.0") not in b.bids
    assert b.duplicate_dropped == 1


def test_zero_qty_removes_and_missing_level_removal_is_normal():
    b = synced_book()
    b.on_event(ev(106, 110, 105, bids=[(99.0, 0), (50.0, 0)], asks=[(102.0, 0)]))
    assert b.is_valid
    assert Decimal("99.0") not in b.bids and Decimal("102.0") not in b.asks
    assert b.desync_count == 0


def test_reconnect_returns_to_buffering_and_hides_features():
    b = synced_book()
    assert b.metrics() is not None
    b.on_disconnect("network")
    assert b.state == BookState.DISCONNECTED and b.metrics() is None
    b.on_connect()
    assert b.state == BookState.BUFFERING and not b.is_valid
    b.on_event(ev(200, 210, 199))
    assert b.metrics() is None
    assert b.apply_snapshot(205, SNAP_BIDS, SNAP_ASKS) is True
    assert b.resync_count == 2


def test_metrics_and_crossed_book():
    b = synced_book()
    m = b.metrics(depth_bps=(10.0,), top_n=2)
    assert m is not None
    assert m.best_bid == Decimal("100.0") and m.best_ask == Decimal("101.0")
    assert m.spread_bps == pytest.approx(1.0 / 100.5 * 1e4)
    # within 10bps of mid 100.5 -> [100.3995, 100.6005]: no levels qualify
    assert m.depth_quote["bid_10bps"] == 0.0
    bq, aq = Decimal("1.2") + Decimal("2.0"), Decimal("1.5") + Decimal("2.5")
    assert m.imbalance_top_n == pytest.approx(float((bq - aq) / (bq + aq)))
    b.on_event(ev(106, 110, 105, bids=[(101.5, 1.0)]))  # bid above best ask -> corrupt
    assert b.metrics() is None and not b.is_valid


def test_symbol_mismatch_and_spot_payload_rejected():
    b = UsdmOrderBook("BTCUSDT")
    b.on_connect()
    with pytest.raises(ValueError):
        b.on_event(ev(1, 2, 0, sym="ETHUSDT"))
    with pytest.raises(ValueError):
        DepthEvent.from_payload({"e": "depthUpdate", "E": 1, "s": "BTCUSDT", "U": 1, "u": 2, "b": [], "a": []})  # no pu


def test_replay_of_recorded_sequence_with_gap_and_recovery():
    """Replay: the same logic used live, fed a recorded sequence containing a gap."""
    seq = [("event", ev(1, 10, 0)), ("event", ev(11, 20, 10)), ("snapshot", 15),
           ("event", ev(21, 30, 20)), ("event", ev(41, 50, 40)),  # gap (31..40 missing)
           ("event", ev(51, 60, 50)), ("snapshot", 55), ("event", ev(61, 70, 60))]
    b = UsdmOrderBook("BTCUSDT")
    b.on_connect()
    valid_trace = []
    for kind, x in seq:
        if kind == "event":
            b.on_event(x)
        else:
            b.apply_snapshot(x, SNAP_BIDS, SNAP_ASKS)
        valid_trace.append(b.is_valid)
    assert valid_trace == [False, False, True, True, False, False, True, True]
    assert b.desync_count == 1 and b.resync_count == 2
    assert b.last_update_id == 70
