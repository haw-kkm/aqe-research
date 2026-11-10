from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fixture_cfg(tmp_path, monkeypatch):
    """LocalConfig pointing at the SYNTHETIC_FIXTURE bars with an isolated temp data/artifact root."""
    from aqe.config import load_local_config

    monkeypatch.setenv("AQE_FIXTURE_ROOT", str(tmp_path))
    return load_local_config(ROOT / "configs" / "fixture.yaml")


@pytest.fixture
def fixture_bars():
    from aqe.contracts.core import InstrumentKey, MarketType, Venue
    from aqe.storage.bars import load_fixture_bars

    inst = InstrumentKey(venue=Venue.BINANCE, market_type=MarketType.USDM_PERPETUAL, instrument_id="BTCUSDT")
    return inst, load_fixture_bars(ROOT / "tests" / "fixtures" / "synthetic_bars_btcusdt_5m.csv", inst, 2.0)


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def network_enabled() -> bool:
    return os.environ.get("AQE_NETWORK_TESTS") == "1"
