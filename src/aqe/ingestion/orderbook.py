"""USDⓈ-M futures local order book synchronisation (B05.3, T02). Pure, IO-free.

Implements the official *USDⓈ-M* procedure ("How to manage a local order book correctly"):

1. buffer diff-depth events, fetch a REST snapshot (lastUpdateId = L)
2. drop buffered events with u < L
3. the first applied event must satisfy U <= L <= u, otherwise re-snapshot
4. every later event's pu must equal the previous event's u, otherwise the book is invalid -> resync
5. quantities are absolute; qty 0 removes the level; removing a missing level is normal

Spot rules (U == previous u + 1) are deliberately NOT used.

States: DISCONNECTED -> BUFFERING -> SNAPSHOT_SYNC -> SYNCED ; on a gap SYNCED -> DESYNCED -> BUFFERING.
Book metrics are only produced while SYNCED (`is_valid`).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable


class BookState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    BUFFERING = "BUFFERING"
    SNAPSHOT_SYNC = "SNAPSHOT_SYNC"
    SYNCED = "SYNCED"
    DESYNCED = "DESYNCED"


Level = tuple[Decimal, Decimal]


def _levels(raw: Iterable[Iterable[Any]]) -> list[Level]:
    out = []
    for p, q, *_ in raw:
        qty = Decimal(str(q))
        if qty < 0:
            raise ValueError(f"negative quantity {q}")
        out.append((Decimal(str(p)), qty))
    return out


@dataclass(frozen=True)
class DepthEvent:
    symbol: str
    event_time_ms: int
    transaction_time_ms: int | None
    first_update_id: int  # U
    final_update_id: int  # u
    prev_final_update_id: int  # pu
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]

    @classmethod
    def from_payload(cls, d: dict[str, Any]) -> "DepthEvent":
        if d.get("e") != "depthUpdate":
            raise ValueError(f"not a depthUpdate payload: e={d.get('e')!r}")
        for k in ("U", "u", "pu"):
            if k not in d:
                raise ValueError(f"USDⓈ-M depth payload missing '{k}' (Spot payload?)")
        return cls(
            symbol=str(d["s"]).upper(),
            event_time_ms=int(d["E"]),
            transaction_time_ms=int(d["T"]) if "T" in d else None,
            first_update_id=int(d["U"]),
            final_update_id=int(d["u"]),
            prev_final_update_id=int(d["pu"]),
            bids=tuple(_levels(d.get("b", []))),
            asks=tuple(_levels(d.get("a", []))),
        )


@dataclass(frozen=True)
class QualityEvent:
    kind: str  # STATE_TRANSITION | DROPPED_STALE | DROPPED_DUPLICATE | BUFFER_OVERFLOW
    from_state: str | None
    to_state: str | None
    reason: str
    event_time_ms: int | None = None
    update_id: int | None = None


@dataclass
class BookMetrics:
    best_bid: Decimal
    best_ask: Decimal
    mid: Decimal
    spread_bps: float
    depth_quote: dict[str, float]  # e.g. {"bid_10bps": .., "ask_10bps": ..}
    imbalance_top_n: float  # (bid_qty - ask_qty) / (bid_qty + ask_qty) over top N levels
    n_bid_levels: int
    n_ask_levels: int
    last_update_id: int


@dataclass
class UsdmOrderBook:
    symbol: str
    max_buffer: int = 20000
    max_quality_events: int = 1000
    state: BookState = BookState.DISCONNECTED
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    last_update_id: int | None = None  # u of the last applied event (or snapshot L before first event)
    snapshot_update_id: int | None = None
    desync_count: int = 0
    resync_count: int = 0  # number of times SYNCED was (re)reached
    stale_dropped: int = 0
    duplicate_dropped: int = 0
    buffer_overflow_dropped: int = 0
    events_applied: int = 0
    _buffer: deque = field(default_factory=deque)
    _snapshot_applied: bool = False
    quality_events: deque = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.symbol = self.symbol.upper()

    # ------------------------------------------------------------------ helpers
    @property
    def is_valid(self) -> bool:
        return self.state == BookState.SYNCED

    def _note(self, q: QualityEvent) -> None:
        self.quality_events.append(q)
        while len(self.quality_events) > self.max_quality_events:
            self.quality_events.popleft()

    def drain_quality_events(self) -> list[QualityEvent]:
        out = list(self.quality_events)
        self.quality_events.clear()
        return out

    def _to(self, new: BookState, reason: str, ev: DepthEvent | None = None) -> None:
        if new == self.state:
            return
        self._note(QualityEvent("STATE_TRANSITION", self.state.value, new.value, reason,
                                ev.event_time_ms if ev else None, ev.final_update_id if ev else None))
        self.state = new
        if new == BookState.SYNCED:
            self.resync_count += 1

    def _clear_book(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_update_id = None
        self.snapshot_update_id = None
        self._snapshot_applied = False

    def _buffer_event(self, ev: DepthEvent) -> None:
        self._buffer.append(ev)
        while len(self._buffer) > self.max_buffer:
            self._buffer.popleft()
            self.buffer_overflow_dropped += 1

    def _apply(self, ev: DepthEvent) -> None:
        for side, levels in ((self.bids, ev.bids), (self.asks, ev.asks)):
            for price, qty in levels:
                if qty == 0:
                    side.pop(price, None)  # removing an unknown level is normal
                else:
                    side[price] = qty
        self.last_update_id = ev.final_update_id
        self.events_applied += 1

    def _desync(self, reason: str, ev: DepthEvent | None, rebuffer: list[DepthEvent]) -> None:
        self.desync_count += 1
        self._to(BookState.DESYNCED, reason, ev)
        self._clear_book()
        self._buffer.clear()
        self._to(BookState.BUFFERING, "recovering: waiting for new snapshot", ev)
        for e in rebuffer:
            self._buffer_event(e)

    # ------------------------------------------------------------------ connection lifecycle
    def on_connect(self) -> None:
        """A (re)connected stream: everything prior is unusable until resynchronised."""
        self._clear_book()
        self._buffer.clear()
        self._to(BookState.BUFFERING, "stream connected; buffering before snapshot")

    def on_disconnect(self, reason: str) -> None:
        self._clear_book()
        self._buffer.clear()
        self._to(BookState.DISCONNECTED, f"disconnected: {reason}")

    def needs_snapshot(self) -> bool:
        return self.state == BookState.BUFFERING and len(self._buffer) > 0

    def begin_snapshot(self) -> None:
        if self.state != BookState.BUFFERING:
            raise RuntimeError(f"begin_snapshot in state {self.state}")
        self._snapshot_applied = False
        self._to(BookState.SNAPSHOT_SYNC, "fetching REST snapshot")

    def snapshot_failed(self, reason: str) -> None:
        if self.state == BookState.SNAPSHOT_SYNC and not self._snapshot_applied:
            self._to(BookState.BUFFERING, f"snapshot failed: {reason}")

    # ------------------------------------------------------------------ data path
    def on_event(self, ev: DepthEvent) -> None:
        if ev.symbol != self.symbol:
            raise ValueError(f"depth event for {ev.symbol} fed to {self.symbol} book")
        if self.state == BookState.DISCONNECTED:
            self._to(BookState.BUFFERING, "event received while disconnected (implicit connect)", ev)
        if self.state in (BookState.BUFFERING, BookState.DESYNCED):
            self._buffer_event(ev)
            return
        if self.state == BookState.SNAPSHOT_SYNC and not self._snapshot_applied:
            self._buffer_event(ev)
            return
        self._process(ev, [])

    def apply_snapshot(self, last_update_id: int, bids: Iterable, asks: Iterable) -> bool:
        """Apply a REST snapshot (GET /fapi/v1/depth). Returns True if the book ends SYNCED."""
        if self.state == BookState.BUFFERING:
            self.begin_snapshot()
        if self.state != BookState.SNAPSHOT_SYNC:
            return False
        L = int(last_update_id)
        self.bids = {p: q for p, q in _levels(bids) if q != 0}
        self.asks = {p: q for p, q in _levels(asks) if q != 0}
        self.snapshot_update_id = L
        self.last_update_id = L
        self._snapshot_applied = True
        pending = list(self._buffer)
        self._buffer.clear()
        for i, ev in enumerate(pending):
            if not self._process(ev, pending[i + 1:]):
                return False
        return self.state == BookState.SYNCED

    def _process(self, ev: DepthEvent, remaining: list[DepthEvent]) -> bool:
        L = self.snapshot_update_id
        if self.state == BookState.SNAPSHOT_SYNC:
            assert L is not None
            if ev.final_update_id < L:
                self.stale_dropped += 1
                return True
            if ev.first_update_id <= L <= ev.final_update_id:
                self._apply(ev)
                self._to(BookState.SYNCED, f"first event U={ev.first_update_id} <= L={L} <= u={ev.final_update_id}", ev)
                return True
            # U > L: events start after the snapshot -> snapshot too old, a gap exists
            self._desync(f"first event U={ev.first_update_id} > snapshot L={L}: snapshot too old", ev, [ev, *remaining])
            return False
        if self.state == BookState.SYNCED:
            assert self.last_update_id is not None
            if ev.final_update_id <= self.last_update_id:
                # already covered by the continuous pu-chain: a duplicate/late copy; never re-applied
                self.duplicate_dropped += 1
                self._note(QualityEvent("DROPPED_DUPLICATE", None, None,
                                        f"u={ev.final_update_id} <= last u={self.last_update_id}",
                                        ev.event_time_ms, ev.final_update_id))
                return True
            if ev.prev_final_update_id != self.last_update_id:
                self._desync(f"pu={ev.prev_final_update_id} != previous u={self.last_update_id} (gap/out-of-order)",
                             ev, [ev, *remaining])
                return False
            self._apply(ev)
            return True
        # BUFFERING / DESYNCED (e.g. after a desync inside apply_snapshot)
        self._buffer_event(ev)
        return False

    # ------------------------------------------------------------------ metrics
    def metrics(self, depth_bps: Iterable[float] = (10.0, 25.0), top_n: int = 10) -> BookMetrics | None:
        """Point-in-time book metrics, or None unless SYNCED. A crossed book forces a resync."""
        if not self.is_valid or not self.bids or not self.asks:
            return None
        best_bid = max(self.bids)
        best_ask = min(self.asks)
        if best_bid >= best_ask:
            self._desync(f"crossed book bid={best_bid} >= ask={best_ask}", None, [])
            return None
        mid = (best_bid + best_ask) / 2
        spread_bps = float((best_ask - best_bid) / mid * Decimal(10000))
        depth: dict[str, float] = {}
        for bps in depth_bps:
            lo = mid * (Decimal(1) - Decimal(str(bps)) / Decimal(10000))
            hi = mid * (Decimal(1) + Decimal(str(bps)) / Decimal(10000))
            depth[f"bid_{bps:g}bps"] = float(sum(p * q for p, q in self.bids.items() if p >= lo))
            depth[f"ask_{bps:g}bps"] = float(sum(p * q for p, q in self.asks.items() if p <= hi))
        top_b = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[:top_n]
        top_a = sorted(self.asks.items(), key=lambda x: x[0])[:top_n]
        bq = sum(q for _, q in top_b)
        aq = sum(q for _, q in top_a)
        imb = float((bq - aq) / (bq + aq)) if (bq + aq) > 0 else 0.0
        return BookMetrics(best_bid=best_bid, best_ask=best_ask, mid=mid, spread_bps=spread_bps, depth_quote=depth,
                           imbalance_top_n=imb, n_bid_levels=len(self.bids), n_ask_levels=len(self.asks),
                           last_update_id=int(self.last_update_id or 0))

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state.value, "is_valid": self.is_valid, "last_update_id": self.last_update_id,
            "snapshot_update_id": self.snapshot_update_id, "desync_count": self.desync_count,
            "resync_count": self.resync_count, "stale_dropped": self.stale_dropped,
            "duplicate_dropped": self.duplicate_dropped, "buffer_overflow_dropped": self.buffer_overflow_dropped,
            "buffered": len(self._buffer), "events_applied": self.events_applied,
            "n_bid_levels": len(self.bids), "n_ask_levels": len(self.asks),
        }
