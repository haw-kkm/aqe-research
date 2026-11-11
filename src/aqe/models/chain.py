"""Shared artifact validation for base models, meta models and calibrators (review F01/F02).

Used by online inference (`ensemble.online`), evaluation (`evaluation.evaluate`) and the risk
engine, so every layer applies the same rules:

* identity: instrument, horizon, feature profile, feature schema hash, feature order, class order;
* point-in-time: `train_label_cutoff <= as_of` for EVERY component (base, meta, calibrator);
* lineage: a calibrator may only be applied to the exact base/meta artifacts it was fitted on;
* knowledge time: a validation result (OOS verdict, calibration_validated, OOS_EVALUATED readiness)
  counts only if it was established (its evidence segment ended) at or before `as_of`.
  Negative/blocking verdicts may still be applied to earlier as_of (conservative), positive ones not.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pandas as pd

from aqe.contracts.core import READINESS_RANK, ModelReadiness
from aqe.models.registry import ArtifactMismatch, check_identity

_EVIDENCE_SEGMENT = re.compile(r"locked test (\S+?)\.\.(\S+?) run")
POSITIVE_VERDICTS = {"SKILL_DEMONSTRATED", "NET_POSITIVE"}
NOT_ESTABLISHED = "NOT_ESTABLISHED_AT_AS_OF"


def _ts(value) -> datetime:
    return pd.Timestamp(value).to_pydatetime()


def component_problems(meta: dict[str, Any], *, expected_kinds: set[str], instrument_id: str, horizon_seconds: int,
                       feature_profile: str, feature_schema_hash: str, feature_names, as_of: datetime) -> list[str]:
    """Empty list = usable at as_of. Otherwise human-readable reasons (never silently accepted)."""
    aid = meta.get("artifact_id", "?")
    probs: list[str] = []
    if meta.get("kind") not in expected_kinds:
        probs.append(f"{aid}: kind {meta.get('kind')!r} not in {sorted(expected_kinds)}")
    try:
        check_identity(meta, instrument_id=instrument_id, horizon_seconds=horizon_seconds, feature_profile=feature_profile,
                       feature_schema_hash=feature_schema_hash, feature_names=feature_names)
    except (ArtifactMismatch, KeyError) as exc:
        probs.append(f"{aid}: identity mismatch ({exc})")
    cutoff = meta.get("train_label_cutoff")
    if not cutoff:
        probs.append(f"{aid}: no train_label_cutoff recorded")
    elif _ts(cutoff) > as_of:
        probs.append(f"{aid}: trained on labels ending {cutoff} after as_of {as_of.isoformat()} (future leakage)")
    return probs


def required_lineage(combo: str, base_kinds: list[str]) -> set[str]:
    if combo in ("numeric_only", "numeric_context"):
        return {"meta", *base_kinds}
    if combo.endswith("_only"):
        return {combo[: -len("_only")]}
    raise ValueError(f"unknown combo {combo}")


def calibration_chain_problems(calib_meta: dict[str, Any], current: dict[str, str | None], combo: str,
                               base_kinds: list[str]) -> list[str]:
    """The calibrator must have been fitted on exactly the currently used base/meta artifacts."""
    aid = calib_meta.get("artifact_id", "?")
    fitted = calib_meta.get("fitted_on_artifacts")
    if not isinstance(fitted, dict) or not fitted:
        return [f"{aid}: calibrator has no fitted_on_artifacts lineage (cannot verify chain)"]
    if calib_meta.get("combo") not in (None, combo):
        return [f"{aid}: calibrator combo {calib_meta.get('combo')} != {combo}"]
    probs = []
    for k in sorted(required_lineage(combo, base_kinds)):
        if k not in fitted:
            probs.append(f"{aid}: lineage lacks '{k}'")
        elif fitted[k] != current.get(k):
            probs.append(f"{aid}: fitted on {k}={fitted[k]} but current {k}={current.get(k)}")
    return probs


def established_at(meta: dict[str, Any]) -> datetime | None:
    """When the artifact's OOS validation became knowable (end of the evidence segment)."""
    times = []
    for entry in meta.get("readiness_history", []) or []:
        if READINESS_RANK.get(ModelReadiness(entry.get("readiness")), 0) < READINESS_RANK[ModelReadiness.OOS_EVALUATED]:
            continue
        if entry.get("established_at"):
            times.append(_ts(entry["established_at"]))
            continue
        m = _EVIDENCE_SEGMENT.search(str(entry.get("evidence", "")))  # legacy entries (before 2026-11-11 fix)
        if m:
            times.append(_ts(m.group(2)))
    return min(times) if times else None


def readiness_at(meta: dict[str, Any], as_of: datetime) -> ModelReadiness:
    r = ModelReadiness(meta.get("readiness", ModelReadiness.IMPLEMENTED_UNTRAINED.value))
    if READINESS_RANK[r] >= READINESS_RANK[ModelReadiness.OOS_EVALUATED]:
        est = established_at(meta)
        if est is None or est > as_of:
            return ModelReadiness.TRAINED_UNVALIDATED
    return r


def verdict_at(value: str | None, meta: dict[str, Any], as_of: datetime) -> str | None:
    """Positive verdicts count only if established by as_of; blocking ones are always applied."""
    if value in POSITIVE_VERDICTS:
        est = established_at(meta)
        if est is None or est > as_of:
            return NOT_ESTABLISHED
    return value


def calibration_validated_at(meta: dict[str, Any], as_of: datetime) -> bool:
    if not meta.get("calibration_validated"):
        return False
    est = established_at(meta)
    return est is not None and est <= as_of
