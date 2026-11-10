"""Cost-aware virtual trade accounting (B11, T12). Units: returns are fractions of notional,
fees/slippage in bps per side, funding_rate is the settled fraction per funding interval.

Sign convention: side=+1 long, -1 short. Gross return = side * (exit/entry - 1).
Slippage is adverse on both legs. Funding: a LONG pays when funding_rate > 0, a SHORT receives it,
for every funding timestamp t with entry_time < t <= exit_time.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TradeResult:
    side: int
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    gross_return: float
    fee_cost: float
    slippage_cost: float
    funding_cost: float
    net_return: float


def trade_return(side: int, entry_price: float, exit_price: float, entry_time: pd.Timestamp, exit_time: pd.Timestamp,
                 taker_fee_bps: float, slippage_bps: float, funding: pd.DataFrame | None) -> TradeResult:
    if side not in (1, -1):
        raise ValueError("side must be +1 (long) or -1 (short)")
    if not (entry_price > 0 and exit_price > 0):
        raise ValueError("prices must be positive")
    if exit_time <= entry_time:
        raise ValueError("exit must be after entry")
    slip = slippage_bps / 1e4
    eff_entry = entry_price * (1 + side * slip)  # buy higher / sell lower
    eff_exit = exit_price * (1 - side * slip)
    gross = side * (exit_price / entry_price - 1.0)
    after_slip = side * (eff_exit / eff_entry - 1.0)
    fee = 2 * taker_fee_bps / 1e4
    fund = 0.0
    if funding is not None and len(funding):
        m = (funding["funding_time"] > entry_time) & (funding["funding_time"] <= exit_time)
        fund = float(side * funding.loc[m, "funding_rate"].sum())  # positive => cost
    net = after_slip - fee - fund
    return TradeResult(side, entry_time, exit_time, entry_price, exit_price, gross, fee, gross - after_slip, fund, net)


def max_drawdown(net_returns: np.ndarray) -> float:
    if len(net_returns) == 0:
        return 0.0
    eq = np.cumsum(np.log1p(np.clip(net_returns, -0.99, None)))
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    return float(np.max(peak - eq)) if len(eq) else 0.0
