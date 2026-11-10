"""Feature and dataset contracts (B06: FeatureSnapshot, DatasetManifest)."""

from __future__ import annotations

import math
from typing import Any

from pydantic import Field, model_validator

from aqe import SCHEMA_VERSION
from aqe.contracts.core import (
    FeatureProfile,
    InstrumentKey,
    Provenance,
    QualityStatus,
    StrictModel,
    UTCDateTime,
)


class FeatureSnapshot(StrictModel):
    instrument: InstrumentKey
    as_of: UTCDateTime
    feature_profile: FeatureProfile
    schema_version: str = SCHEMA_VERSION
    feature_names: tuple[str, ...]
    values: tuple[float | None, ...]
    missing_mask: tuple[bool, ...]
    # latest available_at per source that contributed (must be <= as_of: T01)
    source_watermarks: dict[str, UTCDateTime]
    provenance: Provenance
    quality: QualityStatus
    quality_reasons: tuple[str, ...] = ()
    feature_schema_hash: str
    manifest_hash: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "FeatureSnapshot":
        n = len(self.feature_names)
        if len(self.values) != n or len(self.missing_mask) != n:
            raise ValueError("feature_names, values and missing_mask must have equal length")
        if len(set(self.feature_names)) != n:
            raise ValueError("duplicate feature names")
        for v, m in zip(self.values, self.missing_mask):
            if m and v is not None:
                raise ValueError("missing feature must have value None")
            if not m and (v is None or not math.isfinite(v)):
                raise ValueError("non-missing feature must be a finite float")
        for src, wm in self.source_watermarks.items():
            if wm > self.as_of:
                raise ValueError(f"source {src} watermark {wm} is after as_of {self.as_of} (future data)")
        return self


class FoldSpec(StrictModel):
    fold_id: str
    train_start: UTCDateTime
    # training rows must satisfy label_end_at <= train_label_end_max (purge)
    train_label_end_max: UTCDateTime
    oos_start: UTCDateTime
    oos_end: UTCDateTime  # exclusive
    embargo_seconds: int

    @model_validator(mode="after")
    def _check(self) -> "FoldSpec":
        if not (self.train_start < self.train_label_end_max <= self.oos_start < self.oos_end):
            raise ValueError("fold boundaries must satisfy train_start < train_label_end_max <= oos_start < oos_end")
        return self


class SegmentSpec(StrictModel):
    name: str  # dev | calibration | test
    start: UTCDateTime
    end: UTCDateTime  # exclusive


class SourceFileRef(StrictModel):
    path: str
    sha256: str
    rows: int | None = None


class DatasetManifest(StrictModel):
    dataset_id: str
    created_at: UTCDateTime
    schema_version: str = SCHEMA_VERSION
    instrument: InstrumentKey
    feature_profile: FeatureProfile
    bar_interval_seconds: int
    decision_delay_seconds: int
    feature_names: tuple[str, ...]
    feature_schema_hash: str
    horizons_seconds: tuple[int, ...]
    label_definition: dict[str, Any]
    data_start: UTCDateTime
    data_end: UTCDateTime
    provenance: tuple[Provenance, ...]
    segments: tuple[SegmentSpec, ...]
    folds: dict[str, tuple[FoldSpec, ...]] = Field(default_factory=dict)  # key: horizon seconds as str
    purge_rules: dict[str, Any]
    source_files: tuple[SourceFileRef, ...]
    n_rows: int
    config_hash: str
    code_version: str
    notes: tuple[str, ...] = ()
