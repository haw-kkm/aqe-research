"""Validated contracts (B06). `export_json_schemas` writes JSON Schema files."""

from __future__ import annotations

import json
from pathlib import Path

from aqe.contracts.context import ContextAssessment, EvidenceBundle, EvidenceItem
from aqe.contracts.core import CLASS_ORDER, ClassProbabilities, InstrumentKey, PredictionKey
from aqe.contracts.data import DatasetManifest, FeatureSnapshot
from aqe.contracts.predictions import EnsemblePrediction, ModelPrediction
from aqe.contracts.risk import PortfolioSnapshot, RiskDecision

CONTRACTS = {
    "FeatureSnapshot": FeatureSnapshot,
    "DatasetManifest": DatasetManifest,
    "ModelPrediction": ModelPrediction,
    "EvidenceItem": EvidenceItem,
    "EvidenceBundle": EvidenceBundle,
    "ContextAssessment": ContextAssessment,
    "EnsemblePrediction": EnsemblePrediction,
    "RiskDecision": RiskDecision,
    "PortfolioSnapshot": PortfolioSnapshot,
}


def export_json_schemas(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in CONTRACTS.items():
        p = out_dir / f"{name}.schema.json"
        p.write_text(json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(p)
    return written


__all__ = [
    "CLASS_ORDER",
    "CONTRACTS",
    "ClassProbabilities",
    "ContextAssessment",
    "DatasetManifest",
    "EnsemblePrediction",
    "EvidenceBundle",
    "EvidenceItem",
    "FeatureSnapshot",
    "InstrumentKey",
    "ModelPrediction",
    "PortfolioSnapshot",
    "PredictionKey",
    "RiskDecision",
    "export_json_schemas",
]
