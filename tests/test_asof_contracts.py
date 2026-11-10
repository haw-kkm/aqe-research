"""T01 (as-of / future-data blocking) and core contract validation (B04, B06)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from pydantic import ValidationError

from aqe.contracts.context import (
    EvidenceBundle,
    EvidenceItem,
    ScheduledEvent,
    SourceHealth,
    SourceHealthStatus,
    SourceKind,
    VerificationStatus,
    compute_bundle_hash,
)
from aqe.contracts.core import ClassProbabilities, Provenance
from aqe.features.snapshot import build_snapshot
from aqe.storage.bars import bars_as_of
from tests.conftest import utc


def test_probabilities_validated():
    ClassProbabilities(p_down=0.2, p_neutral=0.3, p_up=0.5)
    with pytest.raises(ValidationError):
        ClassProbabilities(p_down=0.2, p_neutral=0.3, p_up=0.6)
    with pytest.raises(ValidationError):
        ClassProbabilities(p_down=float("nan"), p_neutral=0.5, p_up=0.5)
    with pytest.raises(ValidationError):
        ClassProbabilities(p_down=-0.1, p_neutral=0.6, p_up=0.5)


def test_naive_timestamps_rejected():
    from aqe.contracts.core import InstrumentKey, PredictionKey

    inst = InstrumentKey(venue="binance", market_type="usdm_perpetual", instrument_id="BTCUSDT")
    with pytest.raises(ValidationError):
        PredictionKey(instrument=inst, as_of=datetime(2026, 1, 1), horizon_seconds=3600, feature_profile="ohlcv_v0")
    k = PredictionKey(instrument=inst, as_of=datetime(2026, 1, 1, 9, tzinfo=timezone(timedelta(hours=9))), horizon_seconds=3600, feature_profile="ohlcv_v0")
    assert k.as_of == utc(2026, 1, 1, 0)


def test_bars_as_of_excludes_future_unclosed_and_later_revisions(fixture_bars):
    inst, bars = fixture_bars
    as_of = utc(2026, 1, 5, 12, 0, 10)
    sel = bars_as_of(bars, as_of)
    assert (sel["available_at"] <= pd.Timestamp(as_of)).all()
    assert (sel["close_time"] <= pd.Timestamp(as_of)).all()
    # bar closing exactly at 12:00 becomes available at 12:00:02 <= 12:00:10 -> included; next one not
    assert sel["close_time"].max() == pd.Timestamp(utc(2026, 1, 5, 12, 0))
    # an unclosed bar and a revised (later available) copy must not leak in
    extra = bars.iloc[[-1]].copy()
    extra["open_time"] = pd.Timestamp(utc(2026, 1, 5, 12, 0))
    extra["close_time"] = pd.Timestamp(utc(2026, 1, 5, 12, 5))
    extra["is_closed"] = False
    revised = sel.iloc[[-1]].copy()
    revised["close"] = 1.0
    revised["available_at"] = pd.Timestamp(as_of) + pd.Timedelta(minutes=1)  # revision published later
    sel2 = bars_as_of(pd.concat([bars, extra, revised]), as_of)
    assert sel2["close"].iloc[-1] == sel["close"].iloc[-1]
    assert len(sel2) == len(sel)


def test_snapshot_unchanged_by_future_bars(fixture_bars):
    inst, bars = fixture_bars
    as_of = utc(2026, 1, 9, 0, 0, 10)
    s1, _ = build_snapshot(bars[bars["open_time"] < pd.Timestamp(utc(2026, 1, 9))], inst, as_of)
    s2, _ = build_snapshot(bars, inst, as_of)  # includes bars after as_of
    assert s1.values == s2.values
    assert s2.source_watermarks["bars"] <= as_of
    assert s2.provenance == Provenance.SYNTHETIC_FIXTURE


def _item(eid, avail):
    return EvidenceItem(evidence_id=eid, source_kind=SourceKind.NEWS, source_id="x", title="t", excerpt="e", content_hash="h",
                        received_at=avail, available_at=avail, verification_status=VerificationStatus.REPORTED)


def test_bundle_rejects_future_evidence_and_unreleased_actuals():
    from aqe.contracts.core import InstrumentKey

    inst = InstrumentKey(venue="binance", market_type="usdm_perpetual", instrument_id="BTCUSDT")
    as_of = utc(2026, 3, 1, 12)
    health = (SourceHealth(source_kind=SourceKind.NEWS, status=SourceHealthStatus.AVAILABLE),)
    ok = (_item("a", as_of - timedelta(minutes=1)),)
    EvidenceBundle(bundle_id="b", instruments=(inst,), as_of=as_of, items=ok, source_health=health,
                   bundle_hash=compute_bundle_hash((inst,), as_of, ok, (), health))
    fut = (_item("f", as_of + timedelta(seconds=1)),)
    with pytest.raises(ValidationError):
        EvidenceBundle(bundle_id="b", instruments=(inst,), as_of=as_of, items=fut, source_health=health,
                       bundle_hash=compute_bundle_hash((inst,), as_of, fut, (), health))
    cal = (ScheduledEvent(event_id="cpi", name="US CPI", source_id="bls", scheduled_time=as_of + timedelta(hours=1), importance="HIGH",
                          available_at=as_of - timedelta(days=1), actual_value="3.1%", actual_available_at=as_of + timedelta(hours=1)),)
    with pytest.raises(ValidationError):
        EvidenceBundle(bundle_id="b", instruments=(inst,), as_of=as_of, items=(), calendar_snapshot=cal, source_health=health,
                       bundle_hash=compute_bundle_hash((inst,), as_of, (), cal, health))


def test_feature_snapshot_rejects_future_watermark():
    from aqe.contracts.core import InstrumentKey
    from aqe.contracts.data import FeatureSnapshot

    inst = InstrumentKey(venue="binance", market_type="usdm_perpetual", instrument_id="BTCUSDT")
    with pytest.raises(ValidationError):
        FeatureSnapshot(instrument=inst, as_of=utc(2026, 1, 1), feature_profile="ohlcv_v0", feature_names=("a",), values=(1.0,),
                        missing_mask=(False,), source_watermarks={"bars": utc(2026, 1, 1, 0, 0, 1)}, provenance="LIVE_CAPTURE",
                        quality="OK", feature_schema_hash="h")
