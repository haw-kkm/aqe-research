"""Online (as-of) inference with registered artifacts: base models -> meta -> calibrator (B09).

Never fabricates a combination: if the trained meta/calibrator for the requested combo does not
exist, the ensemble ABSTAINs (T10). Context is combined only by a `numeric_context` meta that was
trained on structured context inputs; otherwise context is reported but not combined.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from aqe.contracts.context import ContextAssessment, context_usable_for_decision
from aqe.contracts.core import (
    READINESS_RANK,
    ClassProbabilities,
    ModelReadiness,
    PredictionKey,
    RuntimeMode,
)
from aqe.contracts.data import FeatureSnapshot
from aqe.contracts.predictions import (
    EnsemblePrediction,
    EnsembleStatus,
    ModelPrediction,
    OODStatus,
    PredictionStatus,
    ReadyAtBasis,
)
from aqe.models.chain import (
    calibration_chain_problems,
    calibration_validated_at,
    component_problems,
    established_at,
    readiness_at,
    verdict_at,
)
from aqe.models.registry import artifact_dir, load_metadata, load_registry

BASE_SLOTS = ("lgbm", "tcn")
ASSUMED_COMPUTE_LATENCY_S = 5.0


def _times(mode: RuntimeMode, as_of: datetime, interval_seconds: int):
    from aqe.contracts.core import utc_now

    generated = utc_now()
    if mode == RuntimeMode.REPLAY:
        ready, basis = as_of + timedelta(seconds=ASSUMED_COMPUTE_LATENCY_S), ReadyAtBasis.ASSUMED_LATENCY
    else:
        ready, basis = generated, ReadyAtBasis.WALL_CLOCK
    valid_until = max(as_of, ready) + timedelta(seconds=interval_seconds)
    return generated, ready, basis, valid_until


def _unavailable(kind: str, key: PredictionKey, reason: str, readiness: ModelReadiness, mode: RuntimeMode,
                 interval_seconds: int, status: PredictionStatus = PredictionStatus.UNAVAILABLE,
                 artifact_id: str | None = None) -> ModelPrediction:
    generated, ready, basis, valid_until = _times(mode, key.as_of, interval_seconds)
    return ModelPrediction(
        prediction_id=f"pred-{uuid.uuid4().hex[:12]}", key=key, model_kind=kind, artifact_id=artifact_id,
        train_label_cutoff=None, probabilities=None, generated_at=generated, ready_at=ready, ready_at_basis=basis,
        valid_until=valid_until, status=status, readiness=readiness, status_reason=reason,
    )


def _load_base(kind: str, adir, meta):
    if kind == "lgbm":
        from aqe.models.lgbm import LGBMPredictor

        return LGBMPredictor.load(adir, meta)
    if kind == "tcn":
        from aqe.models.tcn import TCNPredictor

        return TCNPredictor.load(adir, meta)
    raise ValueError(kind)


def predict_horizon(
    *,
    artifact_root,
    experiment_id: str,
    snapshot: FeatureSnapshot,
    feature_frame: pd.DataFrame,
    horizon_seconds: int,
    mode: RuntimeMode,
    interval_seconds: int,
    context: ContextAssessment | None,
) -> tuple[list[ModelPrediction], EnsemblePrediction, list[dict[str, Any]]]:
    key = PredictionKey(
        instrument=snapshot.instrument, as_of=snapshot.as_of, horizon_seconds=horizon_seconds,
        feature_profile=snapshot.feature_profile,
    )
    reg = load_registry(artifact_root, experiment_id).get("horizons", {}).get(str(horizon_seconds), {})
    preds: list[ModelPrediction] = []
    metas: list[dict[str, Any]] = []
    base_probs: dict[str, np.ndarray] = {}
    ident = dict(instrument_id=snapshot.instrument.instrument_id, horizon_seconds=horizon_seconds,
                 feature_profile=snapshot.feature_profile.value, feature_schema_hash=snapshot.feature_schema_hash,
                 feature_names=snapshot.feature_names, as_of=snapshot.as_of)
    for kind in BASE_SLOTS:
        aid = reg.get(kind)
        if not aid:
            preds.append(_unavailable(kind, key, "no trained artifact registered", ModelReadiness.IMPLEMENTED_UNTRAINED, mode, interval_seconds))
            continue
        meta = load_metadata(artifact_root, experiment_id, aid)
        r_at = readiness_at(meta, snapshot.as_of)
        probs = component_problems(meta, expected_kinds={kind}, **ident)
        if probs:
            preds.append(_unavailable(kind, key, "; ".join(probs), r_at, mode, interval_seconds, PredictionStatus.INVALID, aid))
            continue
        cutoff = pd.Timestamp(meta["train_label_cutoff"]).to_pydatetime()
        predictor = _load_base(kind, artifact_dir(artifact_root, experiment_id, aid), meta)
        try:
            p = predictor.predict_latest(feature_frame)
        except ValueError as exc:
            preds.append(_unavailable(kind, key, f"cannot predict: {exc}", r_at, mode, interval_seconds, PredictionStatus.INVALID, aid))
            continue
        generated, ready, basis, valid_until = _times(mode, key.as_of, interval_seconds)
        preds.append(ModelPrediction(
            prediction_id=f"pred-{uuid.uuid4().hex[:12]}", key=key, model_kind=kind, artifact_id=aid,
            train_label_cutoff=cutoff, probabilities=ClassProbabilities.from_array(p), generated_at=generated,
            ready_at=ready, ready_at_basis=basis, valid_until=valid_until, status=PredictionStatus.OK,
            readiness=r_at,
        ))
        metas.append(meta)
        base_probs[kind] = np.asarray(p, dtype="float64").reshape(1, 3)

    ctx_usable = context_usable_for_decision(context)
    combo = "numeric_context" if ctx_usable and reg.get("meta_numeric_context") else "numeric_only"
    meta_id, calib_id = reg.get(f"meta_{combo}"), reg.get(f"calib_{combo}")
    reasons: list[str] = []
    if ctx_usable and combo == "numeric_only":
        reasons_note = "context available but no trained numeric_context meta: context reported, not combined"
    else:
        reasons_note = None
    disagreement: dict[str, float] = {}
    if "lgbm" in base_probs and "tcn" in base_probs:
        a, b = base_probs["lgbm"][0], base_probs["tcn"][0]
        disagreement["max_abs_diff"] = float(np.max(np.abs(a - b)))
        m = 0.5 * (a + b)
        js = 0.5 * np.sum(a * np.log((a + 1e-12) / (m + 1e-12))) + 0.5 * np.sum(b * np.log((b + 1e-12) / (m + 1e-12)))
        disagreement["js_divergence"] = float(js)
    generated, ready, basis, valid_until = _times(mode, key.as_of, interval_seconds)
    common = dict(
        ensemble_id=f"ens-{uuid.uuid4().hex[:12]}", key=key, combo=combo,
        base_prediction_ids=tuple(p.prediction_id for p in preds),
        context_assessment_id=context.assessment_id if context is not None else None,
        disagreement=disagreement, missingness={"context": not ctx_usable, **{k: k not in base_probs for k in BASE_SLOTS}},
        generated_at=generated, ready_at=ready, valid_until=valid_until,
        extra={"note": reasons_note} if reasons_note else {},
    )
    if not meta_id or not calib_id:
        reasons.append(f"no trained meta/calibrator for combo '{combo}'")
        return preds, EnsemblePrediction(
            **common, meta_artifact_id=meta_id, calibrator_artifact_id=calib_id, probabilities=None,
            disagreement_threshold=None, ood_status=OODStatus.UNKNOWN, status=EnsembleStatus.ABSTAIN,
            abstain_reasons=tuple(reasons), readiness=ModelReadiness.IMPLEMENTED_UNTRAINED, calibration_validated=False,
        ), metas
    from aqe.ensemble.calibration import TemperatureCalibrator
    from aqe.ensemble.meta import MetaModel

    meta_meta = load_metadata(artifact_root, experiment_id, meta_id)
    calib_meta = load_metadata(artifact_root, experiment_id, calib_id)
    metas.extend([meta_meta, calib_meta])
    as_of = snapshot.as_of

    def _abstain(why: list[str]):
        return preds, EnsemblePrediction(
            **common, meta_artifact_id=meta_id, calibrator_artifact_id=calib_id, probabilities=None,
            disagreement_threshold=meta_meta.get("disagreement_threshold"), ood_status=OODStatus.UNKNOWN,
            status=EnsembleStatus.ABSTAIN, abstain_reasons=tuple(why),
            readiness=min(readiness_at(meta_meta, as_of), readiness_at(calib_meta, as_of), key=lambda r: READINESS_RANK[r]),
            calibration_validated=False,
        ), metas

    # F01: identity + point-in-time cutoff for meta and calibrator (same rules as base models)
    problems = component_problems(meta_meta, expected_kinds={"meta"}, **ident)
    problems += component_problems(calib_meta, expected_kinds={"calibrator"}, **ident)
    if problems:
        return _abstain(problems)
    mm = MetaModel.load(artifact_dir(artifact_root, experiment_id, meta_id), meta_meta)
    needed = [k for k in mm.base_kinds if k not in base_probs]
    if needed:
        return _abstain([f"meta requires base predictions that are unavailable: {needed}"])
    # F02: the calibrator must have been fitted on exactly these base/meta artifacts
    current = {"meta": meta_id, **{k: reg.get(k) for k in BASE_SLOTS}}
    chain = calibration_chain_problems(calib_meta, current, combo, mm.base_kinds)
    if chain:
        return _abstain(chain)
    regime = pd.DataFrame([dict(zip(snapshot.feature_names, snapshot.values))])
    raw = mm.predict_proba({k: base_probs[k] for k in mm.base_kinds}, regime)
    cal = TemperatureCalibrator.load(artifact_dir(artifact_root, experiment_id, calib_id), calib_meta)
    final = cal.apply(raw)[0]
    ood = mm.ood_status(regime)
    readiness = min((readiness_at(meta_meta, as_of), readiness_at(calib_meta, as_of),
                     *[p.readiness for p in preds if p.status == PredictionStatus.OK]), key=lambda r: READINESS_RANK[r])
    est = established_at(calib_meta)
    common["extra"] = {**common["extra"],
                       "oos_verdict": verdict_at(calib_meta.get("oos_verdict"), calib_meta, as_of),
                       "oos_cost_verdict": verdict_at(calib_meta.get("oos_cost_verdict"), calib_meta, as_of),
                       "validation_established_at": est.isoformat() if est else None,
                       "calibration_validated_basis": calib_meta.get("calibration_validated_basis")}
    return preds, EnsemblePrediction(
        **common, meta_artifact_id=meta_id, calibrator_artifact_id=calib_id,
        probabilities=ClassProbabilities.from_array(final), disagreement_threshold=meta_meta.get("disagreement_threshold"),
        ood_status=ood, status=EnsembleStatus.OK, readiness=readiness,
        calibration_validated=calibration_validated_at(calib_meta, as_of),
    ), metas
