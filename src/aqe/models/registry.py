"""Artifact registry (B06 artifact rules).

Layout:
  {artifact_root}/{experiment_id}/registry.json      active artifact ids per horizon & combo
  {artifact_root}/{experiment_id}/artifacts/{artifact_id}/metadata.json (+ model files)

An artifact is never usable from its model file alone: metadata must carry dataset manifest hash,
feature profile/schema hash, horizon, label cutoff, class order, code/config hashes, seed,
normalizer, readiness.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aqe.contracts.core import CLASS_ORDER, ModelReadiness
from aqe.storage import atomic_write_json, read_json

REQUIRED_META_KEYS = (
    "artifact_id", "kind", "experiment_id", "instrument_id", "feature_profile", "feature_schema_hash",
    "feature_names", "horizon_seconds", "class_order", "train_label_cutoff", "dataset_manifest_hash",
    "config_hash", "code_version", "seed", "readiness", "created_at",
)


class ArtifactMismatch(ValueError):
    """Raised when an artifact does not match the requested identity (T06)."""


def exp_dir(artifact_root: Path, experiment_id: str) -> Path:
    return Path(artifact_root) / experiment_id


def artifact_dir(artifact_root: Path, experiment_id: str, artifact_id: str) -> Path:
    return exp_dir(artifact_root, experiment_id) / "artifacts" / artifact_id


def save_metadata(artifact_root: Path, experiment_id: str, meta: dict[str, Any]) -> Path:
    missing = [k for k in REQUIRED_META_KEYS if k not in meta]
    if missing:
        raise ValueError(f"artifact metadata missing keys: {missing}")
    if tuple(meta["class_order"]) != CLASS_ORDER:
        raise ValueError("class order must be [down, neutral, up]")
    ModelReadiness(meta["readiness"])
    d = artifact_dir(artifact_root, experiment_id, meta["artifact_id"])
    d.mkdir(parents=True, exist_ok=True)
    p = d / "metadata.json"
    atomic_write_json(p, meta)
    return p


def load_metadata(artifact_root: Path, experiment_id: str, artifact_id: str) -> dict[str, Any]:
    return read_json(artifact_dir(artifact_root, experiment_id, artifact_id) / "metadata.json")


def check_identity(meta: dict[str, Any], *, instrument_id: str, horizon_seconds: int, feature_profile: str,
                   feature_schema_hash: str, feature_names: tuple[str, ...] | list[str] | None = None) -> None:
    if meta["instrument_id"] != instrument_id:
        raise ArtifactMismatch(f"instrument {meta['instrument_id']} != {instrument_id}")
    if int(meta["horizon_seconds"]) != int(horizon_seconds):
        raise ArtifactMismatch(f"horizon {meta['horizon_seconds']} != {horizon_seconds}")
    if meta["feature_profile"] != feature_profile:
        raise ArtifactMismatch(f"feature profile {meta['feature_profile']} != {feature_profile}")
    if meta["feature_schema_hash"] != feature_schema_hash:
        raise ArtifactMismatch("feature schema hash mismatch")
    if feature_names is not None and list(meta["feature_names"]) != list(feature_names):
        raise ArtifactMismatch("feature order/name mismatch")
    if tuple(meta["class_order"]) != CLASS_ORDER:
        raise ArtifactMismatch("class order mismatch")


def registry_path(artifact_root: Path, experiment_id: str) -> Path:
    return exp_dir(artifact_root, experiment_id) / "registry.json"


def load_registry(artifact_root: Path, experiment_id: str) -> dict[str, Any]:
    p = registry_path(artifact_root, experiment_id)
    if not p.exists():
        return {"experiment_id": experiment_id, "horizons": {}}
    return read_json(p)


def update_registry(artifact_root: Path, experiment_id: str, horizon_seconds: int, slot: str, artifact_id: str | None) -> None:
    reg = load_registry(artifact_root, experiment_id)
    h = reg.setdefault("horizons", {}).setdefault(str(horizon_seconds), {})
    if artifact_id is None:
        h.pop(slot, None)
    else:
        h[slot] = artifact_id
    atomic_write_json(registry_path(artifact_root, experiment_id), reg)


def set_readiness(artifact_root: Path, experiment_id: str, artifact_id: str, readiness: ModelReadiness, evidence: str) -> None:
    meta = load_metadata(artifact_root, experiment_id, artifact_id)
    meta["readiness"] = readiness.value
    meta.setdefault("readiness_history", []).append({"readiness": readiness.value, "evidence": evidence})
    save_metadata(artifact_root, experiment_id, meta)
