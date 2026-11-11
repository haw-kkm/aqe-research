"""Time-ordered meta model (`aqe train-meta`, B09): regularised multinomial logistic regression
fitted ONLY on walk-forward OOS base predictions whose labels matured before calibration_start.
Not sklearn StackingClassifier: OOS inputs are produced explicitly by `aqe build-oos`.

Inputs per row: log-probabilities of each base model, base-model disagreement, a few regime
features. A `numeric_context` meta needs historical structured context assessments, which do not
exist yet; it is therefore not trained (context is reported, not combined).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from aqe.config import ExperimentConfig, LocalConfig
from aqe.contracts.core import CLASS_ORDER, ModelReadiness, iso_utc, utc_now
from aqe.contracts.predictions import OODStatus
from aqe.datasets.build import load_dataset, manifest_hash
from aqe.ensemble.oos import oos_path
from aqe.models.registry import artifact_dir, save_metadata, update_registry
from aqe.storage.runs import EXIT_NOT_READY, EXIT_OK, RunRecord

REGIME_FEATURES = ["rv_288", "trend_288", "vol_ratio_12_288"]
PROB_COLS = ["p_down", "p_neutral", "p_up"]


def disagreement(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mad = np.max(np.abs(a - b), axis=1)
    m = 0.5 * (a + b)
    js = 0.5 * np.sum(a * np.log((a + 1e-12) / (m + 1e-12)), 1) + 0.5 * np.sum(b * np.log((b + 1e-12) / (m + 1e-12)), 1)
    return mad, js


def design(base: dict[str, np.ndarray], regime: pd.DataFrame, base_kinds: list[str]) -> np.ndarray:
    cols = [np.log(np.clip(base[k], 1e-6, 1.0)) for k in base_kinds]
    if len(base_kinds) == 2:
        mad, js = disagreement(base[base_kinds[0]], base[base_kinds[1]])
        cols += [mad[:, None], js[:, None]]
    cols.append(regime[REGIME_FEATURES].to_numpy("float64"))
    return np.hstack(cols)


def softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


class MetaModel:
    def __init__(self, base_kinds, mean, scale, coef, intercept, ood_ranges):
        self.base_kinds = list(base_kinds)
        self.mean, self.scale = np.asarray(mean), np.asarray(scale)
        self.coef, self.intercept = np.asarray(coef), np.asarray(intercept)
        self.ood_ranges = ood_ranges

    def predict_proba(self, base: dict[str, np.ndarray], regime: pd.DataFrame) -> np.ndarray:
        X = design(base, regime, self.base_kinds)
        if not np.all(np.isfinite(X)):
            raise ValueError("meta inputs contain NaN/inf")
        return softmax(((X - self.mean) / self.scale) @ self.coef.T + self.intercept)

    def ood_status(self, regime: pd.DataFrame) -> OODStatus:
        row = regime.iloc[-1]
        for f, (lo, hi) in self.ood_ranges.items():
            v = row.get(f)
            if v is None or not np.isfinite(v):
                return OODStatus.UNKNOWN
            if v < lo or v > hi:
                return OODStatus.OUT_OF_DISTRIBUTION
        return OODStatus.IN_DISTRIBUTION

    def to_json(self) -> dict[str, Any]:
        return {"base_kinds": self.base_kinds, "mean": self.mean.tolist(), "scale": self.scale.tolist(),
                "coef": self.coef.tolist(), "intercept": self.intercept.tolist(), "ood_ranges": self.ood_ranges}

    @classmethod
    def load(cls, adir: Path, meta: dict[str, Any]) -> "MetaModel":
        d = json.loads((Path(adir) / "meta_model.json").read_text(encoding="utf-8"))
        return cls(d["base_kinds"], d["mean"], d["scale"], d["coef"], d["intercept"], {k: tuple(v) for k, v in d["ood_ranges"].items()})


def fit_meta(base: dict[str, np.ndarray], regime: pd.DataFrame, y: np.ndarray, base_kinds: list[str], C: float, max_iter: int) -> MetaModel:
    X = design(base, regime, base_kinds)
    sc = StandardScaler().fit(X)
    lr = LogisticRegression(C=C, max_iter=max_iter)
    lr.fit(sc.transform(X), y.astype(int))
    if list(lr.classes_) != [0, 1, 2]:
        raise ValueError("meta training data must contain all three classes")
    ood = {f: (float(np.nanquantile(regime[f], 0.005)), float(np.nanquantile(regime[f], 0.995))) for f in REGIME_FEATURES}
    return MetaModel(base_kinds, sc.mean_, sc.scale_, lr.coef_, lr.intercept_, ood)


def logloss(p: np.ndarray, y: np.ndarray) -> float:
    return float(-np.mean(np.log(np.clip(p[np.arange(len(y)), y.astype(int)], 1e-12, 1.0))))


def load_meta_rows(cfg: LocalConfig, exp: ExperimentConfig, h: int, df: pd.DataFrame, kinds: list[str], label_end_max) -> pd.DataFrame:
    merged = None
    for k in kinds:
        o = pd.read_parquet(oos_path(cfg, exp, k, h))[["open_time", "fold_id", "y", "label_end_at", *PROB_COLS]]
        o = o.rename(columns={c: f"{k}_{c}" for c in PROB_COLS}).rename(columns={"fold_id": f"{k}_fold"})
        merged = o if merged is None else merged.merge(o[["open_time", f"{k}_fold", *[f"{k}_{c}" for c in PROB_COLS]]], on="open_time")
    merged = merged.merge(df[["open_time", "as_of", *REGIME_FEATURES]], on="open_time", how="left")
    ok = merged["y"].notna() & (merged["label_end_at"] <= pd.Timestamp(label_end_max))
    for k in kinds:
        ok &= np.isfinite(merged[f"{k}_p_down"])
    ok &= merged[REGIME_FEATURES].notna().all(axis=1)
    return merged[ok].sort_values("as_of").reset_index(drop=True)


def run_train_meta(exp: ExperimentConfig, cfg: LocalConfig, cfg_hash: str, rec: RunRecord) -> int:
    df, man = load_dataset(cfg, exp)
    boundary = pd.Timestamp(exp.segments.calibration_start) - pd.Timedelta(seconds=exp.segments.embargo_seconds)
    out = {}
    for h in exp.horizons_seconds:
        kinds = [k for k in ("lgbm", "tcn") if oos_path(cfg, exp, k, h).exists()]
        if not kinds:
            rec.notes.append(f"h={h}: no OOS predictions; run build-oos")
            return EXIT_NOT_READY
        stale = []
        for k in kinds:
            om = json.loads(oos_path(cfg, exp, k, h).with_suffix(".manifest.json").read_text(encoding="utf-8"))
            if om.get("dataset_manifest_hash") != manifest_hash(man):
                stale.append(f"{k}: OOS built from dataset {om.get('dataset_id')} (manifest hash differs from current dataset)")
        if stale:
            rec.notes.append(f"h={h}: OOS inputs do not match the current dataset; re-run build-oos: " + "; ".join(stale))
            return EXIT_NOT_READY
        rows = load_meta_rows(cfg, exp, h, df, kinds, boundary)
        if rows["label_end_at"].max() > boundary:
            raise AssertionError("meta rows leak past calibration_start")
        base = {k: rows[[f"{k}_{c}" for c in PROB_COLS]].to_numpy() for k in kinds}
        y = rows["y"].to_numpy().astype(int)
        # development diagnostic: forward split inside the OOS period (earlier folds -> later folds)
        folds = sorted(rows[f"{kinds[0]}_fold"].unique())
        cut = folds[int(len(folds) * 2 / 3)] if len(folds) >= 3 else None
        diag = None
        if cut is not None:
            tr = rows[f"{kinds[0]}_fold"] < cut
            te_start = rows.loc[~tr, "as_of"].min()
            tr &= rows["label_end_at"] <= te_start
            te = rows[f"{kinds[0]}_fold"] >= cut
            m = fit_meta({k: v[tr.to_numpy()] for k, v in base.items()}, rows[tr], y[tr.to_numpy()], kinds, exp.meta.C, exp.meta.max_iter)
            pm = m.predict_proba({k: v[te.to_numpy()] for k, v in base.items()}, rows[te])
            diag = {"train_folds": [f for f in folds if f < cut], "eval_folds": [f for f in folds if f >= cut],
                    "meta_logloss": logloss(pm, y[te.to_numpy()]),
                    **{f"{k}_logloss": logloss(base[k][te.to_numpy()], y[te.to_numpy()]) for k in kinds},
                    "prior_logloss": logloss(np.tile(np.bincount(y[tr.to_numpy()], minlength=3) / tr.sum(), (int(te.sum()), 1)), y[te.to_numpy()])}
            if len(kinds) == 2:
                diag["avg_logloss"] = logloss(0.5 * (base[kinds[0]][te.to_numpy()] + base[kinds[1]][te.to_numpy()]), y[te.to_numpy()])
                diag["oos_error_corr"] = float(np.corrcoef(
                    -np.log(np.clip(base[kinds[0]][np.arange(len(y)), y], 1e-12, 1)),
                    -np.log(np.clip(base[kinds[1]][np.arange(len(y)), y], 1e-12, 1)))[0, 1])
        model = fit_meta(base, rows, y, kinds, exp.meta.C, exp.meta.max_iter)
        thr = None
        if len(kinds) == 2:
            mad, _ = disagreement(base[kinds[0]], base[kinds[1]])
            thr = float(np.quantile(mad, 0.95))
        aid = f"meta-h{h}-numeric_only-{utc_now().strftime('%Y%m%dT%H%M%S')}"
        adir = artifact_dir(cfg.paths.artifact_root, exp.experiment_id, aid)
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "meta_model.json").write_text(json.dumps(model.to_json()), encoding="utf-8")
        meta = {
            "artifact_id": aid, "kind": "meta", "combo": "numeric_only", "experiment_id": exp.experiment_id,
            "instrument_id": exp.instrument.instrument_id, "feature_profile": man.feature_profile.value,
            "feature_schema_hash": man.feature_schema_hash, "feature_names": list(man.feature_names), "horizon_seconds": h,
            "class_order": list(CLASS_ORDER), "train_label_cutoff": str(rows["label_end_at"].max()),
            "dataset_id": man.dataset_id, "dataset_manifest_hash": manifest_hash(man), "config_hash": cfg_hash,
            "code_version": __import__("aqe").__version__, "seed": exp.seed, "readiness": ModelReadiness.TRAINED_UNVALIDATED.value,
            "created_at": iso_utc(utc_now()), "base_kinds": kinds, "regime_features": REGIME_FEATURES, "C": exp.meta.C,
            "train_rows": int(len(rows)), "train_as_of": [str(rows["as_of"].min()), str(rows["as_of"].max())],
            "train_class_frac": [float(x) for x in np.bincount(y, minlength=3) / len(y)],
            "disagreement_threshold": thr, "disagreement_threshold_basis": "95th pct of OOS max_abs_diff (dev)",
            "dev_forward_diagnostic": diag,
            "oos_inputs": {k: str(oos_path(cfg, exp, k, h)) for k in kinds},
        }
        save_metadata(cfg.paths.artifact_root, exp.experiment_id, meta)
        update_registry(cfg.paths.artifact_root, exp.experiment_id, h, "meta_numeric_only", aid)
        out[str(h)] = {"artifact_id": aid, "rows": len(rows), "base_kinds": kinds, "disagreement_threshold": thr, "dev_forward_diagnostic": diag}
    rec.outputs["meta"] = out
    rec.notes.append("numeric_context meta not trained: no historical structured context assessments exist (BLOCKED on context history)")
    return EXIT_OK
