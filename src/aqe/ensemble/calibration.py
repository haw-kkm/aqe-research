"""`aqe calibrate` (B09): scalar temperature scaling fitted on the independent calibration segment,
using the FIXED final base models + meta. Rows whose labels end after test_start are purged.

`calibration_validated` stays False here; it can only become True after the locked test segment
shows acceptable reliability (aqe evaluate). Calibrators for single-model baselines (E1/E2) are
fitted on the same segment so the comparison is like-for-like.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from aqe.config import ExperimentConfig, LocalConfig
from aqe.contracts.core import CLASS_ORDER, ModelReadiness, iso_utc, utc_now
from aqe.datasets.build import load_dataset, manifest_hash, rows_in_window
from aqe.ensemble.meta import MetaModel, logloss, softmax
from aqe.ensemble.oos import load_predictor, predict_rows
from aqe.models.registry import artifact_dir, exp_dir, load_metadata, load_registry, save_metadata, update_registry
from aqe.storage.runs import EXIT_NOT_READY, EXIT_OK, RunRecord


class TemperatureCalibrator:
    def __init__(self, T: float):
        if not (math.isfinite(T) and T > 0):
            raise ValueError("temperature must be positive and finite")
        self.T = float(T)

    def apply(self, p: np.ndarray) -> np.ndarray:
        return softmax(np.log(np.clip(np.asarray(p, dtype="float64"), 1e-12, 1.0)) / self.T)

    @classmethod
    def load(cls, adir: Path, meta: dict[str, Any]) -> "TemperatureCalibrator":
        return cls(json.loads((Path(adir) / "calibrator.json").read_text(encoding="utf-8"))["temperature"])


def fit_temperature(p: np.ndarray, y: np.ndarray) -> float:
    logp = np.log(np.clip(p, 1e-12, 1.0))

    def nll(logT: float) -> float:
        q = softmax(logp / math.exp(logT))
        return -float(np.mean(np.log(np.clip(q[np.arange(len(y)), y], 1e-12, 1.0))))

    lo, hi = math.log(0.2), math.log(5.0)
    g = (math.sqrt(5) - 1) / 2
    a, b = hi - g * (hi - lo), lo + g * (hi - lo)
    fa, fb = nll(a), nll(b)
    for _ in range(80):
        if fa < fb:
            hi, b, fb = b, a, fa
            a = hi - g * (hi - lo)
            fa = nll(a)
        else:
            lo, a, fa = a, b, fb
            b = lo + g * (hi - lo)
            fb = nll(b)
    return math.exp((lo + hi) / 2)


def ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    conf = p.max(1)
    pred = p.argmax(1)
    e = 0.0
    edges = np.linspace(0, 1, bins + 1)
    for i in range(bins):
        m = (conf > edges[i]) & (conf <= edges[i + 1])
        if m.any():
            e += m.mean() * abs((pred[m] == y[m]).mean() - conf[m].mean())
    return float(e)


def segment_predictions(cfg: LocalConfig, exp: ExperimentConfig, df: pd.DataFrame, h: int, rows: pd.Index,
                        reg: dict[str, str]) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Predictions of the registered final base models (+ meta raw) for given rows; cached per artifact."""
    cache_dir = exp_dir(cfg.paths.artifact_root, exp.experiment_id) / "pred_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    out, metas = {}, {}
    for kind in ("lgbm", "tcn"):
        aid = reg.get(kind)
        if not aid:
            continue
        cpath = cache_dir / f"{aid}.parquet"
        cached = pd.read_parquet(cpath) if cpath.exists() else None
        need = rows if cached is None else rows[~df.loc[rows, "open_time"].isin(cached["open_time"])]
        meta, pred = load_predictor(cfg, exp, aid)
        if len(need):
            p = predict_rows(kind, pred, df, need)
            new = pd.DataFrame({"open_time": df.loc[need, "open_time"].to_numpy(), "p_down": p[:, 0], "p_neutral": p[:, 1], "p_up": p[:, 2]})
            cached = new if cached is None else pd.concat([cached, new], ignore_index=True).drop_duplicates("open_time", keep="last")
            cached.to_parquet(cpath, index=False)
        m = df.loc[rows, ["open_time"]].merge(cached, on="open_time", how="left")
        out[kind] = m[["p_down", "p_neutral", "p_up"]].to_numpy("float64")
        metas[kind] = meta
    return out, metas


def run_calibrate(exp: ExperimentConfig, cfg: LocalConfig, cfg_hash: str, rec: RunRecord) -> int:
    df, man = load_dataset(cfg, exp)
    seg = exp.segments
    label_max = (pd.Timestamp(seg.test_start) - pd.Timedelta(seconds=seg.embargo_seconds)).to_pydatetime()
    res = {}
    for h in exp.horizons_seconds:
        reg = load_registry(cfg.paths.artifact_root, exp.experiment_id)["horizons"].get(str(h), {})
        meta_id = reg.get("meta_numeric_only")
        if not meta_id or not reg.get("lgbm"):
            rec.notes.append(f"h={h}: need final base models and meta before calibration")
            return EXIT_NOT_READY
        mmeta = load_metadata(cfg.paths.artifact_root, exp.experiment_id, meta_id)
        mm = MetaModel.load(artifact_dir(cfg.paths.artifact_root, exp.experiment_id, meta_id), mmeta)
        rows = rows_in_window(df, h, seg.calibration_start, seg.test_start, label_end_max=label_max).index
        base, bmetas = segment_predictions(cfg, exp, df, h, rows, reg)
        for k, bm in bmetas.items():
            if pd.Timestamp(bm["train_label_cutoff"]) > pd.Timestamp(seg.calibration_start):
                raise AssertionError(f"{k} final model trained on labels after calibration_start")
        regime = df.loc[rows]
        ok = np.all([np.isfinite(base[k]).all(1) for k in mm.base_kinds], axis=0) & regime[["rv_288", "trend_288", "vol_ratio_12_288"]].notna().all(1).to_numpy()
        y = df.loc[rows, f"y_{h}"].to_numpy()[ok].astype(int)
        raw = {"numeric_only": mm.predict_proba({k: base[k][ok] for k in mm.base_kinds}, regime[ok])}
        for k in base:
            raw[f"{k}_only"] = base[k][ok]
        n_indep = int(len(y) * exp.bar_interval_seconds / h)
        hres = {"rows": int(len(y)), "independent_label_windows": n_indep}
        for combo, p in raw.items():
            T = fit_temperature(p, y)
            cal = TemperatureCalibrator(T).apply(p)
            aid = f"calib-h{h}-{combo}-{utc_now().strftime('%Y%m%dT%H%M%S')}"
            adir = artifact_dir(cfg.paths.artifact_root, exp.experiment_id, aid)
            adir.mkdir(parents=True, exist_ok=True)
            (adir / "calibrator.json").write_text(json.dumps({"method": "temperature", "temperature": T}), encoding="utf-8")
            fitted_on = {"meta": meta_id, **{k: reg[k] for k in mm.base_kinds}} if combo == "numeric_only" else {combo[:-5]: reg[combo[:-5]]}
            meta = {
                "artifact_id": aid, "kind": "calibrator", "combo": combo, "method": "temperature", "temperature": T,
                "experiment_id": exp.experiment_id, "instrument_id": exp.instrument.instrument_id,
                "feature_profile": man.feature_profile.value, "feature_schema_hash": man.feature_schema_hash,
                "feature_names": list(man.feature_names), "horizon_seconds": h, "class_order": list(CLASS_ORDER),
                "train_label_cutoff": str(df.loc[rows[ok], f"label_end_at_{h}"].max()), "dataset_id": man.dataset_id,
                "dataset_manifest_hash": manifest_hash(man), "config_hash": cfg_hash, "code_version": __import__("aqe").__version__,
                "seed": exp.seed, "readiness": ModelReadiness.TRAINED_UNVALIDATED.value, "created_at": iso_utc(utc_now()),
                "fitted_on_artifacts": fitted_on, "segment": [iso_utc(seg.calibration_start), iso_utc(seg.test_start)],
                "rows": int(len(y)), "independent_label_windows": n_indep,
                "sample_sufficient": n_indep >= exp.calibration.min_independent_samples,
                "calibration_validated": False, "calibration_validated_basis": "pending locked-test reliability (aqe evaluate)",
                "in_sample": {"logloss_before": logloss(p, y), "logloss_after": logloss(cal, y), "ece_before": ece(p, y), "ece_after": ece(cal, y)},
            }
            save_metadata(cfg.paths.artifact_root, exp.experiment_id, meta)
            update_registry(cfg.paths.artifact_root, exp.experiment_id, h, f"calib_{combo}", aid)
            hres[combo] = {"artifact_id": aid, "T": round(T, 4), **{k: round(v, 5) for k, v in meta["in_sample"].items()}}
        # distribution-shift diagnostic: meta inputs from final models vs fold models (B09)
        hres["shift_diag_mean_probs"] = {k: [round(float(x), 4) for x in base[k][ok].mean(0)] for k in mm.base_kinds}
        res[str(h)] = hres
    rec.outputs["calibration"] = res
    return EXIT_OK
