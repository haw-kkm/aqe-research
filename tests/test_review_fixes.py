"""Regression tests for the 2026-11-11 code review (commit 6c9ae84) findings F01-F06.

Each test feeds the input that the review's fault-injection script showed was wrongly accepted and
asserts it is now rejected / recomputed. Synthetic data, temp dirs and loopback sockets only.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from aqe.contracts.context import Claim, ContextStatus, TemporalScope
from aqe.contracts.core import Disposition, InstrumentKey, ModelReadiness, RuleOutcome, RuntimeMode
from aqe.contracts.data import FeatureSnapshot
from aqe.contracts.predictions import EnsembleStatus
from aqe.ensemble.meta import MetaModel
from aqe.ensemble.online import predict_horizon
from aqe.models.registry import artifact_dir, save_metadata, update_registry
from aqe.risk.engine import evaluate_risk
from aqe.storage.metadb import JobQueue
from tests.conftest import ROOT, utc
from tests.test_context_risk import _assessment, _bundle, _ok_inputs, _rs

INST = InstrumentKey(venue="binance", market_type="usdm_perpetual", instrument_id="BTCUSDT")
NAMES = ("rv_288", "trend_288", "vol_ratio_12_288")
AS_OF = datetime(2026, 3, 2, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------------------------
# F01 / F02 helpers: a registry with base + meta + calibrator around a dummy base predictor
# ---------------------------------------------------------------------------------------------
class _Dummy:
    def predict_latest(self, feature_frame):
        return np.array([0.2, 0.3, 0.5])


def _chain(tmp: Path, **override) -> dict[str, dict]:
    ids = {k: f"t-{k}" for k in ("lgbm", "tcn", "meta", "calibrator")}
    metas = {}
    for kind, aid in ids.items():
        m = dict(artifact_id=aid, kind=kind, experiment_id="t", instrument_id="BTCUSDT", feature_profile="ohlcv_v0",
                 feature_schema_hash="schema", feature_names=list(NAMES), horizon_seconds=3600,
                 class_order=["down", "neutral", "up"], train_label_cutoff="2025-01-01T00:00:00Z", dataset_manifest_hash="d",
                 config_hash="c", code_version="t", seed=0, readiness="TRAINED_UNVALIDATED", created_at="2026-11-11T00:00:00Z")
        if kind == "calibrator":
            m.update(combo="numeric_only", fitted_on_artifacts={"meta": ids["meta"], "lgbm": ids["lgbm"], "tcn": ids["tcn"]})
        m.update(override.get(kind, {}))
        metas[kind] = m
        save_metadata(tmp, "t", m)
    for kind, slot in {"lgbm": "lgbm", "tcn": "tcn", "meta": "meta_numeric_only", "calibrator": "calib_numeric_only"}.items():
        update_registry(tmp, "t", 3600, slot, ids[kind])
    mm = MetaModel(["lgbm", "tcn"], np.zeros(11), np.ones(11), np.zeros((3, 11)), np.zeros(3), {n: (-10, 10) for n in NAMES})
    (artifact_dir(tmp, "t", ids["meta"]) / "meta_model.json").write_text(json.dumps(mm.to_json()))
    (artifact_dir(tmp, "t", ids["calibrator"]) / "calibrator.json").write_text('{"temperature": 1.0}')
    return metas


def _run(tmp: Path, as_of: datetime = AS_OF):
    snap = FeatureSnapshot(instrument=INST, as_of=as_of, feature_profile="ohlcv_v0", feature_names=NAMES, values=(0.1, 0.1, 0.1),
                           missing_mask=(False,) * 3, source_watermarks={"bars": as_of}, provenance="SYNTHETIC_FIXTURE",
                           quality="OK", feature_schema_hash="schema")
    with patch("aqe.ensemble.online._load_base", return_value=_Dummy()):
        return predict_horizon(artifact_root=tmp, experiment_id="t", snapshot=snap, feature_frame=pd.DataFrame([dict(zip(NAMES, snap.values))]),
                               horizon_seconds=3600, mode=RuntimeMode.REPLAY, interval_seconds=300, context=None)


def test_chain_ok_baseline(tmp_path):
    _chain(tmp_path)
    _, ens, _ = _run(tmp_path)
    assert ens.status == EnsembleStatus.OK


@pytest.mark.parametrize("kind", ["meta", "calibrator"])
def test_f01_future_trained_meta_or_calibrator_rejected(tmp_path, kind):
    _chain(tmp_path, **{kind: {"train_label_cutoff": "2027-01-01T00:00:00Z"}})
    _, ens, _ = _run(tmp_path)
    assert ens.status == EnsembleStatus.ABSTAIN and ens.probabilities is None
    assert any("future leakage" in r for r in ens.abstain_reasons)


@pytest.mark.parametrize("kind,field,value", [
    ("meta", "horizon_seconds", 86400), ("calibrator", "horizon_seconds", 86400),
    ("meta", "instrument_id", "ETHUSDT"), ("calibrator", "instrument_id", "ETHUSDT"),
    ("calibrator", "feature_schema_hash", "other"), ("meta", "kind", "calibrator"),
])
def test_f01_other_horizon_or_instrument_components_rejected(tmp_path, kind, field, value):
    _chain(tmp_path, **{kind: {field: value}})
    _, ens, _ = _run(tmp_path)
    assert ens.status == EnsembleStatus.ABSTAIN


def test_f01_validation_counts_only_after_it_was_established(tmp_path):
    hist = [{"readiness": "OOS_EVALUATED", "evidence": "locked test", "established_at": "2026-11-10T19:40:00Z"}]
    validated = {"readiness": "OOS_EVALUATED", "readiness_history": hist, "calibration_validated": True,
                 "oos_verdict": "SKILL_DEMONSTRATED", "oos_cost_verdict": "NOT_POSITIVE"}
    _chain(tmp_path, calibrator=validated, meta={"readiness": "OOS_EVALUATED", "readiness_history": hist})
    _, ens, _ = _run(tmp_path, AS_OF)  # 2026-03-02: before the evidence existed
    assert ens.status == EnsembleStatus.OK
    assert ens.calibration_validated is False and ens.readiness == ModelReadiness.TRAINED_UNVALIDATED
    assert ens.extra["oos_verdict"] == "NOT_ESTABLISHED_AT_AS_OF"
    assert ens.extra["oos_cost_verdict"] == "NOT_POSITIVE"  # blocking verdicts still apply
    _, ens2, _ = _run(tmp_path, utc(2026, 11, 11))
    assert ens2.calibration_validated is True and ens2.extra["oos_verdict"] == "SKILL_DEMONSTRATED"


def test_f01_legacy_readiness_history_parsed():
    from aqe.models.chain import established_at

    meta = {"readiness": "OOS_EVALUATED", "readiness_history": [
        {"readiness": "OOS_EVALUATED", "evidence": "locked test 2025-07-01T00:00:00Z..2026-11-10T19:40:00Z run run-x trial 1"}]}
    assert established_at(meta) == utc(2026, 11, 10, 19, 40)
    assert established_at({"readiness": "OOS_EVALUATED"}) is None


def test_f01_risk_r1_checks_meta_and_calibrator_cutoffs(fixture_bars):
    rs, h = _rs()
    inp = _ok_inputs(fixture_bars)
    inp.artifact_meta = inp.artifact_meta + [{**inp.artifact_meta[0], "artifact_id": "cal", "kind": "calibrator", "train_label_cutoff": "2027-01-01T00:00:00Z"}]
    d = evaluate_risk(inp, rs, h)
    r1 = [r for r in d.rules if r.rule_id == "R1_READINESS_ARTIFACT"][0]
    assert r1.outcome == RuleOutcome.FAIL and d.disposition == Disposition.ABSTAIN


@pytest.mark.parametrize("lineage", [
    {"meta": "OLD-meta", "lgbm": "OLD-lgbm", "tcn": "OLD-tcn"},
    {"meta": "t-meta", "lgbm": "t-lgbm", "tcn": "OLD-tcn"},
    {"meta": "t-meta", "lgbm": "t-lgbm"},  # incomplete lineage
    None,  # no lineage recorded
])
def test_f02_calibrator_lineage_must_match_current_chain(tmp_path, lineage):
    _chain(tmp_path, calibrator={"fitted_on_artifacts": lineage})
    _, ens, _ = _run(tmp_path)
    assert ens.status == EnsembleStatus.ABSTAIN
    assert any("fitted on" in r or "lineage" in r for r in ens.abstain_reasons)


def test_f02_retraining_a_base_model_invalidates_old_calibrator(tmp_path):
    metas = _chain(tmp_path)
    save_metadata(tmp_path, "t", {**metas["lgbm"], "artifact_id": "t-lgbm-v2"})
    update_registry(tmp_path, "t", 3600, "lgbm", "t-lgbm-v2")
    _, ens, _ = _run(tmp_path)
    assert ens.status == EnsembleStatus.ABSTAIN
    assert (artifact_dir(tmp_path, "t", "t-calibrator") / "calibrator.json").exists()  # nothing overwritten/deleted


# ---------------------------------------------------------------------------------------------
# F03 network guard
# ---------------------------------------------------------------------------------------------
@pytest.fixture
def bridge():
    import importlib.util

    spec = importlib.util.spec_from_file_location("ta_bridge_f03", ROOT / "src" / "aqe" / "context" / "ta_bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _listener(addr: str):
    s = socket.socket()
    s.bind((addr, 0))
    s.listen(1)
    s.settimeout(2)
    return s


@pytest.mark.parametrize("allow", [set(), {"api.openai.com"}])
def test_f03_direct_ip_not_on_allowlist_is_blocked(bridge, allow):
    lst = _listener("127.0.0.2")
    uninstall = bridge.install_network_guard(allow)
    try:
        with socket.socket() as c:
            c.settimeout(2)
            with pytest.raises(OSError):
                c.connect(lst.getsockname())
        assert "127.0.0.2" in bridge.AUDIT["network_denied"]
        with socket.socket() as c2, pytest.raises(OSError):
            c2.connect_ex(lst.getsockname())
    finally:
        uninstall()
        lst.close()


def test_f03_allowlisted_name_maps_to_resolved_ips_only(bridge):
    lst = _listener("127.0.0.1")
    uninstall = bridge.install_network_guard({"localhost"})  # explicit loopback allowance (e.g. local Ollama)
    try:
        infos = socket.getaddrinfo("localhost", lst.getsockname()[1], socket.AF_INET, socket.SOCK_STREAM)
        with socket.socket() as c:
            c.settimeout(2)
            c.connect(infos[0][4])  # allowed: IP obtained from an allow-listed name
        with pytest.raises(OSError):
            socket.getaddrinfo("example.com", 443)
        assert "example.com" in bridge.AUDIT["network_denied"]
    finally:
        uninstall()
        lst.close()


def test_f03_socketpair_still_works_under_guard(bridge):
    uninstall = bridge.install_network_guard(set())
    try:
        a, b = socket.socketpair()  # asyncio self-pipe on Windows uses this
        a.sendall(b"x")
        assert b.recv(1) == b"x"
        a.close()
        b.close()
    finally:
        uninstall()


def test_f03_proxy_env_recorded_and_marks_unverified(bridge, monkeypatch, fixture_cfg):
    from aqe.context.backends import TradingAgentsBackend

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:8080")
    uninstall = bridge.install_network_guard({"api.openai.com"})
    uninstall()
    assert bridge.AUDIT["proxy_env"] == ["HTTPS_PROXY"]
    b = _bundle(utc(2026, 1, 1, 12))
    res = {"status": "OK", "final_rating": "Hold", "memory_log_disabled": True, "cited_ids": ["e1"], "reports": {"market_report": "x [ev:e1]"},
           "audit": {"tool_calls": [], "network_denied": [], "guard_violations": [], "proxy_env": ["HTTPS_PROXY"], "llm_calls": 1}}
    a = TradingAgentsBackend(fixture_cfg).to_assessment(b, res, {"provider": "offline_scripted"}, 1.0)
    assert a.temporal_scope == TemporalScope.UNVERIFIED


# ---------------------------------------------------------------------------------------------
# F04 budget
# ---------------------------------------------------------------------------------------------
def _paid_cfg(fixture_cfg, cap=0.01):
    cfg = fixture_cfg.model_copy(deep=True)
    cfg.context.max_cost_usd_per_day = cap
    cfg.context.llm.provider = "openai"
    cfg.context.llm.usd_per_1k_prompt_tokens = 0.0001
    cfg.context.llm.usd_per_1k_completion_tokens = 0.0004
    return cfg


def test_f04_unknown_cost_blocks_next_paid_job(fixture_cfg, tmp_path):
    from aqe.context.backends import _budget_ok, reserve_budget

    cfg = _paid_cfg(fixture_cfg)
    q = JobQueue(tmp_path / "b.sqlite")
    q.add_usage("tradingagents", 1, None)
    assert _budget_ok(cfg, q, "tradingagents", "openai")[0] is False
    ok, reason, _, _ = reserve_budget(cfg, q, "tradingagents", "openai", None, calls=1)
    assert ok is False and "unknown cost" in reason


def test_f04_reservation_bounds_concurrent_jobs_and_settles(fixture_cfg, tmp_path):
    from aqe.context.backends import job_cost_estimate, reserve_budget

    cfg = _paid_cfg(fixture_cfg, cap=0.01)
    per_job = job_cost_estimate(cfg, 1)  # 8000*0.0001/1000 + 2000*0.0004/1000 = 0.0016
    q = JobQueue(tmp_path / "b.sqlite")
    granted = [reserve_budget(cfg, q, "simple_llm", "openai", None, calls=1) for _ in range(10)]
    n_ok = sum(1 for g in granted if g[0])
    assert n_ok == int(0.01 // per_job) and n_ok < 10  # reservations, not spend after the fact, enforce the cap
    q.settle("simple_llm", granted[0][2], 0.0001, True)
    u = q.usage_today("simple_llm")
    assert u["cost_usd"] == pytest.approx(0.0001) and u["reserved_usd"] == pytest.approx(per_job * (n_ok - 1))
    q.settle("simple_llm", granted[1][2], None, True)  # unknown -> reservation charged + flagged
    u = q.usage_today("simple_llm")
    assert u["cost_unknown_calls"] == 1 and u["cost_usd"] == pytest.approx(0.0001 + per_job)


def test_f04_provider_name_alone_is_not_free():
    from aqe.context.backends import is_free_local

    assert is_free_local("ollama", None) is True
    assert is_free_local("openai_compatible", "http://127.0.0.1:8000/v1") is True
    assert is_free_local("openai_compatible", "https://paid.example.com/v1") is False
    assert is_free_local("openai_compatible", None) is False
    assert is_free_local("ollama", "https://remote-ollama.example.com/v1") is False
    assert is_free_local("openai", None) is False


def test_f04_ta_incomplete_tokens_charge_reservation(fixture_cfg, tmp_path):
    from aqe.context.backends import TradingAgentsBackend, reserve_budget

    cfg = _paid_cfg(fixture_cfg, cap=1.0)
    q = JobQueue(tmp_path / "b.sqlite")
    be = TradingAgentsBackend(cfg, q)
    ok, _, reserved, paid = reserve_budget(cfg, q, "tradingagents", "openai", None, calls=cfg.context.max_llm_calls_per_job)
    assert ok and paid and reserved > 0
    be._reservation = (reserved, paid, cfg.context.max_llm_calls_per_job)
    b = _bundle(utc(2026, 1, 1, 12))
    res = {"status": "OK", "final_rating": "Hold", "memory_log_disabled": True, "cited_ids": [], "reports": {},
           "audit": {"tool_calls": [], "network_denied": [], "guard_violations": [], "llm_calls": 12, "prompt_tokens": 5000,
                     "completion_tokens": 900, "tokens_complete": False}}
    a = be.to_assessment(b, res, {"provider": "openai"}, 3.0)
    assert a.usage.cost_estimate_usd is None and "reservation charged" in a.usage.cost_basis
    u = q.usage_today("tradingagents")
    assert u["cost_unknown_calls"] == 1 and u["cost_usd"] == pytest.approx(reserved) and u["reserved_usd"] == pytest.approx(0)
    res["audit"]["budget_abort"] = "LLM call 41 exceeds max_llm_calls=40"
    assert be.to_assessment(b, res, {"provider": "openai"}, 3.0).temporal_scope == TemporalScope.UNVERIFIED


# ---------------------------------------------------------------------------------------------
# F05 event risk vs missing / unverified context
# ---------------------------------------------------------------------------------------------
GOOD_CLAIM = (Claim(text="c", kind="OBSERVATION", direction="NONE", evidence_ids=("e1",)),)


@pytest.mark.parametrize("coverage,scope,cal_days", [
    ({"MACRO_CALENDAR": "NOT_CONFIGURED", "NEWS": "NOT_CONFIGURED"}, TemporalScope.INTRADAY_ASOF_VERIFIED, 3),
    ({"MACRO_CALENDAR": "AVAILABLE", "NEWS": "STALE"}, TemporalScope.INTRADAY_ASOF_VERIFIED, 3),
    ({"MACRO_CALENDAR": "AVAILABLE", "NEWS": "AVAILABLE"}, TemporalScope.UNVERIFIED, 3),
    ({"MACRO_CALENDAR": "AVAILABLE", "NEWS": "AVAILABLE"}, TemporalScope.DAILY_CONTEXT_ONLY, 3),
    ({"MACRO_CALENDAR": "AVAILABLE", "NEWS": "AVAILABLE"}, TemporalScope.INTRADAY_ASOF_VERIFIED, None),
])
def test_f05_unknown_event_information_never_passes(fixture_bars, coverage, scope, cal_days):
    rs, h = _rs()
    b = _bundle(utc(2026, 1, 10, 0, 0, 10))
    inp = _ok_inputs(fixture_bars, context=_assessment(b, GOOD_CLAIM, coverage=coverage, scope=scope, calendar_days=cal_days))
    d = evaluate_risk(inp, rs, h)
    r4 = [r for r in d.rules if r.rule_id == "R4_EVENT_LIQUIDITY_COST"][0]
    assert r4.outcome == RuleOutcome.UNKNOWN and d.disposition != Disposition.REVIEW_CANDIDATE


def test_f05_calendar_window_shorter_than_horizon_is_unknown(fixture_bars):
    rs, h = _rs()
    b = _bundle(utc(2026, 1, 10, 0, 0, 10))
    ctx = _assessment(b, GOOD_CLAIM)
    ctx = ctx.model_copy(update={"calendar_coverage_until": b.as_of + timedelta(minutes=30)})
    d = evaluate_risk(_ok_inputs(fixture_bars, context=ctx), rs, h)  # horizon 1h > 30 min coverage
    assert [r for r in d.rules if r.rule_id == "R4_EVENT_LIQUIDITY_COST"][0].outcome == RuleOutcome.UNKNOWN


def test_f05_verified_empty_calendar_is_a_real_pass(fixture_bars):
    rs, h = _rs()
    b = _bundle(utc(2026, 1, 10, 0, 0, 10))
    d = evaluate_risk(_ok_inputs(fixture_bars, context=_assessment(b, GOOD_CLAIM)), rs, h)
    assert [r for r in d.rules if r.rule_id == "R4_EVENT_LIQUIDITY_COST"][0].outcome == RuleOutcome.PASS
    assert d.disposition == Disposition.REVIEW_CANDIDATE


def test_f05_unverified_context_not_combined(tmp_path):
    from aqe.contracts.context import context_usable_for_decision

    b = _bundle(AS_OF)
    assert context_usable_for_decision(_assessment(b, GOOD_CLAIM)) is True
    assert context_usable_for_decision(_assessment(b, GOOD_CLAIM, scope=TemporalScope.UNVERIFIED)) is False
    invalid = _assessment(b, GOOD_CLAIM).model_copy(update={"status": ContextStatus.INVALID, "status_reasons": ("unknown evidence ids",)})
    assert context_usable_for_decision(invalid) is False


# ---------------------------------------------------------------------------------------------
# F06 prediction cache keyed by input content
# ---------------------------------------------------------------------------------------------
def test_f06_cache_recomputes_when_inputs_change_and_reuses_when_identical(tmp_path):
    import aqe.ensemble.calibration as calibration

    cfg = SimpleNamespace(paths=SimpleNamespace(artifact_root=tmp_path))
    exp = SimpleNamespace(experiment_id="cache", instrument=INST)
    df = pd.DataFrame({"open_time": pd.to_datetime(["2026-01-01T00:00:00Z", "2026-01-01T00:05:00Z"]), "f": [0.0, 1.0]})
    p1, p2 = np.array([[0.2, 0.3, 0.5]] * 2), np.array([[0.7, 0.2, 0.1]] * 2)
    meta = {"feature_names": ["f"]}
    with patch.object(calibration, "load_predictor", return_value=(meta, object())), patch.object(calibration, "predict_rows", return_value=p1) as r1:
        first, _ = calibration.segment_predictions(cfg, exp, df, 3600, df.index, {"lgbm": "a1"})
        again, _ = calibration.segment_predictions(cfg, exp, df, 3600, df.index, {"lgbm": "a1"})
    assert r1.call_count == 1 and np.allclose(first["lgbm"], again["lgbm"])  # identical input: reuse
    df.loc[0, "f"] = 99.0
    with patch.object(calibration, "load_predictor", return_value=(meta, object())), patch.object(calibration, "predict_rows", return_value=p2) as r2:
        second, _ = calibration.segment_predictions(cfg, exp, df, 3600, df.index, {"lgbm": "a1"})
    assert r2.call_count == 1 and np.allclose(second["lgbm"], p2)  # changed input: recomputed


def test_f06_cache_recomputes_when_model_file_changes(tmp_path):
    import aqe.ensemble.calibration as calibration

    cfg = SimpleNamespace(paths=SimpleNamespace(artifact_root=tmp_path))
    exp = SimpleNamespace(experiment_id="cache", instrument=INST)
    adir = artifact_dir(tmp_path, "cache", "a1")
    adir.mkdir(parents=True)
    (adir / "model.txt").write_text("v1")
    df = pd.DataFrame({"open_time": pd.to_datetime(["2026-01-01T00:00:00Z"]), "f": [0.0]})
    meta = {"feature_names": ["f"]}
    with patch.object(calibration, "load_predictor", return_value=(meta, object())), patch.object(calibration, "predict_rows", return_value=np.array([[0.2, 0.3, 0.5]])):
        calibration.segment_predictions(cfg, exp, df, 3600, df.index, {"lgbm": "a1"})
    (adir / "model.txt").write_text("v2")  # model rewritten under the same artifact id
    with patch.object(calibration, "load_predictor", return_value=(meta, object())), patch.object(calibration, "predict_rows", return_value=np.array([[0.6, 0.3, 0.1]])) as r:
        out, _ = calibration.segment_predictions(cfg, exp, df, 3600, df.index, {"lgbm": "a1"})
    assert r.call_count == 1 and np.allclose(out["lgbm"], [[0.6, 0.3, 0.1]])
