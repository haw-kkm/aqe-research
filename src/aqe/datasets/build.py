"""`aqe build-dataset` (B07.1, B09): causal features, per-horizon 3-class labels, walk-forward folds.

Label at decision row t (bar closing at C_t, as_of = C_t + decision_delay):
  r = log(P(C_t + h) / P(C_t)),  b_t = max(cost_floor, k * rv_288_t * sqrt(h / interval))
  up: r > b_t · down: r < -b_t · neutral otherwise.
k is fitted ONLY on rows of the initial training window (as_of < first_oos_start with matured
labels) to hit the configured neutral fraction, then frozen for every later row.
label_end_at = C_t + h + assumed_latency (when the label's closing bar became available).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from aqe import __version__
from aqe.config import ExperimentConfig, LocalConfig
from aqe.contracts.core import Provenance, iso_utc, utc_now
from aqe.contracts.data import DatasetManifest, FoldSpec, SegmentSpec, SourceFileRef
from aqe.features.ohlcv_v0 import FEATURES_V0, compute_ohlcv_v0, feature_schema_hash
from aqe.features.snapshot import weakest_provenance
from aqe.storage import atomic_write_json, read_json, sha256_file
from aqe.storage.bars import bars_dir, read_bars, to_grid
from aqe.storage.runs import EXIT_NOT_READY, EXIT_OK, RunRecord

CLASS_DOWN, CLASS_NEUTRAL, CLASS_UP = 0, 1, 2


def fit_k(r: pd.Series, sigma: pd.Series, n_bars: int, cost_floor: float, target_neutral: float) -> float:
    """Bisection on k so that P(|r| <= max(c, k*sigma*sqrt(n))) == target_neutral (training rows only)."""
    ok = r.notna() & sigma.notna()
    r, s = r[ok].to_numpy(), sigma[ok].to_numpy() * np.sqrt(n_bars)
    if len(r) < 1000:
        raise ValueError("not enough rows to fit label threshold k")
    lo, hi = 0.0, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        frac = np.mean(np.abs(r) <= np.maximum(cost_floor, mid * s))
        if frac < target_neutral:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def make_labels(close: pd.Series, sigma: pd.Series, n_bars: int, k: float, cost_floor: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    fut = close.shift(-n_bars)
    r = np.log(fut / close)
    b = np.maximum(cost_floor, k * sigma * np.sqrt(n_bars))
    y = pd.Series(np.where(r > b, CLASS_UP, np.where(r < -b, CLASS_DOWN, CLASS_NEUTRAL)), index=close.index, dtype="float64")
    y[r.isna() | sigma.isna()] = np.nan
    return r, b, y


def add_months(ts: pd.Timestamp, months: int) -> pd.Timestamp:
    return ts + pd.DateOffset(months=months)


def make_folds(exp: ExperimentConfig, horizon: int) -> list[FoldSpec]:
    seg = exp.segments
    folds = []
    s = pd.Timestamp(seg.first_oos_start)
    cal = pd.Timestamp(seg.calibration_start)
    i = 0
    while s < cal:
        e = min(add_months(s, seg.fold_months), cal)
        folds.append(FoldSpec(
            fold_id=f"h{horizon}_f{i:02d}", train_start=seg.dev_start,
            train_label_end_max=(s - pd.Timedelta(seconds=seg.embargo_seconds)).to_pydatetime(),
            oos_start=s.to_pydatetime(), oos_end=e.to_pydatetime(), embargo_seconds=seg.embargo_seconds,
        ))
        s, i = e, i + 1
    return folds


def dataset_dir(cfg: LocalConfig, exp: ExperimentConfig) -> Path:
    return cfg.paths.artifact_root / exp.experiment_id / "dataset"


def load_dataset(cfg: LocalConfig, exp: ExperimentConfig) -> tuple[pd.DataFrame, DatasetManifest]:
    d = dataset_dir(cfg, exp)
    man = DatasetManifest.model_validate(read_json(d / "manifest.json"))
    df = pd.read_parquet(d / "dataset.parquet")
    return df, man


def manifest_hash(man: DatasetManifest) -> str:
    return hashlib.sha256(json.dumps(man.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


def run_build_dataset(exp: ExperimentConfig, cfg: LocalConfig, cfg_hash: str, rec: RunRecord) -> int:
    inst = exp.instrument
    interval = exp.bar_interval_seconds
    bars = read_bars(cfg.paths.data_root, inst, "5m")
    bars = bars[bars["is_closed"]]
    if len(bars) < 50_000:
        rec.notes.append(f"only {len(bars)} closed bars available; run `aqe backfill` first")
        return EXIT_NOT_READY
    grid = to_grid(bars, interval)
    feats = compute_ohlcv_v0(grid, interval)
    close = grid["close"].astype("float64")
    bar_close = grid.index + pd.Timedelta(seconds=interval)
    latency = pd.Timedelta(seconds=cfg.backfill.assumed_latency_seconds)
    df = feats.copy()
    df.insert(0, "bar_close", bar_close)
    df.insert(1, "as_of", bar_close + pd.Timedelta(seconds=exp.decision_delay_seconds))
    df.insert(2, "close", close.to_numpy())
    df.insert(3, "bar_present", grid["present"].astype(bool).to_numpy())
    # provenance per row (weakest of bars used is tracked at manifest level)
    sigma = feats["rv_288"]
    lab_def = {"kind": exp.label.kind, "vol_window_bars": exp.label.vol_window_bars, "cost_floor_bps": exp.label.cost_floor_bps,
               "target_neutral_fraction": exp.label.target_neutral_fraction, "price": "close of completed 5m bar",
               "classes": ["down", "neutral", "up"], "k_by_horizon": {}, "k_fit_window": None}
    cost_floor = exp.label.cost_floor_bps / 1e4
    seg = exp.segments
    for h in exp.horizons_seconds:
        n = h // interval
        label_end = bar_close + pd.Timedelta(seconds=h) + latency
        r_raw = np.log(close.shift(-n) / close)
        fit_rows = (df["as_of"] >= pd.Timestamp(seg.dev_start)) & (label_end <= pd.Timestamp(seg.first_oos_start))
        k = fit_k(r_raw[fit_rows.to_numpy()], sigma[fit_rows.to_numpy()], n, cost_floor, exp.label.target_neutral_fraction)
        r, b, y = make_labels(close, sigma, n, k, cost_floor)
        df[f"r_{h}"] = r.to_numpy()
        df[f"b_{h}"] = b.to_numpy()
        df[f"y_{h}"] = y.to_numpy()
        df[f"label_end_at_{h}"] = label_end
        lab_def["k_by_horizon"][str(h)] = k
    lab_def["k_fit_window"] = {"as_of_from": iso_utc(seg.dev_start), "label_end_at_max": iso_utc(seg.first_oos_start)}
    # keep the complete 5m grid (missing bars stay as NaN rows) so sequence windows stay contiguous
    df = df.reset_index().rename(columns={"index": "open_time"})

    data_end = df["bar_close"].max().to_pydatetime()
    test_end = seg.test_end or data_end
    segments = (
        SegmentSpec(name="dev", start=seg.dev_start, end=seg.calibration_start),
        SegmentSpec(name="calibration", start=seg.calibration_start, end=seg.test_start),
        SegmentSpec(name="test", start=seg.test_start, end=test_end),
    )
    folds = {str(h): tuple(make_folds(exp, h)) for h in exp.horizons_seconds}
    files = sorted(bars_dir(cfg.paths.data_root, inst, "5m").glob("*.parquet"))
    src = tuple(SourceFileRef(path=str(f), sha256=sha256_file(f)) for f in files)
    provs = weakest_provenance(bars["provenance"].unique())
    out = dataset_dir(cfg, exp)
    out.mkdir(parents=True, exist_ok=True)
    dpath = out / "dataset.parquet"
    df.to_parquet(dpath, index=False)
    ds_hash = hashlib.sha256((sha256_file(dpath) + cfg_hash).encode()).hexdigest()
    man = DatasetManifest(
        dataset_id=f"ds-{exp.experiment_id}-{ds_hash[:10]}", created_at=utc_now(), instrument=inst,
        feature_profile=exp.feature_profile, bar_interval_seconds=interval, decision_delay_seconds=exp.decision_delay_seconds,
        feature_names=FEATURES_V0, feature_schema_hash=feature_schema_hash(), horizons_seconds=tuple(exp.horizons_seconds),
        label_definition=lab_def, data_start=df["bar_close"].min().to_pydatetime(), data_end=data_end,
        provenance=tuple(sorted({Provenance(p) for p in bars["provenance"].unique()}, key=lambda p: p.value)), segments=segments,
        folds=folds,
        purge_rules={"rule": "training rows require label_end_at <= boundary - embargo_seconds",
                     "label_end_at": "bar_close + horizon + assumed_latency", "embargo_seconds": seg.embargo_seconds,
                     "applies_to": ["fold train", "final base train (boundary=calibration_start)",
                                    "meta train (boundary=calibration_start)", "calibration fit (boundary=test_start)"]},
        source_files=src, n_rows=len(df), config_hash=cfg_hash, code_version=__version__,
        notes=(f"weakest provenance: {provs.value}",
               "bars downloaded after the fact are ASSUMED_LATENCY_REPLAY, not historical receipt records"),
    )
    atomic_write_json(out / "manifest.json", man.model_dump(mode="json"))
    stats = {}
    for h in exp.horizons_seconds:
        yy = df[f"y_{h}"].dropna()
        stats[str(h)] = {"rows_labeled": int(len(yy)), "class_frac": [round(float((yy == c).mean()), 4) for c in (0, 1, 2)],
                         "k": lab_def["k_by_horizon"][str(h)], "n_folds": len(folds[str(h)])}
    rec.outputs.update({"dataset_id": man.dataset_id, "path": str(dpath), "manifest": str(out / "manifest.json"),
                        "rows": len(df), "data_start": iso_utc(man.data_start), "data_end": iso_utc(data_end), "labels": stats})
    return EXIT_OK


def rows_for_training(df: pd.DataFrame, h: int, start: datetime, label_end_max: datetime) -> pd.DataFrame:
    sel = df[(df["as_of"] >= pd.Timestamp(start)) & (df[f"label_end_at_{h}"] <= pd.Timestamp(label_end_max)) & df[f"y_{h}"].notna()]
    return sel


def rows_in_window(df: pd.DataFrame, h: int, start: datetime, end: datetime, require_label: bool = True,
                   label_end_max: datetime | None = None) -> pd.DataFrame:
    m = (df["as_of"] >= pd.Timestamp(start)) & (df["as_of"] < pd.Timestamp(end))
    if require_label:
        m &= df[f"y_{h}"].notna()
    if label_end_max is not None:
        m &= df[f"label_end_at_{h}"] <= pd.Timestamp(label_end_max)
    return df[m]


def assert_purged(train: pd.DataFrame, h: int, boundary: datetime) -> None:
    """T05: every training label must have matured before the boundary."""
    if len(train) and train[f"label_end_at_{h}"].max() > pd.Timestamp(boundary):
        raise AssertionError(f"label leakage: training label ends {train[f'label_end_at_{h}'].max()} after boundary {boundary}")
