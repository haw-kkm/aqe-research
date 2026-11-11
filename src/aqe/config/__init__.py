"""Configuration models and loader (B03 config/). Paths come from YAML, secrets from env only."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from aqe.contracts.core import FeatureProfile, InstrumentKey, RuntimeMode, UTCDateTime


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Cfg):
    data_root: Path
    artifact_root: Path


class BinanceConfig(_Cfg):
    rest_base_url: str = "https://fapi.binance.com"
    # WebSocket bases are configurable because Binance split /public and /market paths (B05.2).
    ws_public_base: str = "wss://fstream.binance.com/public"
    ws_market_base: str = "wss://fstream.binance.com/market"
    http_timeout_seconds: float = 10.0
    max_retries: int = 6
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 30.0
    max_weight_per_minute: int = 1200  # self-imposed budget below the exchange limit


class BackfillConfig(_Cfg):
    kline_interval: str = "5m"
    start: UTCDateTime
    end: UTCDateTime | None = None
    assumed_latency_seconds: float = 2.0
    funding_history: bool = True
    oi_hist_period: str = "5m"
    oi_hist: bool = True


class CollectorConfig(_Cfg):
    depth_speed: str = "100ms"
    snapshot_limit: int = 1000
    rotate_max_mb: int = 64
    rotate_max_minutes: int = 60
    status_interval_seconds: int = 15
    oi_poll_seconds: int = 60
    flow_summary_seconds: int = 60
    clock_check_seconds: int = 300


class DatabaseConfig(_Cfg):
    postgres_dsn_env: str = "AQE_PG_DSN"
    sqlite_relpath: str = "meta/aqe_meta.sqlite"


class ClockConfig(_Cfg):
    max_abs_skew_ms: float = 1000.0


class LLMConfig(_Cfg):
    provider: str | None = None  # e.g. openai | anthropic | ollama ; None = not configured
    model_id: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    temperature: float = 0.0
    max_output_tokens: int = 2000
    max_prompt_tokens_per_call: int = 8000  # upper bound used only for pre-call cost reservation
    usd_per_1k_prompt_tokens: float | None = None  # explicit price table; None => cost unknown
    usd_per_1k_completion_tokens: float | None = None


class TradingAgentsConfig(_Cfg):
    upstream_sha: str = "9fa1196fffbcfd9de5146d2dd2869df84ab99838"
    worker_python: Path | None = None  # separate venv interpreter for the TA worker
    selected_analysts: list[str] = Field(default_factory=lambda: ["market", "news"])
    max_debate_rounds: int = 1
    max_risk_discuss_rounds: int = 1


class ContextConfig(_Cfg):
    backend: str = "none"  # none | offline_fixture | simple_llm | tradingagents
    ttl_seconds: int = 1800
    lease_seconds: int = 900
    job_timeout_seconds: int = 600
    max_attempts: int = 2
    concurrency: int = 1
    max_calls_per_day: int = 200
    max_llm_calls_per_job: int = 40  # TradingAgents graph LLM calls per job; the bridge aborts beyond this
    max_cost_usd_per_day: float = 0.0  # 0 => no paid calls allowed
    evidence_root: str = "evidence"
    llm: LLMConfig = LLMConfig()
    tradingagents: TradingAgentsConfig = TradingAgentsConfig()


class AnalysisConfig(_Cfg):
    experiment_config: Path
    decision_delay_seconds: int = 10
    portfolio_snapshot: Path | None = None


class LocalConfig(_Cfg):
    schema_version: str = "1.0.0"
    mode: RuntimeMode = RuntimeMode.RESEARCH
    paths: PathsConfig
    instruments: list[InstrumentKey]
    binance: BinanceConfig = BinanceConfig()
    backfill: BackfillConfig
    collector: CollectorConfig = CollectorConfig()
    database: DatabaseConfig = DatabaseConfig()
    clock: ClockConfig = ClockConfig()
    context: ContextConfig = ContextConfig()
    risk_ruleset: Path
    analysis: AnalysisConfig
    fixture_bars: Path | None = None  # SYNTHETIC_FIXTURE input for M0 path

    @field_validator("instruments")
    @classmethod
    def _nonempty(cls, v):
        if not v:
            raise ValueError("at least one instrument required")
        return v


class LabelConfig(_Cfg):
    kind: str = "vol_scaled_3class"
    vol_window_bars: int = 288
    cost_floor_bps: float = 10.0
    target_neutral_fraction: float = 0.34  # used to fit k on the initial training window only


class SegmentsConfig(_Cfg):
    dev_start: UTCDateTime
    first_oos_start: UTCDateTime
    fold_months: int = 6
    calibration_start: UTCDateTime
    test_start: UTCDateTime
    test_end: UTCDateTime | None = None
    embargo_seconds: int = 0


class LGBMConfig(_Cfg):
    params: dict[str, Any] = Field(
        default_factory=lambda: {
            "objective": "multiclass",
            "num_class": 3,
            "num_leaves": 31,
            "max_depth": 6,
            "learning_rate": 0.05,
            "min_data_in_leaf": 200,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 1,
            "lambda_l2": 1.0,
            "verbosity": -1,
            "num_threads": 4,
        }
    )
    num_boost_round: int = 2000
    early_stopping_rounds: int = 100
    val_fraction: float = 0.15
    train_stride: int = 1


class TCNConfig(_Cfg):
    window: int = 256
    channels: int = 32
    levels: int = 6
    kernel_size: int = 3
    dilations: list[int] = Field(default_factory=lambda: [1, 2, 4, 8, 16, 32])
    dropout: float = 0.1
    lr: float = 1e-3
    weight_decay: float = 1e-4
    batch_size: int = 256
    max_epochs: int = 8
    patience: int = 2
    grad_clip: float = 1.0
    train_stride: int = 3
    eval_stride: int = 1
    val_fraction: float = 0.15
    max_train_samples: int | None = 150000
    device: str = "auto"
    num_threads: int = 6


class MetaConfig(_Cfg):
    C: float = 0.5
    max_iter: int = 2000


class CalibrationConfig(_Cfg):
    method: str = "temperature"
    min_independent_samples: int = 100  # counted as non-overlapping label windows


class EvaluationConfig(_Cfg):
    taker_fee_bps: float = 5.0
    slippage_bps: float = 2.0
    policy_min_edge: float = 0.10  # p_dir - p_opposite threshold for the fixed virtual policy
    policy_min_prob: float = 0.40
    bootstrap_block_hours: int = 72
    bootstrap_samples: int = 500


class ExperimentConfig(_Cfg):
    schema_version: str = "1.0.0"
    experiment_id: str
    base_config: Path
    instrument: InstrumentKey
    feature_profile: FeatureProfile = FeatureProfile.OHLCV_V0
    bar_interval_seconds: int = 300
    decision_delay_seconds: int = 10
    horizons_seconds: list[int] = Field(default_factory=lambda: [3600, 86400])
    label: LabelConfig = LabelConfig()
    segments: SegmentsConfig
    lgbm: LGBMConfig = LGBMConfig()
    tcn: TCNConfig = TCNConfig()
    meta: MetaConfig = MetaConfig()
    calibration: CalibrationConfig = CalibrationConfig()
    evaluation: EvaluationConfig = EvaluationConfig()
    seed: int = 20261111


def _canonical(obj: BaseModel) -> str:
    return json.dumps(obj.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)


def config_hash(obj: BaseModel) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def _resolve_rel(p: Path | None, base: Path) -> Path | None:
    if p is None:
        return None
    expanded = os.path.expandvars(str(p))
    if "${" in expanded or "%" in expanded:
        raise ValueError(f"unresolved environment variable in path: {p}")
    p = Path(expanded)
    return p if p.is_absolute() else (base / p)


def project_root_from(config_path: Path) -> Path:
    # configs/<file>.yaml -> project root
    cp = config_path.resolve()
    return cp.parent.parent if cp.parent.name == "configs" else cp.parent


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return data


def load_local_config(path: Path | str) -> LocalConfig:
    path = Path(path)
    cfg = LocalConfig.model_validate(load_yaml(path))
    root = project_root_from(path)
    cfg.risk_ruleset = _resolve_rel(cfg.risk_ruleset, root)
    cfg.analysis.experiment_config = _resolve_rel(cfg.analysis.experiment_config, root)
    cfg.analysis.portfolio_snapshot = _resolve_rel(cfg.analysis.portfolio_snapshot, root)
    cfg.fixture_bars = _resolve_rel(cfg.fixture_bars, root)
    cfg.paths.data_root = _resolve_rel(cfg.paths.data_root, root)
    cfg.paths.artifact_root = _resolve_rel(cfg.paths.artifact_root, root)
    return cfg


def load_experiment_config(path: Path | str) -> tuple[ExperimentConfig, LocalConfig]:
    path = Path(path)
    exp = ExperimentConfig.model_validate(load_yaml(path))
    root = project_root_from(path)
    exp.base_config = _resolve_rel(exp.base_config, root)
    base = load_local_config(exp.base_config)
    return exp, base


def resolve_secret(env_name: str | None) -> str | None:
    """Secrets are read from environment variables only and never logged."""
    if not env_name:
        return None
    val = os.environ.get(env_name)
    return val if val else None


__all__ = [
    "ExperimentConfig",
    "LocalConfig",
    "config_hash",
    "load_experiment_config",
    "load_local_config",
    "resolve_secret",
]

_ = datetime  # keep import for type users
