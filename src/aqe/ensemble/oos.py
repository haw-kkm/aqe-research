"""Walk-forward OOS generation (`aqe build-oos`) and final base training (`aqe train-lgbm/-tcn`) (B07, B09).

For every fold the model is re-fitted from scratch (incl. TCN normalizer) on rows whose labels
matured before the fold's OOS start; predictions are produced only for that fold's OOS window.
The final base model is trained on rows whose labels matured before calibration_start.
"""

from __future__ import annotations

import platform
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from aqe import __version__
from aqe.config import ExperimentConfig, LocalConfig
from aqe.contracts.core import CLASS_ORDER, ModelReadiness, iso_utc, utc_now
from aqe.datasets.build import assert_purged, load_dataset, manifest_hash, rows_for_training
from aqe.models.registry import artifact_dir, exp_dir, load_metadata, load_registry, save_metadata, update_registry
from aqe.storage import atomic_write_json
from aqe.storage.runs import EXIT_NOT_READY, EXIT_OK, RunRecord


def _versions() -> dict[str, str]:
    import lightgbm
    import sklearn
    import torch

    return {"lightgbm": lightgbm.__version__, "torch": torch.__version__, "scikit-learn": sklearn.__version__,
            "numpy": np.__version__, "pandas": pd.__version__, "python": platform.python_version()}


def _meta(kind: str, role: str, exp: ExperimentConfig, man, h: int, artifact_id: str, info: dict[str, Any],
          cfg_hash: str, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id, "kind": kind, "role": role, "experiment_id": exp.experiment_id,
        "instrument_id": exp.instrument.instrument_id, "venue": exp.instrument.venue.value,
        "market_type": exp.instrument.market_type.value, "feature_profile": man.feature_profile.value,
        "feature_schema_hash": man.feature_schema_hash, "feature_names": list(man.feature_names),
        "horizon_seconds": h, "class_order": list(CLASS_ORDER), "train_label_cutoff": info["train_label_cutoff"],
        "dataset_id": man.dataset_id, "dataset_manifest_hash": manifest_hash(man), "label_definition": man.label_definition,
        "config_hash": cfg_hash, "code_version": __version__, "dependency_versions": _versions(), "seed": exp.seed,
        "readiness": ModelReadiness.TRAINED_UNVALIDATED.value, "created_at": iso_utc(utc_now()), "training": info, **extra,
    }


def _aid(kind: str, h: int, tag: str) -> str:
    return f"{kind}-h{h}-{tag}-{utc_now().strftime('%Y%m%dT%H%M%S')}"


def fit_and_save(kind: str, role: str, df: pd.DataFrame, h: int, train: pd.DataFrame, exp: ExperimentConfig,
                 cfg: LocalConfig, man, cfg_hash: str, tag: str, log=print) -> tuple[str, Any]:
    features = list(man.feature_names)
    aid = _aid(kind, h, tag)
    adir = artifact_dir(cfg.paths.artifact_root, exp.experiment_id, aid)
    if kind == "lgbm":
        from aqe.models.lgbm import LGBMPredictor, save_lgbm, train_lgbm

        booster, info = train_lgbm(train, h, features, exp.lgbm, exp.seed)
        save_lgbm(booster, adir)
        extra = {"params": exp.lgbm.params, "num_boost_round": exp.lgbm.num_boost_round}
        meta = _meta(kind, role, exp, man, h, aid, info, cfg_hash, extra)
        save_metadata(cfg.paths.artifact_root, exp.experiment_id, meta)
        return aid, LGBMPredictor(booster, features)
    if kind == "tcn":
        from aqe.models.tcn import TCNPredictor, save_tcn, train_tcn

        mask = np.zeros(len(df), dtype=bool)
        mask[df.index.get_indexer(train.index)] = True
        model, norm, info = train_tcn(df, mask, h, features, exp.tcn, exp.seed, log=log)
        save_tcn(model, norm, adir)
        arch = {"window": exp.tcn.window, "channels": exp.tcn.channels, "kernel_size": exp.tcn.kernel_size,
                "dilations": exp.tcn.dilations, "dropout": exp.tcn.dropout, "levels": exp.tcn.levels,
                "receptive_field": model.receptive_field()}
        meta = _meta(kind, role, exp, man, h, aid, info, cfg_hash, {"architecture": arch, "train_config": exp.tcn.model_dump()})
        save_metadata(cfg.paths.artifact_root, exp.experiment_id, meta)
        return aid, TCNPredictor(model, norm, features, exp.tcn.window)
    raise ValueError(kind)


def predict_rows(kind: str, predictor, df: pd.DataFrame, rows: pd.Index) -> np.ndarray:
    if kind == "lgbm":
        return predictor.predict_frame(df.loc[rows])
    pos = df.index.get_indexer(rows)
    return predictor.predict_grid(df, pos)


def load_predictor(cfg: LocalConfig, exp: ExperimentConfig, aid: str):
    meta = load_metadata(cfg.paths.artifact_root, exp.experiment_id, aid)
    adir = artifact_dir(cfg.paths.artifact_root, exp.experiment_id, aid)
    if meta["kind"] == "lgbm":
        from aqe.models.lgbm import LGBMPredictor

        return meta, LGBMPredictor.load(adir, meta)
    from aqe.models.tcn import TCNPredictor

    return meta, TCNPredictor.load(adir, meta)


def oos_path(cfg: LocalConfig, exp: ExperimentConfig, kind: str, h: int) -> Path:
    return exp_dir(cfg.paths.artifact_root, exp.experiment_id) / "oos" / f"{kind}_h{h}.parquet"


def run_build_oos(exp: ExperimentConfig, cfg: LocalConfig, cfg_hash: str, rec: RunRecord, models: list[str],
                  horizons: list[int] | None = None) -> int:
    df, man = load_dataset(cfg, exp)
    horizons = horizons or list(exp.horizons_seconds)
    summary: dict[str, Any] = {}
    for h in horizons:
        folds = man.folds[str(h)]
        for kind in models:
            parts, fold_log = [], []
            for fold in folds:
                train = rows_for_training(df, h, fold.train_start, fold.train_label_end_max)
                assert_purged(train, h, fold.oos_start)  # T05
                oos_rows = df.index[(df["as_of"] >= pd.Timestamp(fold.oos_start)) & (df["as_of"] < pd.Timestamp(fold.oos_end)) & df["bar_present"]]
                print(f"[build-oos] {kind} h={h} {fold.fold_id}: train {len(train)} rows -> OOS {len(oos_rows)} rows", flush=True)
                aid, pred = fit_and_save(kind, "fold_model", df, h, train, exp, cfg, man, cfg_hash, fold.fold_id)
                p = predict_rows(kind, pred, df, oos_rows)
                part = pd.DataFrame({"open_time": df.loc[oos_rows, "open_time"].to_numpy(), "as_of": df.loc[oos_rows, "as_of"].to_numpy(),
                                     "fold_id": fold.fold_id, "artifact_id": aid,
                                     "p_down": p[:, 0], "p_neutral": p[:, 1], "p_up": p[:, 2],
                                     "y": df.loc[oos_rows, f"y_{h}"].to_numpy(),
                                     "label_end_at": df.loc[oos_rows, f"label_end_at_{h}"].to_numpy()})
                parts.append(part)
                meta = load_metadata(cfg.paths.artifact_root, exp.experiment_id, aid)
                fold_log.append({"fold_id": fold.fold_id, "artifact_id": aid, "train_rows": int(len(train)),
                                 "train_label_end_max_observed": str(train[f"label_end_at_{h}"].max()),
                                 "oos_start": iso_utc(fold.oos_start), "oos_end": iso_utc(fold.oos_end),
                                 "oos_rows": int(len(oos_rows)), "oos_predicted": int(np.isfinite(p[:, 0]).sum()),
                                 "leak_check": "PASS", "training": {k: v for k, v in meta["training"].items() if k != "importance_gain"}})
            out = pd.concat(parts, ignore_index=True)
            path = oos_path(cfg, exp, kind, h)
            path.parent.mkdir(parents=True, exist_ok=True)
            out.to_parquet(path, index=False)
            ok = out.dropna(subset=["p_down", "y"])
            ll = float(-np.mean(np.log(np.clip(ok[["p_down", "p_neutral", "p_up"]].to_numpy()[np.arange(len(ok)), ok["y"].astype(int)], 1e-12, 1))))
            atomic_write_json(path.with_suffix(".manifest.json"), {
                "kind": kind, "horizon_seconds": h, "dataset_id": man.dataset_id, "dataset_manifest_hash": manifest_hash(man),
                "config_hash": cfg_hash, "folds": fold_log, "rows": int(len(out)), "rows_with_pred_and_label": int(len(ok)),
                "oos_logloss_dev_diagnostic": ll, "created_at": iso_utc(utc_now()),
                "note": "Development-period OOS (used to train the meta model). Not the locked final test.",
            })
            summary[f"{kind}_h{h}"] = {"rows": int(len(out)), "dev_oos_logloss": round(ll, 5), "folds": len(folds)}
    rec.outputs["oos"] = summary
    return EXIT_OK


def run_train_final(exp: ExperimentConfig, cfg: LocalConfig, cfg_hash: str, rec: RunRecord, kind: str,
                    horizons: list[int] | None = None) -> int:
    df, man = load_dataset(cfg, exp)
    horizons = horizons or list(exp.horizons_seconds)
    boundary = (pd.Timestamp(exp.segments.calibration_start) - pd.Timedelta(seconds=exp.segments.embargo_seconds)).to_pydatetime()
    out = {}
    for h in horizons:
        train = rows_for_training(df, h, exp.segments.dev_start, boundary)
        assert_purged(train, h, boundary)
        if len(train) < 10_000:
            rec.notes.append(f"h={h}: not enough training rows ({len(train)})")
            return EXIT_NOT_READY
        print(f"[train-final] {kind} h={h}: {len(train)} rows, labels end <= {boundary}", flush=True)
        aid, _ = fit_and_save(kind, "final", df, h, train, exp, cfg, man, cfg_hash, "final")
        update_registry(cfg.paths.artifact_root, exp.experiment_id, h, kind, aid)
        meta = load_metadata(cfg.paths.artifact_root, exp.experiment_id, aid)
        out[str(h)] = {"artifact_id": aid, "train_rows": len(train), "train_label_cutoff": meta["train_label_cutoff"],
                       "readiness": meta["readiness"],
                       "val": {k: meta["training"].get(k) for k in ("best_iteration", "val_multi_logloss_best", "best_val_logloss")}}
    rec.outputs[f"final_{kind}"] = out
    rec.outputs["registry"] = load_registry(cfg.paths.artifact_root, exp.experiment_id)
    return EXIT_OK


_ = datetime
