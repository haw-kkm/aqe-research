"""Build an as-of FeatureSnapshot from stored bars (B04, B06). Enforces available_at <= as_of (T01)."""

from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from aqe.contracts.core import FeatureProfile, InstrumentKey, Provenance, QualityStatus
from aqe.contracts.data import FeatureSnapshot
from aqe.features.ohlcv_v0 import FEATURES_V0, MAX_LOOKBACK_BARS, compute_ohlcv_v0, feature_schema_hash
from aqe.storage.bars import bars_as_of, to_grid

PROVENANCE_STRENGTH = {
    Provenance.SYNTHETIC_FIXTURE: 0,
    Provenance.ASSUMED_LATENCY_REPLAY: 1,
    Provenance.VERIFIED_ARCHIVE: 2,
    Provenance.LIVE_CAPTURE: 3,
}


def weakest_provenance(values) -> Provenance:
    provs = {Provenance(v) for v in values}
    if not provs:
        return Provenance.SYNTHETIC_FIXTURE
    return min(provs, key=lambda p: PROVENANCE_STRENGTH[p])


def build_snapshot(
    bars: pd.DataFrame,
    inst: InstrumentKey,
    as_of: datetime,
    interval_seconds: int = 300,
    max_bar_age_bars: int = 2,
) -> tuple[FeatureSnapshot, pd.Timestamp | None]:
    sel = bars_as_of(bars, as_of)
    names = FEATURES_V0
    shash = feature_schema_hash()
    if sel.empty:
        snap = FeatureSnapshot(
            instrument=inst, as_of=as_of, feature_profile=FeatureProfile.OHLCV_V0,
            feature_names=names, values=tuple(None for _ in names), missing_mask=tuple(True for _ in names),
            source_watermarks={}, provenance=Provenance.SYNTHETIC_FIXTURE, quality=QualityStatus.MISSING,
            quality_reasons=("no closed bars available at as_of",), feature_schema_hash=shash,
        )
        return snap, None
    tail = sel.tail(MAX_LOOKBACK_BARS + 300)
    grid = to_grid(tail, interval_seconds)
    feats = compute_ohlcv_v0(grid, interval_seconds)
    last_open = grid.index[-1]
    row = feats.iloc[-1]
    values, mask = [], []
    for n in names:
        v = row[n]
        if v is None or (isinstance(v, float) and not math.isfinite(v)) or pd.isna(v):
            values.append(None)
            mask.append(True)
        else:
            values.append(float(v))
            mask.append(False)
    reasons = []
    quality = QualityStatus.OK
    last_close = last_open + pd.Timedelta(seconds=interval_seconds)
    age = (pd.Timestamp(as_of) - last_close).total_seconds()
    if age > max_bar_age_bars * interval_seconds:
        quality = QualityStatus.INVALID
        reasons.append(f"last available bar closed {age:.0f}s before as_of")
    if any(mask):
        if quality == QualityStatus.OK:
            quality = QualityStatus.DEGRADED
        reasons.append(f"{sum(mask)} feature(s) missing (incomplete lookback or gaps)")
    n_missing_bars = int((~grid["present"].astype(bool)).sum())
    if n_missing_bars:
        reasons.append(f"{n_missing_bars} missing bar(s) in lookback window")
    used = tail[tail["open_time"] <= last_open]
    watermark = used["available_at"].max().to_pydatetime()
    snap = FeatureSnapshot(
        instrument=inst, as_of=as_of, feature_profile=FeatureProfile.OHLCV_V0,
        feature_names=names, values=tuple(values), missing_mask=tuple(mask),
        source_watermarks={"bars": watermark}, provenance=weakest_provenance(used["provenance"].unique()),
        quality=quality, quality_reasons=tuple(reasons), feature_schema_hash=shash,
    )
    return snap, last_open
