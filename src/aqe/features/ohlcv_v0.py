"""ohlcv_v0 causal feature set (B05.1, B07.1).

Every feature at grid row t (bar opening at t, closing at t+interval) is a function of bars
with open_time <= t only. No learned state: rolling windows are fixed-length trailing windows.
Missing bars propagate NaN (rolling windows require full coverage).

Note: `taker_buy_ratio_12` uses the taker-buy volume field that is part of the Binance kline
record itself; it is NOT aggTrade CVD and NOT order-book information (flow_v1).
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from aqe.contracts.core import FeatureProfile

FEATURE_VERSION = "ohlcv_v0.1"

FEATURES_V0: tuple[str, ...] = (
    "ret_1", "ret_3", "ret_12", "ret_48", "ret_288", "ret_2016",
    "rv_12", "rv_48", "rv_288",
    "rv_ratio_12_288",
    "vol_ratio_12_288",
    "range_1", "range_mean_12",
    "body_1",
    "taker_buy_ratio_12",
    "trend_288", "trend_2016",
    "dd_288", "du_288",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "funding_phase",
)

# Longest trailing window used (bars): 2016 (7 days). Rows with a shorter history have NaNs.
MAX_LOOKBACK_BARS = 2016


def feature_schema_hash(names: tuple[str, ...] = FEATURES_V0, profile: FeatureProfile = FeatureProfile.OHLCV_V0) -> str:
    payload = {"profile": profile.value, "version": FEATURE_VERSION, "names": list(names), "units": "log-ratio/unitless"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def compute_ohlcv_v0(grid: pd.DataFrame, interval_seconds: int = 300) -> pd.DataFrame:
    """grid: complete interval grid indexed by open_time (from storage.bars.to_grid)."""
    close = grid["close"].astype("float64")
    high = grid["high"].astype("float64")
    low = grid["low"].astype("float64")
    open_ = grid["open"].astype("float64")
    vol = grid["volume"].astype("float64")
    tb = grid["taker_buy_base"].astype("float64")
    logc = np.log(close)
    lr = logc.diff()

    f = pd.DataFrame(index=grid.index)
    for n in (1, 3, 12, 48, 288, 2016):
        f[f"ret_{n}"] = logc - logc.shift(n)
    for n in (12, 48, 288):
        f[f"rv_{n}"] = lr.rolling(n, min_periods=n).std()
    f["rv_ratio_12_288"] = np.log((f["rv_12"] + 1e-12) / (f["rv_288"] + 1e-12))
    v12 = vol.rolling(12, min_periods=12).mean()
    v288 = vol.rolling(288, min_periods=288).mean()
    f["vol_ratio_12_288"] = np.log((v12 + 1e-9) / (v288 + 1e-9))
    f["range_1"] = np.log(high / low)
    f["range_mean_12"] = f["range_1"].rolling(12, min_periods=12).mean()
    f["body_1"] = np.log(close / open_)
    f["taker_buy_ratio_12"] = tb.rolling(12, min_periods=12).sum() / (vol.rolling(12, min_periods=12).sum() + 1e-12) - 0.5
    f["trend_288"] = logc - np.log(close.rolling(288, min_periods=288).mean())
    f["trend_2016"] = logc - np.log(close.rolling(2016, min_periods=2016).mean())
    f["dd_288"] = logc - np.log(high.rolling(288, min_periods=288).max())
    f["du_288"] = logc - np.log(low.rolling(288, min_periods=288).min())

    close_time = grid.index + pd.Timedelta(seconds=interval_seconds)
    hours = close_time.hour + close_time.minute / 60.0
    f["hour_sin"] = np.sin(2 * np.pi * hours / 24.0)
    f["hour_cos"] = np.cos(2 * np.pi * hours / 24.0)
    dow = close_time.day_of_week + hours / 24.0
    f["dow_sin"] = np.sin(2 * np.pi * dow / 7.0)
    f["dow_cos"] = np.cos(2 * np.pi * dow / 7.0)
    # position inside the 8h funding cycle (00/08/16 UTC); deterministic clock feature
    f["funding_phase"] = ((close_time.hour % 8) * 60 + close_time.minute) / 480.0

    # a missing bar invalidates its own row entirely (calendar features included)
    f.loc[~grid["present"].astype(bool)] = np.nan
    f = f.replace([np.inf, -np.inf], np.nan)
    return f[list(FEATURES_V0)].astype("float64")
