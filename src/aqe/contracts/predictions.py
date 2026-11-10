"""Prediction contracts (B06: ModelPrediction, EnsemblePrediction)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import model_validator

from aqe.contracts.core import (
    ClassProbabilities,
    ModelReadiness,
    PredictionKey,
    StrictModel,
    UTCDateTime,
)


class PredictionStatus(StrEnum):
    OK = "OK"
    INVALID = "INVALID"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class ReadyAtBasis(StrEnum):
    WALL_CLOCK = "WALL_CLOCK"  # ready_at == generated_at, measured live
    ASSUMED_LATENCY = "ASSUMED_LATENCY"  # replay: ready_at = as_of + assumed compute latency


class ModelPrediction(StrictModel):
    prediction_id: str
    key: PredictionKey
    model_kind: str  # lgbm | tcn | baseline_prior
    artifact_id: str | None
    train_label_cutoff: UTCDateTime | None
    probabilities: ClassProbabilities | None
    # expected return is unknown unless a separately trained magnitude model exists (B07.1)
    expected_return: float | None = None
    expected_return_status: str = "unknown"
    generated_at: UTCDateTime
    ready_at: UTCDateTime
    ready_at_basis: ReadyAtBasis
    valid_until: UTCDateTime
    status: PredictionStatus
    readiness: ModelReadiness
    status_reason: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "ModelPrediction":
        if self.status == PredictionStatus.OK and self.probabilities is None:
            raise ValueError("OK prediction must carry probabilities")
        if self.train_label_cutoff is not None and self.train_label_cutoff > self.key.as_of:
            raise ValueError("model trained on labels that end after as_of (future leakage)")
        if self.valid_until <= self.key.as_of:
            raise ValueError("valid_until must be after as_of")
        if self.expected_return is not None and self.expected_return_status == "unknown":
            raise ValueError("expected_return given but status says unknown")
        return self


class OODStatus(StrEnum):
    IN_DISTRIBUTION = "IN_DISTRIBUTION"
    OUT_OF_DISTRIBUTION = "OUT_OF_DISTRIBUTION"
    UNKNOWN = "UNKNOWN"


class EnsembleStatus(StrEnum):
    OK = "OK"
    ABSTAIN = "ABSTAIN"


class EnsemblePrediction(StrictModel):
    ensemble_id: str
    key: PredictionKey
    combo: str  # numeric_only | numeric_context
    base_prediction_ids: tuple[str, ...]
    context_assessment_id: str | None
    meta_artifact_id: str | None
    calibrator_artifact_id: str | None
    probabilities: ClassProbabilities | None
    disagreement: dict[str, float]
    disagreement_threshold: float | None  # OOS-derived; None => not validated
    ood_status: OODStatus
    missingness: dict[str, bool]
    status: EnsembleStatus
    abstain_reasons: tuple[str, ...] = ()
    readiness: ModelReadiness
    calibration_validated: bool
    generated_at: UTCDateTime
    ready_at: UTCDateTime
    valid_until: UTCDateTime
    extra: dict[str, Any] = {}

    @model_validator(mode="after")
    def _check(self) -> "EnsemblePrediction":
        if self.status == EnsembleStatus.OK and self.probabilities is None:
            raise ValueError("OK ensemble must carry probabilities")
        if self.status == EnsembleStatus.ABSTAIN and not self.abstain_reasons:
            raise ValueError("ABSTAIN ensemble must state reasons")
        return self
