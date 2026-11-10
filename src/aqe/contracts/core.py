"""Core contract types (B04, B06).

All timestamps are timezone-aware UTC. Class order is always [down, neutral, up].
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, field_validator, model_validator

from aqe import SCHEMA_VERSION

CLASS_ORDER: tuple[str, str, str] = ("down", "neutral", "up")
PROB_SUM_TOLERANCE = 1e-6


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    return value.astimezone(timezone.utc)


UTCDateTime = Annotated[datetime, AfterValidator(_require_utc)]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return _require_utc(value).isoformat().replace("+00:00", "Z")


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def to_ms(value: datetime) -> int:
    return int(round(_require_utc(value).timestamp() * 1000))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class Venue(StrEnum):
    BINANCE = "binance"


class MarketType(StrEnum):
    USDM_PERPETUAL = "usdm_perpetual"
    USDM_DELIVERY = "usdm_delivery"
    SPOT = "spot"


class Provenance(StrEnum):
    LIVE_CAPTURE = "LIVE_CAPTURE"
    VERIFIED_ARCHIVE = "VERIFIED_ARCHIVE"
    ASSUMED_LATENCY_REPLAY = "ASSUMED_LATENCY_REPLAY"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


class FeatureProfile(StrEnum):
    OHLCV_V0 = "ohlcv_v0"
    FLOW_V1 = "flow_v1"


class QualityStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"
    MISSING = "MISSING"


class ModelReadiness(StrEnum):
    IMPLEMENTED_UNTRAINED = "IMPLEMENTED_UNTRAINED"
    TRAINED_UNVALIDATED = "TRAINED_UNVALIDATED"
    OOS_EVALUATED = "OOS_EVALUATED"
    FORWARD_EVALUATED = "FORWARD_EVALUATED"


READINESS_RANK = {
    ModelReadiness.IMPLEMENTED_UNTRAINED: 0,
    ModelReadiness.TRAINED_UNVALIDATED: 1,
    ModelReadiness.OOS_EVALUATED: 2,
    ModelReadiness.FORWARD_EVALUATED: 3,
}


class RuntimeMode(StrEnum):
    RESEARCH = "research"
    REPLAY = "replay"
    SHADOW = "shadow"


class RuleOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class Disposition(StrEnum):
    REVIEW_CANDIDATE = "REVIEW_CANDIDATE"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    ABSTAIN = "ABSTAIN"


DISPOSITION_SEVERITY = {
    Disposition.REVIEW_CANDIDATE: 0,
    Disposition.RESEARCH_ONLY: 1,
    Disposition.ABSTAIN: 2,
}


def most_restrictive(*items: Disposition) -> Disposition:
    return max(items, key=lambda d: DISPOSITION_SEVERITY[d])


class InstrumentKey(StrictModel):
    venue: Venue
    market_type: MarketType
    instrument_id: str

    @field_validator("instrument_id")
    @classmethod
    def _upper(cls, v: str) -> str:
        if not v or v != v.upper() or not v.isalnum():
            raise ValueError("instrument_id must be upper-case alphanumeric, e.g. BTCUSDT")
        return v

    def slug(self) -> str:
        return f"{self.venue.value}.{self.market_type.value}.{self.instrument_id}"


class PredictionKey(StrictModel):
    """Join key for predictions (B04): venue, market_type, instrument_id, as_of,
    horizon_seconds, feature_profile, schema_version."""

    instrument: InstrumentKey
    as_of: UTCDateTime
    horizon_seconds: int
    feature_profile: FeatureProfile
    schema_version: str = SCHEMA_VERSION

    @field_validator("horizon_seconds")
    @classmethod
    def _pos(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("horizon_seconds must be positive")
        return v


class ClassProbabilities(StrictModel):
    """Probabilities in fixed class order [down, neutral, up]."""

    p_down: float
    p_neutral: float
    p_up: float

    @model_validator(mode="after")
    def _check(self) -> "ClassProbabilities":
        vals = (self.p_down, self.p_neutral, self.p_up)
        for v in vals:
            if not math.isfinite(v):
                raise ValueError("probabilities must be finite")
            if v < 0.0 or v > 1.0:
                raise ValueError("probabilities must be within [0, 1]")
        if abs(sum(vals) - 1.0) > PROB_SUM_TOLERANCE:
            raise ValueError(f"probabilities must sum to 1 (got {sum(vals):.9f})")
        return self

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.p_down, self.p_neutral, self.p_up)

    @classmethod
    def from_array(cls, arr) -> "ClassProbabilities":
        vals = [float(x) for x in arr]
        if len(vals) != 3:
            raise ValueError("expected 3 probabilities in order [down, neutral, up]")
        s = sum(vals)
        if not math.isfinite(s) or s <= 0:
            raise ValueError("invalid probability vector")
        # renormalise tiny float drift only; large deviations are rejected by the validator
        if abs(s - 1.0) <= 1e-4:
            vals = [v / s for v in vals]
        return cls(p_down=vals[0], p_neutral=vals[1], p_up=vals[2])


def expiry(start: datetime, seconds: float) -> datetime:
    return _require_utc(start) + timedelta(seconds=seconds)
