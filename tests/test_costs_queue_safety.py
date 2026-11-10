"""T12 cost/PnL signs, T13 restart/lease/idempotency, T14 no order path, M0 fixture CLI path."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pandas as pd
import pytest

from aqe.evaluation.costs import max_drawdown, trade_return
from aqe.storage import RawLogWriter, iter_raw_log
from aqe.storage.metadb import JobQueue
from tests.conftest import ROOT, utc

T0, T1 = pd.Timestamp("2026-01-01T00:00:00Z"), pd.Timestamp("2026-01-02T00:00:00Z")
FUND = pd.DataFrame({"funding_time": [pd.Timestamp("2026-01-01T08:00:00Z"), pd.Timestamp("2026-01-01T16:00:00Z"), pd.Timestamp("2026-01-02T00:00:00Z")],
                     "funding_rate": [0.0001, 0.0001, 0.0001]})


def test_long_short_signs_fees_slippage_funding():
    long_ = trade_return(1, 100.0, 110.0, T0, T1, taker_fee_bps=5, slippage_bps=0, funding=None)
    short = trade_return(-1, 100.0, 110.0, T0, T1, taker_fee_bps=5, slippage_bps=0, funding=None)
    assert long_.gross_return == pytest.approx(0.10) and short.gross_return == pytest.approx(-0.10)
    assert long_.net_return == pytest.approx(0.10 - 0.001) and short.net_return == pytest.approx(-0.10 - 0.001)
    slip = trade_return(1, 100.0, 100.0, T0, T1, 0, 10, None)
    assert slip.net_return < 0 and slip.slippage_cost > 0  # adverse on both legs
    sslip = trade_return(-1, 100.0, 100.0, T0, T1, 0, 10, None)
    assert sslip.net_return < 0
    # funding: positive rate -> long pays, short receives; 3 settlements in (T0, T1]
    lf = trade_return(1, 100.0, 100.0, T0, T1, 0, 0, FUND)
    sf = trade_return(-1, 100.0, 100.0, T0, T1, 0, 0, FUND)
    assert lf.funding_cost == pytest.approx(0.0003) and lf.net_return == pytest.approx(-0.0003)
    assert sf.funding_cost == pytest.approx(-0.0003) and sf.net_return == pytest.approx(0.0003)
    # funding at exactly entry time is excluded, at exit time included
    edge = trade_return(1, 100.0, 100.0, pd.Timestamp("2026-01-01T08:00:00Z"), pd.Timestamp("2026-01-01T16:00:00Z"), 0, 0, FUND)
    assert edge.funding_cost == pytest.approx(0.0001)
    with pytest.raises(ValueError):
        trade_return(0, 100, 100, T0, T1, 0, 0, None)
    assert max_drawdown([0.1, -0.2, 0.05]) > 0


def test_job_queue_lease_expiry_and_no_double_completion(tmp_path):
    q = JobQueue(tmp_path / "m.sqlite", lease_seconds=60, max_attempts=3)
    j1 = q.enqueue("key-1", "BTCUSDT", utc(2026, 1, 1), "b1", "tradingagents")
    j1b = q.enqueue("key-1", "BTCUSDT", utc(2026, 1, 1), "b1", "tradingagents")
    assert j1["job_id"] == j1b["job_id"]  # idempotent re-delivery
    now = utc(2026, 1, 1, 0, 0)
    a = q.lease("worker-A", now=now)
    assert a["job_id"] == j1["job_id"] and q.lease("worker-B", now=now) is None
    # worker A dies; lease expires; B reclaims
    b = q.lease("worker-B", now=now + timedelta(seconds=61))
    assert b["job_id"] == j1["job_id"] and b["attempts"] == 2
    assert q.complete(j1["job_id"], "worker-A", "stale-result") is False  # stale holder cannot complete
    assert q.complete(j1["job_id"], "worker-B", "ctx-1") is True
    assert q.complete(j1["job_id"], "worker-B", "ctx-2") is False  # no double completion
    assert q.get(j1["job_id"])["result_assessment_id"] == "ctx-1"


def test_job_failures_retry_then_terminal(tmp_path):
    q = JobQueue(tmp_path / "m.sqlite", lease_seconds=60, max_attempts=2)
    j = q.enqueue("k", "BTCUSDT", utc(2026, 1, 1), "b", "simple_llm")
    q.lease("w")
    assert q.fail(j["job_id"], "w", "timeout") == "PENDING"
    q.lease("w")
    assert q.fail(j["job_id"], "w", "timeout") == "FAILED"


def test_usage_unknown_cost_not_zero(tmp_path):
    q = JobQueue(tmp_path / "m.sqlite")
    q.add_usage("simple_llm", 2, None)
    u = q.usage_today("simple_llm")
    assert u["calls"] == 2 and u["cost_unknown_calls"] == 2 and u["cost_usd"] == 0.0


def test_raw_log_recovers_after_crash(tmp_path):
    w = RawLogWriter(tmp_path, "s", max_bytes=10**9)
    for i in range(5):
        w.write({"i": i})
    w.flush()
    w._fh.close()  # simulate crash: file never rotated
    w2 = RawLogWriter(tmp_path, "s")
    rec = [p for p in w2.closed_files if "recovered" in p.name]
    assert rec and [r["i"] for r in iter_raw_log(rec[0])] == [0, 1, 2, 3, 4]


def test_no_order_execution_paths_in_source():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src").rglob("*.py"))
    for pat in (r"/fapi/v1/order", r"/fapi/v1/batchOrders", r"/fapi/v1/leverage", r"X-MBX-APIKEY", r"newOrderRespType", r"hmac\.new", r"/api/v3/order"):
        assert not re.search(pat, src), pat
    from aqe.config import BinanceConfig
    from aqe.ingestion.binance_rest import BinanceRest

    rest = BinanceRest(BinanceConfig())
    with pytest.raises(ValueError):
        rest.get("/fapi/v1/order", {"symbol": "BTCUSDT"})
    rest.close()


def test_m0_fixture_cli_end_to_end(fixture_cfg, monkeypatch, tmp_path, capsys):
    from aqe.cli.main import main

    code = main(["analyze", "--config", str(ROOT / "configs" / "fixture.yaml")])
    assert code == 4  # explicit NOT_READY: no trained artifacts
    reports = list((tmp_path / "data" / "reports").glob("analysis-*.json"))
    assert reports
    rep = json.loads(reports[0].read_text(encoding="utf-8"))
    assert rep["execution_enabled"] is False and rep["approval_required"] is True
    for h in rep["horizons"]:
        assert h["ensemble"]["status"] == "ABSTAIN" and h["risk"]["disposition"] == "ABSTAIN"
    assert main(["report", "--config", str(ROOT / "configs" / "fixture.yaml")]) == 0
    runs = list((tmp_path / "data" / "runs").glob("run-*.json"))
    assert runs and all(json.loads(r.read_text(encoding="utf-8"))["config_hash"] for r in runs)
