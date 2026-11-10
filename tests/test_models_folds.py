"""T04 (TCN causality/shape), T05 (fold purge / label maturity), T06 (artifact identity)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import torch

from aqe.config import ExperimentConfig
from aqe.datasets.build import assert_purged, fit_k, make_folds, make_labels, rows_for_training
from aqe.models.registry import ArtifactMismatch, check_identity
from aqe.models.tcn import Normalizer, SmallTCN, valid_window_ends
from tests.conftest import utc


def test_tcn_receptive_field_and_shapes():
    m = SmallTCN(24, 32, 3, [1, 2, 4, 8, 16, 32], 0.1)
    assert m.receptive_field() == 1 + 2 * (3 - 1) * (1 + 2 + 4 + 8 + 16 + 32) == 253
    x = torch.randn(4, 24, 256)
    assert m(x).shape == (4, 3)
    assert m.forward_sequence(x).shape == (4, 3, 256)


def test_tcn_future_inputs_do_not_change_past_outputs():
    torch.manual_seed(0)
    m = SmallTCN(5, 8, 3, [1, 2, 4], 0.0).eval()
    x = torch.randn(2, 5, 64)
    y1 = m.forward_sequence(x)
    x2 = x.clone()
    x2[:, :, 40:] = torch.randn(2, 5, 24) * 100  # perturb the future
    y2 = m.forward_sequence(x2)
    assert torch.allclose(y1[:, :, :40], y2[:, :, :40], atol=1e-6)
    assert not torch.allclose(y1[:, :, 40:], y2[:, :, 40:])


def test_tcn_output_depends_only_on_receptive_field():
    torch.manual_seed(1)
    m = SmallTCN(3, 8, 3, [1, 2, 4, 8, 16, 32], 0.0).eval()
    rf = m.receptive_field()
    x = torch.randn(1, 3, 300)
    x2 = x.clone()
    x2[:, :, : 300 - rf] = 0.0  # change inputs older than the receptive field
    assert torch.allclose(m(x), m(x2), atol=1e-6)


def test_valid_window_ends_and_normalizer_train_only():
    X = np.ones((10, 2), dtype="float32")
    X[3, 0] = np.nan
    ok = valid_window_ends(X, 3)
    assert list(ok) == [False, False, True, False, False, False, True, True, True, True]
    train = np.arange(20, dtype="float32").reshape(10, 2)
    n = Normalizer.fit(train[:5])
    assert np.allclose(n.mean, train[:5].mean(0))


def _exp():
    return ExperimentConfig.model_validate({
        "experiment_id": "t", "base_config": "configs/local.yaml",
        "instrument": {"venue": "binance", "market_type": "usdm_perpetual", "instrument_id": "BTCUSDT"},
        "segments": {"dev_start": "2020-01-01T00:00:00Z", "first_oos_start": "2022-01-01T00:00:00Z", "fold_months": 6,
                     "calibration_start": "2023-07-01T00:00:00Z", "test_start": "2024-01-01T00:00:00Z", "embargo_seconds": 600},
    })


def test_folds_are_contiguous_and_purged():
    folds = make_folds(_exp(), 86400)
    assert [f.oos_start.isoformat() for f in folds] == ["2022-01-01T00:00:00+00:00", "2022-07-01T00:00:00+00:00", "2023-01-01T00:00:00+00:00"]
    for a, b in zip(folds, folds[1:]):
        assert a.oos_end == b.oos_start
    assert folds[-1].oos_end == utc(2023, 7, 1)
    for f in folds:
        assert (f.oos_start - f.train_label_end_max).total_seconds() == 600


def test_rows_for_training_respects_label_end():
    t = pd.date_range("2021-12-29", periods=2000, freq="5min", tz="UTC")
    h = 86400
    df = pd.DataFrame({"as_of": t + pd.Timedelta(seconds=10), f"label_end_at_{h}": t + pd.Timedelta(seconds=h + 2), f"y_{h}": 1.0})
    boundary = utc(2022, 1, 1)
    tr = rows_for_training(df, h, utc(2021, 1, 1), boundary)
    assert tr[f"label_end_at_{h}"].max() <= pd.Timestamp(boundary)
    assert tr["as_of"].max() < pd.Timestamp(boundary) - pd.Timedelta(hours=23)  # 24h label maturity lag
    assert_purged(tr, h, boundary)
    with pytest.raises(AssertionError):
        assert_purged(df, h, boundary)


def test_labels_and_k_fit():
    rng = np.random.default_rng(0)
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.001, 5000))))
    sigma = np.log(close).diff().rolling(288).std()
    k = fit_k(np.log(close.shift(-12) / close), sigma, 12, 0.001, 0.34)
    r, b, y = make_labels(close, sigma, 12, k, 0.001)
    frac = (y.dropna() == 1).mean()
    assert 0.3 < frac < 0.38
    assert y.iloc[-12:].isna().all()  # incomplete labels excluded
    assert ((y == 2) == (r > b)).loc[y.notna()].all()


def test_artifact_identity_mismatch_rejected():
    meta = {"instrument_id": "BTCUSDT", "horizon_seconds": 3600, "feature_profile": "ohlcv_v0", "feature_schema_hash": "abc",
            "feature_names": ["a", "b"], "class_order": ["down", "neutral", "up"]}
    check_identity(meta, instrument_id="BTCUSDT", horizon_seconds=3600, feature_profile="ohlcv_v0", feature_schema_hash="abc", feature_names=["a", "b"])
    for kw in ({"instrument_id": "ETHUSDT"}, {"horizon_seconds": 86400}, {"feature_profile": "flow_v1"}, {"feature_schema_hash": "zzz"},
               {"feature_names": ["b", "a"]}):
        args = dict(instrument_id="BTCUSDT", horizon_seconds=3600, feature_profile="ohlcv_v0", feature_schema_hash="abc", feature_names=["a", "b"])
        args.update(kw)
        with pytest.raises(ArtifactMismatch):
            check_identity(meta, **args)
