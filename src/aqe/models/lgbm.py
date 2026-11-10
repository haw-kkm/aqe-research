"""LightGBM multiclass base model (B07.2). Uses the official `lightgbm` package; AQE adds the
crypto dataset, time-ordered early-stopping split with purge, artifact metadata and inference."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from aqe.config import LGBMConfig
from aqe.contracts.core import CLASS_ORDER


def check_probs(p: np.ndarray) -> np.ndarray:
    p = np.asarray(p, dtype="float64")
    if p.ndim != 2 or p.shape[1] != 3:
        raise ValueError(f"expected (n,3) probabilities, got {p.shape}")
    if not np.all(np.isfinite(p)):
        raise ValueError("non-finite probabilities")
    if np.any(p < -1e-9) or np.any(p > 1 + 1e-9) or np.max(np.abs(p.sum(1) - 1)) > 1e-5:
        raise ValueError("invalid probability rows")
    return np.clip(p, 0.0, 1.0) / np.clip(p, 0.0, 1.0).sum(1, keepdims=True)


def time_split_with_purge(train: pd.DataFrame, h: int, val_fraction: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Last `val_fraction` (by as_of) is validation; fit rows must have labels matured before validation starts."""
    train = train.sort_values("as_of")
    n_val = max(1, int(len(train) * val_fraction))
    val = train.iloc[-n_val:]
    val_start = val["as_of"].iloc[0]
    fit = train.iloc[:-n_val]
    fit = fit[fit[f"label_end_at_{h}"] <= val_start]
    return fit, val


def train_lgbm(train: pd.DataFrame, h: int, features: list[str], cfg: LGBMConfig, seed: int) -> tuple[lgb.Booster, dict[str, Any]]:
    fit, val = time_split_with_purge(train, h, cfg.val_fraction)
    if cfg.train_stride > 1:
        fit = fit.iloc[:: cfg.train_stride]
    params = {**cfg.params, "seed": seed, "deterministic": True, "force_row_wise": True}
    dfit = lgb.Dataset(fit[features].to_numpy("float32"), label=fit[f"y_{h}"].astype(int).to_numpy(), feature_name=features, free_raw_data=True)
    dval = lgb.Dataset(val[features].to_numpy("float32"), label=val[f"y_{h}"].astype(int).to_numpy(), reference=dfit, free_raw_data=True)
    evals: dict = {}
    booster = lgb.train(params, dfit, num_boost_round=cfg.num_boost_round, valid_sets=[dval], valid_names=["val"],
                        callbacks=[lgb.early_stopping(cfg.early_stopping_rounds, verbose=False), lgb.record_evaluation(evals)])
    info = {
        "fit_rows": int(len(fit)), "val_rows": int(len(val)),
        "fit_as_of": [str(fit["as_of"].min()), str(fit["as_of"].max())],
        "val_as_of": [str(val["as_of"].min()), str(val["as_of"].max())],
        "best_iteration": int(booster.best_iteration),
        "val_multi_logloss_best": float(evals["val"]["multi_logloss"][booster.best_iteration - 1]) if evals else None,
        "train_label_cutoff": str(train[f"label_end_at_{h}"].max()),
        "importance_gain": dict(zip(features, [float(x) for x in booster.feature_importance("gain")])),
    }
    return booster, info


class LGBMPredictor:
    def __init__(self, booster: lgb.Booster, features: list[str]):
        self.booster = booster
        self.features = features

    @classmethod
    def load(cls, adir: Path, meta: dict[str, Any]) -> "LGBMPredictor":
        if tuple(meta["class_order"]) != CLASS_ORDER:
            raise ValueError("class order mismatch")
        b = lgb.Booster(model_file=str(Path(adir) / "model.txt"))
        if list(b.feature_name()) != list(meta["feature_names"]):
            raise ValueError("booster feature order differs from metadata")
        return cls(b, list(meta["feature_names"]))

    def predict_frame(self, df: pd.DataFrame) -> np.ndarray:
        missing = [f for f in self.features if f not in df.columns]
        if missing:
            raise ValueError(f"missing features: {missing}")
        X = df[self.features].to_numpy("float32")
        return check_probs(self.booster.predict(X, num_iteration=self.booster.best_iteration or None))

    def predict_latest(self, feature_frame: pd.DataFrame) -> np.ndarray:
        if feature_frame.empty:
            raise ValueError("empty feature frame")
        return self.predict_frame(feature_frame.iloc[[-1]])[0]


def save_lgbm(booster: lgb.Booster, adir: Path) -> Path:
    adir.mkdir(parents=True, exist_ok=True)
    p = adir / "model.txt"
    booster.save_model(str(p), num_iteration=booster.best_iteration)
    return p
