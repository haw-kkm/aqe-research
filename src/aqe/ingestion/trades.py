"""aggTrade normalisation, de-duplication, gap accounting and CVD (B05.3, T03).

Binance field `m` = "is the buyer the market maker?":
  m = true  -> the buyer was passive, the *seller* was the aggressor -> SELL, sign -1
  m = false -> the *buyer* was the aggressor                          -> BUY,  sign +1

Units: quantities are base units (BTC for BTCUSDT); quote = price * qty (USDT). CVD is the
cumulative signed *base* volume since an explicit start time. aggTrade is aggregated trade data
(same price/side within 100 ms), not every raw fill.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class AggTrade:
    symbol: str
    agg_id: int
    price: Decimal
    qty_base: Decimal
    first_trade_id: int
    last_trade_id: int
    trade_time_ms: int
    event_time_ms: int | None
    buyer_is_maker: bool

    @property
    def aggressor(self) -> str:
        return "SELL" if self.buyer_is_maker else "BUY"

    @property
    def sign(self) -> int:
        return -1 if self.buyer_is_maker else 1

    @property
    def quote(self) -> Decimal:
        return self.price * self.qty_base

    @classmethod
    def from_payload(cls, d: dict[str, Any], expected_symbol: str | None = None) -> "AggTrade":
        if d.get("e", "aggTrade") != "aggTrade":
            raise ValueError(f"not an aggTrade payload: e={d.get('e')!r}")
        sym = str(d["s"]).upper() if "s" in d else (expected_symbol or "").upper()
        if expected_symbol is not None and sym != expected_symbol.upper():
            raise ValueError(f"aggTrade for {sym} does not match expected instrument {expected_symbol}")
        qty = Decimal(str(d["q"]))
        price = Decimal(str(d["p"]))
        if qty <= 0 or price <= 0:
            raise ValueError("aggTrade price/qty must be positive")
        m = d["m"]
        if not isinstance(m, bool):
            raise ValueError("aggTrade field m must be boolean")
        return cls(symbol=sym, agg_id=int(d["a"]), price=price, qty_base=qty, first_trade_id=int(d["f"]),
                   last_trade_id=int(d["l"]), trade_time_ms=int(d["T"]), event_time_ms=int(d["E"]) if "E" in d else None,
                   buyer_is_maker=m)


@dataclass
class IntervalFlow:
    n_trades: int = 0
    buy_base: Decimal = Decimal(0)
    sell_base: Decimal = Decimal(0)
    buy_quote: Decimal = Decimal(0)
    sell_quote: Decimal = Decimal(0)
    missing: int = 0
    duplicates: int = 0
    late: int = 0
    first_trade_ms: int | None = None
    last_trade_ms: int | None = None

    @property
    def cvd_delta_base(self) -> Decimal:
        return self.buy_base - self.sell_base


@dataclass
class AggTradeTracker:
    symbol: str
    cvd_start_ms: int | None = None
    recent_window: int = 50000
    last_id: int | None = None
    dedup_floor: int | None = None  # after a restart: ids <= checkpointed last id were already processed
    cvd_base: Decimal = Decimal(0)
    accepted: int = 0
    duplicates: int = 0
    missing: int = 0  # ids skipped in the sequence (net of late arrivals)
    gaps: int = 0
    late: int = 0
    resets: list[dict[str, Any]] = field(default_factory=list)
    _recent: deque = field(default_factory=deque)
    _recent_set: set = field(default_factory=set)
    _interval: IntervalFlow = field(default_factory=IntervalFlow)

    def __post_init__(self) -> None:
        self.symbol = self.symbol.upper()

    def _remember(self, agg_id: int) -> None:
        self._recent.append(agg_id)
        self._recent_set.add(agg_id)
        while len(self._recent) > self.recent_window:
            self._recent_set.discard(self._recent.popleft())

    def reset_cvd(self, at_ms: int, reason: str) -> None:
        self.resets.append({"at_ms": at_ms, "reason": reason, "cvd_before": str(self.cvd_base)})
        self.cvd_base = Decimal(0)
        self.cvd_start_ms = at_ms

    def resume_from_checkpoint(self, last_id: int | None, cvd_start_ms: int | None, cvd_base: str | None, reason: str) -> None:
        """After a restart: continue id-gap accounting from the checkpoint. Trades missed while down
        are counted as missing when the next id arrives; CVD continuity is NOT assumed (reset)."""
        self.last_id = last_id
        self.dedup_floor = last_id
        self.resets.append({"at_ms": cvd_start_ms, "reason": reason, "cvd_before": cvd_base})
        self.cvd_base = Decimal(0)
        self.cvd_start_ms = None

    def on_trade(self, t: AggTrade) -> bool:
        """Returns True if accepted, False if dropped as a duplicate."""
        if t.symbol != self.symbol:
            raise ValueError(f"aggTrade for {t.symbol} fed to {self.symbol} tracker")
        if (t.agg_id in self._recent_set or (self.last_id is not None and t.agg_id == self.last_id)
                or (self.dedup_floor is not None and t.agg_id <= self.dedup_floor)):
            self.duplicates += 1
            self._interval.duplicates += 1
            return False
        if self.last_id is not None:
            if t.agg_id > self.last_id + 1:
                gap = t.agg_id - self.last_id - 1
                self.missing += gap
                self.gaps += 1
                self._interval.missing += gap
            elif t.agg_id < self.last_id:
                # late arrival of an id previously counted as missing
                self.late += 1
                self._interval.late += 1
                if self.missing > 0:
                    self.missing -= 1
        if self.cvd_start_ms is None:
            self.cvd_start_ms = t.trade_time_ms
        self._remember(t.agg_id)
        self.last_id = t.agg_id if self.last_id is None else max(self.last_id, t.agg_id)
        self.accepted += 1
        self.cvd_base += t.sign * t.qty_base
        iv = self._interval
        iv.n_trades += 1
        if t.sign > 0:
            iv.buy_base += t.qty_base
            iv.buy_quote += t.quote
        else:
            iv.sell_base += t.qty_base
            iv.sell_quote += t.quote
        iv.first_trade_ms = t.trade_time_ms if iv.first_trade_ms is None else min(iv.first_trade_ms, t.trade_time_ms)
        iv.last_trade_ms = t.trade_time_ms if iv.last_trade_ms is None else max(iv.last_trade_ms, t.trade_time_ms)
        return True

    def pop_interval(self) -> IntervalFlow:
        iv = self._interval
        self._interval = IntervalFlow()
        return iv

    def status(self) -> dict[str, Any]:
        return {"last_id": self.last_id, "accepted": self.accepted, "duplicates": self.duplicates, "missing": self.missing,
                "gaps": self.gaps, "late": self.late, "cvd_base": str(self.cvd_base), "cvd_start_ms": self.cvd_start_ms,
                "resets": self.resets[-10:]}
